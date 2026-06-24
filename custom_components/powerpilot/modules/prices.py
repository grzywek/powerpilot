"""Price module.

Delegates price retrieval to a pluggable :class:`PriceSource` (HA sensors or the
prądcast.pl API) and contributes hourly buy prices to the forecast, marking
each hour as confirmed or forecast.

It also maintains a rolling **price profile** keyed by ``(weekday, hour)`` from the
confirmed prices it sees over time — this is what lets PowerPilot "understand" how
prices distribute (cheapest 13–16, nights, weekends) and fill interior gaps. The
profile is persisted across restarts and grown by folding in each freshly-completed
past day (plus a one-time backfill of the last week on first run).
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_PRICE_REFRESH_HOURS,
    CONF_PRICE_SOURCE,
    DOMAIN,
    ESTIMATE_WEEKLY_WEIGHTS,
    PRICE_SOURCE_PRADCAST,
    PRICE_SOURCE_SENSOR,
    PRICE_TYPE_CERTAIN,
    PRICE_TYPE_ESTIMATED,
    PRICE_TYPE_FORECAST,
    STORAGE_VERSION_PRICE_ARCHIVE,
)
from ..models import Forecast
from .base import PowerPilotModule
from .price_sources import (
    PradcastPriceSource,
    PriceData,
    PriceSource,
    SensorPriceSource,
)

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
BACKFILL_DAYS = 7  # how many past days to seed into the learned profile
# How many past days of confirmed prices to seed into the archive. Must cover the
# 3-week estimate lookback (+ a buffer) so estimated prices have samples.
_ARCHIVE_BACKFILL_DAYS = 22
_ARCHIVE_RETENTION_DAYS = 90  # how long fetched prices stay in the archive


class PriceProfile:
    """Running average buy price per ``(weekday, hour)``.

    Deduplication is by calendar date (the caller folds each settled day exactly
    once), so the stored state stays compact and restart-safe.
    """

    def __init__(self) -> None:
        self._sum: dict[tuple[int, int], float] = {}
        self._count: dict[tuple[int, int], int] = {}
        self._observed_dates: set[str] = set()

    def is_date_observed(self, day: date) -> bool:
        return day.isoformat() in self._observed_dates

    def observe(self, hour_start: datetime, price: float) -> None:
        key = (hour_start.weekday(), hour_start.hour)
        self._sum[key] = self._sum.get(key, 0.0) + price
        self._count[key] = self._count.get(key, 0) + 1

    def mark_date_observed(self, day: date) -> None:
        self._observed_dates.add(day.isoformat())

    def value(self, weekday: int, hour: int) -> float | None:
        key = (weekday, hour)
        count = self._count.get(key, 0)
        if count == 0:
            return None
        return self._sum[key] / count

    @property
    def samples(self) -> int:
        return sum(self._count.values())

    @property
    def observed_days(self) -> int:
        return len(self._observed_dates)

    def as_matrix(self) -> dict[str, list[float | None]]:
        """7×24 matrix for diagnostics / charts (rows = weekdays)."""
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return {
            day: [self.value(weekday, hour) for hour in range(24)]
            for weekday, day in enumerate(days)
        }

    def to_dict(self) -> dict:
        return {
            "sum": {f"{wd}-{hr}": v for (wd, hr), v in self._sum.items()},
            "count": {f"{wd}-{hr}": c for (wd, hr), c in self._count.items()},
            "observed_dates": sorted(self._observed_dates),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "PriceProfile":
        profile = cls()
        if not data:
            return profile
        for key, value in (data.get("sum") or {}).items():
            wd, hr = (int(x) for x in key.split("-"))
            profile._sum[(wd, hr)] = float(value)
        for key, value in (data.get("count") or {}).items():
            wd, hr = (int(x) for x in key.split("-"))
            profile._count[(wd, hr)] = int(value)
        profile._observed_dates = set(data.get("observed_dates") or [])
        return profile


class PriceArchive:
    """Per-hour energy-price archive with provenance.

    Once a price is fetched from the source it is stored permanently (pruned only
    after :data:`_ARCHIVE_RETENTION_DAYS`), so any past day can be reviewed. The
    layering rule is *estimated → forecast → certain*:

      * a freshly published **forecast** refreshes a prior forecast entry;
      * a **certain** (binding RDN) price is final and is never downgraded back
        to a forecast.

    Estimated prices are **not** stored here — they are derived on read from the
    confirmed history, so they always reflect the latest archive.
    """

    def __init__(self) -> None:
        # {utc_iso_hour: {"energy","type","source","fetched_at","p10","p90"}}
        self._entries: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _key(hour_start: datetime) -> str:
        return (
            dt_util.as_utc(hour_start)
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )

    def record(
        self,
        hour_start: datetime,
        energy: float,
        price_type: str,
        source: str,
        fetched_at: str,
        p10: float | None = None,
        p90: float | None = None,
    ) -> bool:
        """Insert/refresh an entry honouring layering. Returns True if it changed."""
        key = self._key(hour_start)
        existing = self._entries.get(key)
        if (
            existing is not None
            and existing.get("type") == PRICE_TYPE_CERTAIN
            and price_type != PRICE_TYPE_CERTAIN
        ):
            # Certain is final — never downgrade to a forecast.
            return False
        energy = float(energy)
        if existing is not None and (
            existing.get("energy") == energy
            and existing.get("type") == price_type
            and existing.get("p10") == p10
            and existing.get("p90") == p90
        ):
            # Same price + provenance → keep the ORIGINAL fetch time. Re-fetching
            # an unchanged price (e.g. a certain price seen again after a restart)
            # must not bump "pobrano"; only a real value/type change does.
            return False
        self._entries[key] = {
            "energy": energy,
            "type": price_type,
            "source": source,
            "fetched_at": fetched_at,
            "p10": p10,
            "p90": p90,
        }
        return True

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, hour_start: datetime) -> dict[str, Any] | None:
        return self._entries.get(self._key(hour_start))

    def estimate(self, hour_start: datetime) -> tuple[float | None, list[dict[str, Any]]]:
        """Weighted same-weekday+hour average from 1/2/3 weeks ago.

        Returns ``(price, breakdown)``. ``breakdown`` always lists the three
        contributing weeks (date, weight, value, type) for the UI tooltip;
        ``price`` is ``None`` when no historical sample exists. Weights are
        renormalised over whatever samples are available.
        """
        breakdown: list[dict[str, Any]] = []
        for weeks, weight in zip((1, 2, 3), ESTIMATE_WEEKLY_WEIGHTS):
            past = hour_start - timedelta(days=7 * weeks)
            entry = self.get(past)
            breakdown.append(
                {
                    "weeks_ago": weeks,
                    "weight": weight,
                    "date": dt_util.as_local(past).date().isoformat(),
                    "value": entry["energy"] if entry else None,
                    "type": entry["type"] if entry else None,
                }
            )
        total_weight = sum(b["weight"] for b in breakdown if b["value"] is not None)
        if total_weight <= 0:
            return None, breakdown
        price = (
            sum(b["value"] * b["weight"] for b in breakdown if b["value"] is not None)
            / total_weight
        )
        return price, breakdown

    def prune(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=_ARCHIVE_RETENTION_DAYS)
        kept: dict[str, dict[str, Any]] = {}
        for key, value in self._entries.items():
            try:
                stamp = datetime.fromisoformat(key)
            except (ValueError, TypeError):
                continue
            if stamp >= cutoff:
                kept[key] = value
        self._entries = kept

    def to_dict(self) -> dict[str, Any]:
        return {"entries": self._entries}

    @classmethod
    def from_dict(cls, data: dict | None) -> "PriceArchive":
        archive = cls()
        if data:
            archive._entries = dict(data.get("entries") or {})
        return archive


class PriceModule(PowerPilotModule):
    """Provides hourly buy prices to the forecast."""

    domain = "prices"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._data = PriceData()
        self.profile = PriceProfile()
        self.archive = PriceArchive()
        self._store: Store | None = None
        self._archive_store: Store | None = None
        self._last_backfill_day: date | None = None
        self._last_archive_backfill_day: date | None = None
        self._last_source_fetch: datetime | None = None

    def _build_source(self) -> PriceSource:
        if self.config.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_PRADCAST:
            return PradcastPriceSource(self.hass, self.config)
        return SensorPriceSource(self.hass, self.config)

    async def async_setup(self) -> None:
        self._store = Store(
            self.hass,
            STORAGE_VERSION,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_price_profile",
        )
        stored = await self._store.async_load()
        if stored:
            self.profile = PriceProfile.from_dict(stored.get("profile"))
            last = stored.get("last_backfill_day")
            self._last_backfill_day = date.fromisoformat(last) if last else None

        self._archive_store = Store(
            self.hass,
            STORAGE_VERSION_PRICE_ARCHIVE,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_price_archive",
        )
        archived = await self._archive_store.async_load()
        self.archive = PriceArchive.from_dict(archived)
        self.archive.prune()

        await self._maybe_backfill()
        await self._maybe_backfill_archive()

    def _refresh_interval(self) -> timedelta:
        hours = float(self.config.get(CONF_PRICE_REFRESH_HOURS, 3) or 3)
        return timedelta(hours=max(hours, 0.0))

    async def async_update(self) -> None:
        await self._maybe_backfill()
        await self._maybe_backfill_archive()

        now = dt_util.now()
        # Throttle the actual source hit: forecasts only change every few hours,
        # so the optimizer runs off cached prices in between. A fresh restart
        # (empty buy data) always fetches immediately.
        if (
            self._data.buy
            and self._last_source_fetch is not None
            and (now - self._last_source_fetch) < self._refresh_interval()
        ):
            next_fetch = self._last_source_fetch + self._refresh_interval()
            self.log_info(
                f"Ceny z pamięci podręcznej (następne pobranie ~{next_fetch.strftime('%H:%M')}). "
                f"Archiwum: {len(self.archive)} godzin.",
                extra={
                    "cached": True,
                    "next_fetch": next_fetch.isoformat(),
                    "archive_hours": len(self.archive),
                },
            )
            return

        source = self._build_source()
        self._data = await source.async_fetch()
        self._last_source_fetch = now
        await self._ingest_into_archive(now)
        confirmed_count = len(self._data.confirmed_hours)
        total_count = len(self._data.buy)
        forecast_count = total_count - confirmed_count
        last_hour = max(self._data.buy) if self._data.buy else None
        source_label = (
            "pradcast"
            if self.config.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_PRADCAST
            else "sensor"
        )
        self.log_info(
            f"Źródło {source_label}: {confirmed_count}h potwierdzonych + {forecast_count}h prognozy "
            f"(do {last_hour.isoformat() if last_hour else '–'}). "
            f"Profil: {self.profile.observed_days} dni / {self.profile.samples} próbek. "
            f"Archiwum: {len(self.archive)} godzin.",
            extra={
                "source": source_label,
                "confirmed_hours": confirmed_count,
                "forecast_hours": forecast_count,
                "last_priced_hour": last_hour.isoformat() if last_hour else None,
                "profile_days": self.profile.observed_days,
                "profile_samples": self.profile.samples,
                "archive_hours": len(self.archive),
            },
        )

    async def async_fetch_forecasts(self, target_date) -> dict:
        """Horizon-indexed forecasts (D+1..D+3) for the overlay, if supported."""
        source = self._build_source()
        if isinstance(source, PradcastPriceSource):
            return await source.async_fetch_forecasts(target_date)
        return {}

    def price_at(self, hour) -> float | None:
        return self._data.buy.get(hour)

    def is_confirmed(self, hour) -> bool:
        return hour in self._data.confirmed_hours

    async def _maybe_backfill(self) -> None:
        """Fold each settled past day into the profile exactly once."""
        if self.config.get(CONF_PRICE_SOURCE) != PRICE_SOURCE_PRADCAST:
            return
        today = dt_util.now().date()
        if self._last_backfill_day == today and self.profile.observed_days > 0:
            return

        wanted = [
            today - timedelta(days=offset)
            for offset in range(1, BACKFILL_DAYS + 1)
            if not self.profile.is_date_observed(today - timedelta(days=offset))
        ]
        if wanted:
            source = self._build_source()
            if isinstance(source, PradcastPriceSource):
                data = await source.async_fetch_days(wanted)
                for day in wanted:
                    hours = {
                        h: data.buy[h]
                        for h in data.confirmed_hours
                        if h.date() == day and h in data.buy
                    }
                    if hours:
                        for hour_start, price in hours.items():
                            self.profile.observe(hour_start, price)
                        self.profile.mark_date_observed(day)

        self._last_backfill_day = today
        await self._async_save()

    async def _maybe_backfill_archive(self) -> None:
        """Seed the archive with settled past days so estimates have history.

        Independent of the profile backfill: the archive must hold ~3 weeks of
        confirmed prices for the weighted estimate to produce a value, even when
        the learned profile was already backfilled by an earlier version (which
        would otherwise short-circuit :meth:`_maybe_backfill`).
        """
        if self.config.get(CONF_PRICE_SOURCE) != PRICE_SOURCE_PRADCAST:
            return
        today = dt_util.now().date()
        if self._last_archive_backfill_day == today:
            return

        wanted = [
            today - timedelta(days=offset)
            for offset in range(1, _ARCHIVE_BACKFILL_DAYS + 1)
            if not self._archive_has_day(today - timedelta(days=offset))
        ]
        if wanted:
            source = self._build_source()
            if isinstance(source, PradcastPriceSource):
                data = await source.async_fetch_days(wanted)
                stamp = dt_util.now().isoformat()
                dirty = False
                for hour in data.confirmed_hours:
                    price = data.buy.get(hour)
                    if price is None:
                        continue
                    if self.archive.record(
                        hour, price, PRICE_TYPE_CERTAIN, PRICE_SOURCE_PRADCAST, stamp
                    ):
                        dirty = True
                if dirty:
                    self.archive.prune()
                    await self._async_save_archive()

        self._last_archive_backfill_day = today

    def _archive_has_day(self, day: date) -> bool:
        """Whether the archive already holds a representative hour of ``day``."""
        probe = dt_util.start_of_local_day(day) + timedelta(hours=12)
        return self.archive.get(probe) is not None

    async def _ingest_into_archive(self, fetched_at: datetime) -> None:
        """Fold the latest fetch into the permanent archive (layered)."""
        source_label = (
            PRICE_SOURCE_PRADCAST
            if self.config.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_PRADCAST
            else PRICE_SOURCE_SENSOR
        )
        stamp = fetched_at.isoformat()
        dirty = False
        for hour, energy in self._data.buy.items():
            is_confirmed = hour in self._data.confirmed_hours
            price_type = PRICE_TYPE_CERTAIN if is_confirmed else PRICE_TYPE_FORECAST
            if self.archive.record(hour, energy, price_type, source_label, stamp):
                dirty = True
        if dirty:
            self.archive.prune()
            await self._async_save_archive()

    async def _async_save(self) -> None:
        if self._store is None:
            return
        await self._store.async_save(
            {
                "profile": self.profile.to_dict(),
                "last_backfill_day": self._last_backfill_day.isoformat()
                if self._last_backfill_day
                else None,
            }
        )

    async def _async_save_archive(self) -> None:
        if self._archive_store is None:
            return
        await self._archive_store.async_save(self.archive.to_dict())

    def contribute(self, forecast: Forecast) -> None:
        for slot in forecast.slots:
            hour = slot.start.replace(minute=0, second=0, microsecond=0)
            price = self._data.buy.get(hour)
            confirmed = hour in self._data.confirmed_hours
            if price is None:
                # Fall back to the permanent archive (covers cached hours between
                # source fetches), then to the estimated weekday+hour average.
                archived = self.archive.get(hour)
                if archived is not None:
                    price = archived["energy"]
                    confirmed = archived["type"] == PRICE_TYPE_CERTAIN
                    if archived["type"] == PRICE_TYPE_FORECAST:
                        slot.tags.append("price_archived")
                else:
                    # Estimated prices fill the tail (D+4..D+7) so the plan
                    # reaches a full week; prefer the 3-week weighted average,
                    # else the long-run learned profile.
                    estimate, _ = self.archive.estimate(hour)
                    if estimate is None:
                        estimate = self.profile.value(hour.weekday(), hour.hour)
                    if estimate is not None:
                        price = estimate
                        slot.tags.append("price_estimated")
            if price is not None:
                slot.buy_price = price
            slot.price_confirmed = confirmed
            if hour in self._data.levels:
                slot.tags.append(f"price:{self._data.levels[hour]}")
            if hour in self._data.confidence:
                slot.tags.append(f"confidence:{self._data.confidence[hour]}")


