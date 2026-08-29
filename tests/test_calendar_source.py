"""Every calendar-derived entry names the calendar it came from.

With several calendars configured, a trip or a ``#soc`` deadline shown in the
panel is untraceable without its source: you can see that *something* asked for
80 % on Friday, but not which calendar to open to change it. So the source
entity_id (and its friendly name) rides along with trips and targets, and the
configured calendars are reported even when they delivered nothing.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_CALENDARS,
    CONF_TRAVEL_MARGIN_AFTER_MIN,
    CONF_TRAVEL_MARGIN_BEFORE_MIN,
)
from custom_components.powerpilot.modules.calendar import (
    CalendarEvent,
    CalendarModule,
    Trip,
)

NOW = dt_util.now().replace(minute=0, second=0, microsecond=0)


class _State:
    def __init__(self, name: str) -> None:
        self.name = name


class _States:
    def __init__(self, names: dict[str, str]) -> None:
        self._names = names

    def get(self, entity_id: str):
        name = self._names.get(entity_id)
        return _State(name) if name else None


class _Hass:
    def __init__(self, names: dict[str, str]) -> None:
        self.states = _States(names)


def _event(summary: str, calendar: str, location: str = "") -> CalendarEvent:
    return CalendarEvent(
        summary=summary,
        location=location,
        start=NOW + timedelta(hours=4),
        end=NOW + timedelta(hours=6),
        calendar=calendar,
    )


def _module(names: dict[str, str], calendars: list[str]) -> CalendarModule:
    module = object.__new__(CalendarModule)
    module.hass = _Hass(names)
    module.config = {CONF_CALENDARS: calendars}
    module.events = []
    module.trips = []
    return module


def test_calendar_name_is_the_friendly_name() -> None:
    module = _module({"calendar.rodzina": "Rodzina"}, ["calendar.rodzina"])
    assert module.calendar_name("calendar.rodzina") == "Rodzina"


def test_calendar_name_is_none_when_ha_has_no_such_entity() -> None:
    """Renamed or deleted after being picked — say so, don't invent a name."""
    module = _module({}, ["calendar.znikniety"])
    assert module.calendar_name("calendar.znikniety") is None


def test_payload_counts_events_and_trips_per_calendar() -> None:
    module = _module(
        {"calendar.rodzina": "Rodzina", "calendar.praca": "Praca"},
        ["calendar.rodzina", "calendar.praca"],
    )
    module.events = [
        _event("Babcia", "calendar.rodzina", location="Kraków"),
        _event("#kotek_soc80", "calendar.rodzina"),
        _event("Spotkanie", "calendar.praca"),
    ]
    module.trips = [
        Trip(
            label="Babcia",
            location="Kraków",
            event_start=NOW,
            event_end=NOW + timedelta(hours=2),
            depart=NOW,
            return_end=NOW + timedelta(hours=3),
            calendar="calendar.rodzina",
        )
    ]

    rows = module.calendars_payload()

    assert [r["entity_id"] for r in rows] == ["calendar.rodzina", "calendar.praca"]
    assert rows[0] == {
        "entity_id": "calendar.rodzina",
        "name": "Rodzina",
        "available": True,
        "events": 2,
        "trips": 1,
    }
    assert rows[1]["events"] == 1 and rows[1]["trips"] == 0


def test_a_configured_but_missing_calendar_is_reported_not_hidden() -> None:
    """"Selected but broken" must not look the same as "never selected"."""
    module = _module({}, ["calendar.znikniety"])

    row = module.calendars_payload()[0]

    assert row["available"] is False
    assert row["name"] is None
    assert row["events"] == 0


def test_no_calendars_configured_yields_no_rows() -> None:
    assert _module({}, []).calendars_payload() == []


async def test_trip_carries_the_calendar_of_the_event_it_came_from() -> None:
    module = _module({"calendar.rodzina": "Rodzina"}, ["calendar.rodzina"])
    module.config = {**module.config, CONF_TRAVEL_MARGIN_BEFORE_MIN: 0.0,
                     CONF_TRAVEL_MARGIN_AFTER_MIN: 0.0}
    module.events = [_event("Babcia", "calendar.rodzina", location="Kraków")]
    module._resolver = None
    module.log_warning = lambda *a, **kw: None

    await module._build_trips()

    assert [t.calendar for t in module.trips] == ["calendar.rodzina"]
