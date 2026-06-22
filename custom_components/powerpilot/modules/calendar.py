"""Calendar module (placeholder for Stage 3).

Planned behaviour:
* Read Apple Calendar via CalDAV (or an HA ``calendar`` entity).
* For travel events, compute trip distance (home → event → home) and translate it
  into the EV energy needed before departure.
* All-day "away" events keep the home battery SoC in a lower band, waiting for
  better prices, and feed an absence forecast.
* Hourly events ("pranie" 3 kWh/h, "prasowanie" 2 kWh/h) become scheduled loads.

Stage 0 provides the module shell so the registry and pipeline are complete; it
contributes nothing until implemented.
"""

from __future__ import annotations

import logging

from ..models import Forecast
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)


class CalendarModule(PowerPilotModule):
    """Reads calendar events and turns them into trips and scheduled loads."""

    domain = "calendar"

    def contribute(self, forecast: Forecast) -> None:  # noqa: D102 - placeholder
        return
