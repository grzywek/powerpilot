"""Forecast assembly.

Builds the hourly horizon and runs every module's ``contribute`` pass, then trims
the horizon to where price data actually reaches — the forecast is "as far as the
price information in the system allows".
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .const import PLANNING_HORIZON_HOURS
from .models import Forecast, HourSlot
from .modules.base import ModuleRegistry

_LOGGER = logging.getLogger(__name__)

# Upper bound on how far we ever look ahead (7 days of hourly slots). Energy
# prices beyond the source's forecast horizon (Pradcast: D+1..D+3) are filled
# with the estimated weekday+hour average, so the plan reaches the full week.
MAX_HORIZON_HOURS = PLANNING_HORIZON_HOURS


class ForecastBuilder:
    """Constructs a :class:`Forecast` from the active module registry."""

    def __init__(self, registry: ModuleRegistry, max_hours: int = MAX_HORIZON_HOURS) -> None:
        self._registry = registry
        self._max_hours = max_hours

    def build(self) -> Forecast:
        start = dt_util.now().replace(minute=0, second=0, microsecond=0)
        slots = [HourSlot(start=start + timedelta(hours=i)) for i in range(self._max_hours)]
        forecast = Forecast(slots=slots)

        self._registry.contribute_all(forecast)

        return self._trim_to_prices(forecast)

    def build_tail(self, start: datetime, end: datetime) -> Forecast:
        """Accounting-only slots for ``[start, end)``, every module contributing.

        Used for the part of a billing month that lies past the planning
        horizon: the monthly price cap depends on those hours too. Unlike
        :meth:`build` the result is NOT trimmed — an unpriced tail hour is a
        gap the caller must report, not silently drop.
        """
        hours = max(0, int((end - start).total_seconds() // 3600))
        slots = [HourSlot(start=start + timedelta(hours=i)) for i in range(hours)]
        forecast = Forecast(slots=slots)
        if slots:
            self._registry.contribute_all(forecast)
        return forecast

    @staticmethod
    def _trim_to_prices(forecast: Forecast) -> Forecast:
        """Drop trailing slots that have no buy price."""
        slots = forecast.slots
        last_priced = -1
        for index, slot in enumerate(slots):
            if slot.buy_price is not None:
                last_priced = index
        if last_priced < 0:
            # No prices at all yet — keep a short 24h horizon so the integration
            # still produces a (price-less) plan instead of nothing.
            forecast.slots = slots[:24]
        else:
            forecast.slots = slots[: last_priced + 1]
        return forecast
