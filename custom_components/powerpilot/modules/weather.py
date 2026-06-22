"""Weather module.

Reads an hourly temperature forecast from a Home Assistant ``weather`` entity and
attaches the temperature to each slot. The climate module then converts those
temperatures into heating/cooling energy.

Stage 0 reads whatever forecast the weather entity already exposes in its state
attributes; Stage 4 calls the ``weather.get_forecasts`` service for richer data.
"""

from __future__ import annotations

import logging

from homeassistant.util import dt as dt_util

from ..const import CONF_WEATHER_ENTITY
from ..models import Forecast
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)


class WeatherModule(PowerPilotModule):
    """Provides hourly temperature to the forecast."""

    domain = "weather"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._temps: dict = {}

    async def async_update(self) -> None:
        self._temps = {}
        entity_id = self.config.get(CONF_WEATHER_ENTITY)
        if not entity_id:
            self.log_info("Brak skonfigurowanej encji pogody.")
            return
        state = self.hass.states.get(entity_id)
        if state is None:
            self.log_warning(f"Encja pogody {entity_id} niedostępna.")
            return
        forecast = state.attributes.get("forecast")
        if not isinstance(forecast, (list, tuple)):
            self.log_warning(f"Encja {entity_id} nie udostępnia atrybutu 'forecast'.")
            return
        for entry in forecast:
            if not isinstance(entry, dict):
                continue
            start = dt_util.parse_datetime(str(entry.get("datetime", "")))
            temp = entry.get("temperature")
            if start is not None and temp is not None:
                hour = dt_util.as_local(start).replace(minute=0, second=0, microsecond=0)
                try:
                    self._temps[hour] = float(temp)
                except (TypeError, ValueError):
                    continue
        if self._temps:
            self.log_info(
                f"Prognoza temperatury: {len(self._temps)} godzin (encja {entity_id}).",
                extra={"entity": entity_id, "hours": len(self._temps)},
            )

    def contribute(self, forecast: Forecast) -> None:
        if not self._temps:
            return
        for slot in forecast.slots:
            hour = slot.start.replace(minute=0, second=0, microsecond=0)
            if hour in self._temps:
                slot.temperature = self._temps[hour]
