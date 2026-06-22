"""PowerPilot modules package."""

from __future__ import annotations

from .base import ModuleRegistry, PowerPilotModule
from .calendar import CalendarModule
from .climate import ClimateModule
from .consumption import ConsumptionModule
from .ev import EVModule, EVRequest
from .loads import LoadsModule, ScheduledLoad
from .prices import PriceModule
from .weather import WeatherModule

__all__ = [
    "ModuleRegistry",
    "PowerPilotModule",
    "CalendarModule",
    "ClimateModule",
    "ConsumptionModule",
    "EVModule",
    "EVRequest",
    "LoadsModule",
    "ScheduledLoad",
    "PriceModule",
    "WeatherModule",
]
