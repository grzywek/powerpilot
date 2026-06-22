"""EV module.

Computes how much energy the car needs and when it is available to charge, then
exposes a structured :class:`EVRequest` the optimizer can schedule into the
cheapest hours (respecting the phase shared with the inverter).

Stage 0 sizes the need from the EV SoC deficit to a target SoC. Stage 3 refines
the target from calendar trips and the weekly off-calendar km, and adds the
weekend "arrive with an empty EV battery" strategy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime

from ..const import (
    CONF_EV_BATTERY_KWH,
    CONF_EV_CHARGER_KW,
    CONF_EV_CHARGER_PHASE,
    CONF_EV_ENABLED,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_RANGE_KM,
    CONF_EV_SOC_SENSOR,
    CONF_EV_WEEKLY_KM,
)
from ..models import Forecast
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

DEFAULT_TARGET_SOC = 80.0
HOME_STATES = {"home", "on", "true", "connected"}


@dataclass
class EVRequest:
    """Structured EV charging need passed to the optimizer."""

    enabled: bool = False
    required_kwh: float = 0.0
    charger_kw: float = 3.5
    phase: int = 1
    available_hours: set[datetime] = field(default_factory=set)

    @property
    def is_actionable(self) -> bool:
        return self.enabled and self.required_kwh > 0 and bool(self.available_hours)


class EVModule(PowerPilotModule):
    """Provides the EV charging request and home-availability."""

    domain = "ev"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._soc: float | None = None
        self._home: bool = False
        self._request = EVRequest()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get(CONF_EV_ENABLED))

    async def async_update(self) -> None:
        if not self.enabled:
            self._request = EVRequest(enabled=False)
            return

        self._soc = self._read_float(self.config.get(CONF_EV_SOC_SENSOR))
        self._home = self._read_home(self.config.get(CONF_EV_LOCATION_SENSOR))

    def _read_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_home(self, entity_id: str | None) -> bool:
        if not entity_id:
            return True  # assume home if no tracker configured
        state = self.hass.states.get(entity_id)
        if state is None:
            return False
        return str(state.state).lower() in HOME_STATES

    def get_request(self, forecast: Forecast) -> EVRequest:
        if not self.enabled:
            return EVRequest(enabled=False)

        battery_kwh = float(self.config.get(CONF_EV_BATTERY_KWH, 60.0))
        target_soc = DEFAULT_TARGET_SOC
        current_soc = self._soc if self._soc is not None else target_soc
        required = max(0.0, (target_soc - current_soc) / 100.0 * battery_kwh)

        available = {
            slot.start.replace(minute=0, second=0, microsecond=0)
            for slot in forecast.slots
        } if self._home else set()

        self._request = EVRequest(
            enabled=True,
            required_kwh=required,
            charger_kw=float(self.config.get(CONF_EV_CHARGER_KW, 3.5)),
            phase=int(self.config.get(CONF_EV_CHARGER_PHASE, 1)),
            available_hours=available,
        )
        return self._request

    def collect_reminders(self) -> list[str]:
        if not self.enabled:
            return []
        reminders: list[str] = []
        if not self._home and self._request.required_kwh > 0:
            reminders.append("Plug in the EV when you get home — charging is needed.")
        return reminders

    @property
    def weekly_km(self) -> int:
        return int(self.config.get(CONF_EV_WEEKLY_KM, 0))

    @property
    def kwh_per_km(self) -> float:
        battery = float(self.config.get(CONF_EV_BATTERY_KWH, 60.0))
        rng = float(self.config.get(CONF_EV_RANGE_KM, 400.0)) or 1.0
        return battery / rng
