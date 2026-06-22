"""Scheduled-loads module.

Injects extra energy for appliances that run on a schedule but outside the learned
base profile — e.g. washing machine (~3 kWh/h), ironing (~2 kWh/h), dishwasher.

Stage 0 provides the data model and a no-op default. Stage 3 wires these to
calendar events (hourly events such as "pranie" / "prasowanie") and lets the user
declare deferrable loads with a deadline so the optimizer can place them in cheap
hours.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from ..models import Forecast
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)


@dataclass
class ScheduledLoad:
    """A fixed-time load contribution."""

    start: datetime
    kwh: float
    label: str = "load"


class LoadsModule(PowerPilotModule):
    """Provides scheduled extra loads to the forecast."""

    domain = "loads"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._loads: list[ScheduledLoad] = []

    def set_loads(self, loads: list[ScheduledLoad]) -> None:
        """Replace the active scheduled loads (used by the calendar module)."""
        self._loads = list(loads)

    def contribute(self, forecast: Forecast) -> None:
        if not self._loads:
            return
        by_hour: dict[datetime, float] = {}
        labels: dict[datetime, list[str]] = {}
        for load in self._loads:
            hour = load.start.replace(minute=0, second=0, microsecond=0)
            by_hour[hour] = by_hour.get(hour, 0.0) + load.kwh
            labels.setdefault(hour, []).append(load.label)
        for slot in forecast.slots:
            hour = slot.start.replace(minute=0, second=0, microsecond=0)
            if hour in by_hour:
                slot.extra_load_kwh += by_hour[hour]
                slot.tags.extend(labels[hour])
