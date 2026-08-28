"""The calendar is read over the whole plan horizon.

The plan reaches 7 days (prices past the source's own D+1..D+3 are estimated),
so the calendar must be read that far too. A shorter read fails silently — the
events at the far end never arrive and the plan is built as if the calendar were
empty there, which is exactly how a trip on day 5 goes missing.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import CONF_CALENDARS
from custom_components.powerpilot.forecast import MAX_HORIZON_HOURS
from custom_components.powerpilot.modules.calendar import (
    CALENDAR_LOOKAHEAD_HOURS,
    CalendarModule,
)


class _RecordingServices:
    """Stands in for ``hass.services``, capturing the get_events payload."""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def async_call(
        self, domain, service, data, blocking=False, return_response=False
    ):
        self.calls.append(data)
        return {data["entity_id"]: {"events": []}}


class _Hass:
    def __init__(self) -> None:
        self.services = _RecordingServices()


def _module(hass: _Hass) -> CalendarModule:
    module = object.__new__(CalendarModule)
    module.hass = hass
    module.config = {CONF_CALENDARS: ["calendar.test"]}
    module.events = []
    module.trips = []
    module.ess_targets = []
    module.ess_forced_hours = set()
    module._resolver = None
    module.log_info = lambda *a, **kw: None
    module.log_warning = lambda *a, **kw: None
    return module


def test_lookahead_matches_the_plan_horizon() -> None:
    assert CALENDAR_LOOKAHEAD_HOURS == MAX_HORIZON_HOURS


async def test_fetch_window_reaches_the_end_of_the_horizon() -> None:
    """An event on day 5 is inside the plan, so it must be inside the read."""
    hass = _Hass()

    await _module(hass).async_update()

    assert len(hass.services.calls) == 1
    payload = hass.services.calls[0]
    start = dt_util.parse_datetime(payload["start_date_time"])
    end = dt_util.parse_datetime(payload["end_date_time"])
    assert end - start >= timedelta(hours=MAX_HORIZON_HOURS)
    assert end - start >= timedelta(days=5)  # the day the trip was missing
