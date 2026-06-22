"""Forecast assembly.

Builds the hourly horizon and runs every module's ``contribute`` pass, then trims
the horizon to where price data actually reaches — the forecast is "as far as the
price information in the system allows".
"""

from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.util import dt as dt_util

from .models import Forecast, HourSlot
from .modules.base import ModuleRegistry

_LOGGER = logging.getLogger(__name__)

# Upper bound on how far we ever look ahead (4 days of hourly slots).
MAX_HORIZON_HOURS = 96


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
