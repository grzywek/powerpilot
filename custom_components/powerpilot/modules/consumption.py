"""Consumption module.

Maintains an hourly household consumption profile keyed by ``(weekday, hour)`` and
uses it to forecast base demand for every slot in the horizon.

Stage 0 ships a sensible default daily shape so the optimizer has realistic input
immediately. Stage 2 (see ROADMAP) replaces the seed with a rolling profile
learned from the configured consumption sensor via the recorder, with known loads
(EV / scheduled appliances) subtracted so the base stays clean.
"""

from __future__ import annotations

import logging

from ..const import CONF_CONSUMPTION_SENSOR
from ..models import Forecast
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

# Relative weighting of a typical residential day (sums are normalised later).
_DEFAULT_SHAPE = [
    0.5, 0.4, 0.4, 0.4, 0.4, 0.5,  # 00-05 night
    0.7, 1.0, 1.1, 0.9, 0.8, 0.8,  # 06-11 morning
    0.8, 0.8, 0.7, 0.7, 0.8, 1.0,  # 12-17 afternoon
    1.4, 1.5, 1.4, 1.1, 0.8, 0.6,  # 18-23 evening peak
]


class ConsumptionModule(PowerPilotModule):
    """Provides base (non-EV, non-scheduled) consumption per hour."""

    domain = "consumption"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        # profile[(weekday, hour)] = kWh. weekday: 0=Mon .. 6=Sun.
        self._profile: dict[tuple[int, int], float] = {}
        self._daily_kwh_estimate = 12.0

    async def async_setup(self) -> None:
        self._seed_default_profile()

    def _seed_default_profile(self) -> None:
        total_shape = sum(_DEFAULT_SHAPE)
        for weekday in range(7):
            # Weekends run a little higher during the day.
            weekend_factor = 1.1 if weekday >= 5 else 1.0
            daily = self._daily_kwh_estimate * weekend_factor
            for hour, weight in enumerate(_DEFAULT_SHAPE):
                self._profile[(weekday, hour)] = daily * weight / total_shape

    def profile_value(self, weekday: int, hour: int) -> float:
        return self._profile.get((weekday, hour), self._daily_kwh_estimate / 24.0)

    def contribute(self, forecast: Forecast) -> None:
        for slot in forecast.slots:
            value = self.profile_value(slot.start.weekday(), slot.start.hour)
            slot.base_consumption_kwh += value

    @property
    def has_sensor(self) -> bool:
        return bool(self.config.get(CONF_CONSUMPTION_SENSOR))
