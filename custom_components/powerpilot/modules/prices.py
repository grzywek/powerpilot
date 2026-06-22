"""Price module.

Delegates price retrieval to a pluggable :class:`PriceSource` (HA sensors or the
prądcast.pl API) and contributes hourly buy/sell prices to the forecast, marking
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

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import CONF_PRICE_SOURCE, DOMAIN, PRICE_SOURCE_PRADCAST
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
BACKFILL_DAYS = 7  # how many past days to seed on first run


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


class PriceModule(PowerPilotModule):
    """Provides hourly buy/sell prices to the forecast."""

    domain = "prices"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._data = PriceData()
        self.profile = PriceProfile()
        self._store: Store | None = None
        self._last_backfill_day: date | None = None

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
        await self._maybe_backfill()

    async def async_update(self) -> None:
        await self._maybe_backfill()
        source = self._build_source()
        self._data = await source.async_fetch()

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

    def contribute(self, forecast: Forecast) -> None:
        for slot in forecast.slots:
            hour = slot.start.replace(minute=0, second=0, microsecond=0)
            price = self._data.buy.get(hour)
            if price is None:
                # Interior gap fallback to the learned profile (does not extend
                # the horizon past the last real price — trailing gaps stay None).
                profile_price = self.profile.value(hour.weekday(), hour.hour)
                if profile_price is not None:
                    price = profile_price
                    slot.tags.append("price_profile")
            if price is not None:
                slot.buy_price = price
            if hour in self._data.sell:
                slot.sell_price = self._data.sell[hour]
            slot.price_confirmed = hour in self._data.confirmed_hours
            if hour in self._data.levels:
                slot.tags.append(f"price:{self._data.levels[hour]}")
            if hour in self._data.confidence:
                slot.tags.append(f"confidence:{self._data.confidence[hour]}")


