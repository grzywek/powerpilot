"""Climate module.

Converts the per-hour temperature (from the weather module) into heating/cooling
energy and adds it to the forecast as extra load.

Stage 0 ships a simple degree-hour model with user-tunable parameters; Stage 4
calibrates the coefficients from measured daily energy vs outside temperature for
each configured heating/cooling source.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..models import Forecast
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)


@dataclass
class ClimateModel:
    """Linear degree-hour model around a comfort set-point."""

    enabled: bool = False
    heating_balance_c: float = 16.0  # below this, heating kicks in
    cooling_balance_c: float = 24.0  # above this, cooling kicks in
    heating_kwh_per_degree_hour: float = 0.0
    cooling_kwh_per_degree_hour: float = 0.0

    def energy_for(self, temperature: float | None) -> float:
        if not self.enabled or temperature is None:
            return 0.0
        if temperature < self.heating_balance_c:
            return (self.heating_balance_c - temperature) * self.heating_kwh_per_degree_hour
        if temperature > self.cooling_balance_c:
            return (temperature - self.cooling_balance_c) * self.cooling_kwh_per_degree_hour
        return 0.0


class ClimateModule(PowerPilotModule):
    """Adds heating/cooling energy to the forecast based on temperature."""

    domain = "climate"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self.model = ClimateModel()

    def contribute(self, forecast: Forecast) -> None:
        if not self.model.enabled:
            self.log_info("Model klimatu wyłączony.")
            return
        total = 0.0
        hits = 0
        for slot in forecast.slots:
            energy = self.model.energy_for(slot.temperature)
            if energy > 0:
                slot.extra_load_kwh += energy
                slot.tags.append("climate")
                total += energy
                hits += 1
        self.log_info(
            f"Klimat dorzucił {total:.1f} kWh na {hits}h horyzontu.",
            extra={"hours": hits, "total_kwh": round(total, 2)},
        )
