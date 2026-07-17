"""Weather module.

Reads an hourly temperature forecast from a Home Assistant ``weather`` entity via
the ``weather.get_forecasts`` service (modern HA removed the state-attribute
forecast) and attaches the temperature to each slot. The climate module then
converts those temperatures into expected consumption of the configured
weather-dependent load.
"""

from __future__ import annotations

import logging

from homeassistant.exceptions import HomeAssistantError
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
        # Live outside temperature from the entity's state attributes — the
        # climate module records it hourly to learn temp → kWh.
        self.current_temperature: float | None = None

    async def async_hourly_forecast(self, entity_id: str) -> list[dict]:
        """Hourly forecast entries via ``weather.get_forecasts``.

        Returns ``[]`` (with a warning) when the entity does not support the
        hourly forecast type — no daily/twice-daily substitution: temperatures
        interpolated from a daily forecast would silently miscalibrate the
        climate profile.
        """
        try:
            resp = await self.hass.services.async_call(
                "weather",
                "get_forecasts",
                {"entity_id": entity_id, "type": "hourly"},
                blocking=True,
                return_response=True,
            )
        except HomeAssistantError as err:
            self.log_warning(
                f"Encja {entity_id} nie zwróciła prognozy godzinowej "
                f"(weather.get_forecasts type=hourly): {err}"
            )
            return []
        forecast = ((resp or {}).get(entity_id) or {}).get("forecast")
        return list(forecast) if isinstance(forecast, (list, tuple)) else []

    async def async_update(self) -> None:
        self._temps = {}
        self.current_temperature = None
        entity_id = self.config.get(CONF_WEATHER_ENTITY)
        if not entity_id:
            self.log_info("Brak skonfigurowanej encji pogody.")
            return
        state = self.hass.states.get(entity_id)
        if state is None:
            self.log_warning(f"Encja pogody {entity_id} niedostępna.")
            return
        try:
            self.current_temperature = float(state.attributes.get("temperature"))
        except (TypeError, ValueError):
            self.current_temperature = None
        for entry in await self.async_hourly_forecast(entity_id):
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

    @property
    def forecast_hours(self) -> int:
        """How many hourly temperatures the last update yielded."""
        return len(self._temps)

    def temperature_at(self, hour) -> float | None:
        return self._temps.get(hour)

    def contribute(self, forecast: Forecast) -> None:
        if not self._temps:
            return
        for slot in forecast.slots:
            hour = slot.start.replace(minute=0, second=0, microsecond=0)
            if hour in self._temps:
                slot.temperature = self._temps[hour]
