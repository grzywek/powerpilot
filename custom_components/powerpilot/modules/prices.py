"""Price module.

Delegates price retrieval to a pluggable :class:`PriceSource` (HA sensors or the
prądcast.pl API) and contributes hourly buy prices to the forecast, marking
each hour as confirmed or forecast.

For hours the source no longer covers it falls back to the permanent
:class:`PriceArchive`, and beyond that to a weighted **estimate** (same
weekday+hour averaged over the last 1/2/3 weeks of confirmed prices). The
archive is seeded on first run with the last three weeks so estimates have
history immediately.
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

# How many past days of confirmed prices to seed into the archive. Must cover the
# 3-week estimate lookback (+ a buffer) so estimated prices have samples.
_ARCHIVE_BACKFILL_DAYS = 22
_ARCHIVE_RETENTION_DAYS = 90  # how long fetched prices stay in the archive


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
        self.archive = PriceArchive()
        self._archive_store: Store | None = None
        self._last_archive_backfill_day: date | None = None
        self._last_source_fetch: datetime | None = None

    def _build_source(self) -> PriceSource:
        if self.config.get(CONF_PRICE_SOURCE) == PRICE_SOURCE_PRADCAST:
            return PradcastPriceSource(self.hass, self.config)
        return SensorPriceSource(self.hass, self.config)

    async def async_setup(self) -> None:
        self._archive_store = Store(
            self.hass,
            STORAGE_VERSION_PRICE_ARCHIVE,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_price_archive",
        )
        archived = await self._archive_store.async_load()
        self.archive = PriceArchive.from_dict(archived)
        self.archive.prune()

        await self._maybe_backfill_archive()

    def _refresh_interval(self) -> timedelta:
        hours = float(self.config.get(CONF_PRICE_REFRESH_HOURS, 3) or 3)
        return timedelta(hours=max(hours, 0.0))

    async def async_update(self) -> None:
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
            f"Archiwum: {len(self.archive)} godzin.",
            extra={
                "source": source_label,
                "confirmed_hours": confirmed_count,
                "forecast_hours": forecast_count,
                "last_priced_hour": last_hour.isoformat() if last_hour else None,
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
        # The live fetch window (``_data.buy``) only covers today→D+N; past hours
        # roll out of it on the next fetch. The permanent archive retains them
        # (same values, see ``_ingest_into_archive``), so fall back to it — this
        # is what makes historical prices show on the chart. Mirrors
        # ``contribute``'s archive fallback.
        price = self._data.buy.get(hour)
        if price is None:
            archived = self.archive.get(hour)
            if archived is not None:
                return archived["energy"]
        return price

    def is_confirmed(self, hour) -> bool:
        if hour in self._data.confirmed_hours:
            return True
        archived = self.archive.get(hour)
        return archived is not None and archived["type"] == PRICE_TYPE_CERTAIN

    async def _maybe_backfill_archive(self) -> None:
        """Seed the archive with settled past days so estimates have history.

        The archive must hold ~3 weeks of confirmed prices for the weighted
        estimate to produce a value, so on first run (and once per day) any
        missing past day within the lookback window is fetched and folded in.
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

    async def _async_save_archive(self) -> None:
        if self._archive_store is None:
            return
        await self._archive_store.async_save(self.archive.to_dict())

    async def async_clear_data(self) -> None:
        """Drop the price archive and in-memory fetch caches."""
        if self._archive_store is not None:
            await self._archive_store.async_remove()
        self._data = PriceData()
        self.archive = PriceArchive()
        self._last_archive_backfill_day = None
        self._last_source_fetch = None

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
                    # reaches a full week: a 3-week weighted weekday+hour average.
                    estimate, _ = self.archive.estimate(hour)
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


