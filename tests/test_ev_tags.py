"""EV charging steered by ``#<keyword>`` tags.

``#kotek_socNN`` on an event WITHOUT a location is a plain deadline: be at NN %
by the event's start. On an event WITH a location it means something more
useful — be at NN % when the car *departs* for that trip, which is earlier than
the event's start by the travel time.

A bare ``#kotek`` charges flat out for the hours the event covers.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.modules.calendar import CalendarEvent, Trip
from custom_components.powerpilot.modules.ev import EVModule

NOW = dt_util.now().replace(minute=0, second=0, microsecond=0)


def _module(keyword: str = "Kotek") -> EVModule:
    ev = EVModule.__new__(EVModule)
    ev._targets = []
    ev._trip_targets = []
    ev._forced_hours = set()
    ev._capacity = 100.0
    ev._kwh_per_km = 0.2
    ev.config = {}
    # Reserve floor comes from a number entity; pin it to 0 so the assertions
    # below are about the trip energy alone.
    ev.min_soc_entity = SimpleNamespace(native_value=0.0)
    return ev


def _event(
    summary: str, hours_ahead: int = 5, length: int = 1, location: str = ""
) -> CalendarEvent:
    return CalendarEvent(
        summary=summary,
        location=location,
        start=NOW + timedelta(hours=hours_ahead),
        end=NOW + timedelta(hours=hours_ahead + length),
        calendar="calendar.rodzina",
    )


def _trip(soc_target: float | None, km: float = 50.0) -> Trip:
    return Trip(
        label="Babcia",
        location="Kraków",
        event_start=NOW + timedelta(hours=6),
        event_end=NOW + timedelta(hours=8),
        depart=NOW + timedelta(hours=5),
        return_end=NOW + timedelta(hours=9),
        outbound_distance_km=km,
        return_distance_km=km,
        soc_target=soc_target,
        calendar="calendar.rodzina",
    )


def test_soc_tag_without_location_is_a_deadline_at_event_start() -> None:
    ev = _module()
    ev._parse_keyword_event(_event("#kotek_soc100"), "Kotek", NOW)

    assert len(ev._targets) == 1
    assert ev._targets[0].target_soc == 100.0
    assert ev._targets[0].deadline == NOW + timedelta(hours=5)


def test_bare_tag_forces_every_hour_of_the_event() -> None:
    ev = _module()
    ev._parse_keyword_event(_event("#kotek", length=3), "Kotek", NOW)

    assert ev._forced_hours == {
        NOW + timedelta(hours=5),
        NOW + timedelta(hours=6),
        NOW + timedelta(hours=7),
    }
    assert ev._targets == []


def test_soc_tag_is_not_also_a_forced_window() -> None:
    """``#kotek_soc100`` says where to end up, not to charge flat out."""
    ev = _module()
    ev._parse_keyword_event(_event("#kotek_soc100", length=3), "Kotek", NOW)

    assert ev._forced_hours == set()


def test_old_prefix_syntax_no_longer_charges() -> None:
    ev = _module()
    ev._parse_keyword_event(_event("Kotek 100%"), "Kotek", NOW)
    ev._parse_keyword_event(_event("Kotek"), "Kotek", NOW)

    assert ev._targets == []
    assert ev._forced_hours == set()


def test_located_event_is_left_to_the_trip_pass() -> None:
    """A trip's deadline is its departure, which only the trip pass knows."""
    ev = _module()
    ev._parse_keyword_event(
        _event("Babcia #kotek_soc100", location="Kraków"), "Kotek", NOW
    )

    assert ev._targets == []
    assert ev._forced_hours == set()


def test_trip_tag_targets_the_departure_not_the_event_start() -> None:
    ev = _module()
    ev._apply_chain_targets([_trip(soc_target=100.0)], NOW)

    assert len(ev._trip_targets) == 1
    target = ev._trip_targets[0]
    assert target.target_soc == 100.0
    assert target.deadline == NOW + timedelta(hours=5)  # depart, not event_start


def test_untagged_trip_keeps_the_distance_derived_floor() -> None:
    ev = _module()
    ev._apply_chain_targets([_trip(soc_target=None)], NOW)

    # 100 km round trip × 0.2 kWh/km = 20 kWh = 20 % of the pack, + min_soc.
    assert ev._trip_targets[0].target_soc == 20.0


def test_trip_tag_can_only_raise_the_floor_never_lower_it() -> None:
    """A tag below what the drive needs would strand the car mid-trip."""
    ev = _module()
    ev._apply_chain_targets([_trip(soc_target=5.0)], NOW)

    assert ev._trip_targets[0].target_soc == 20.0


def test_keyword_is_configurable() -> None:
    ev = _module()
    ev._parse_keyword_event(_event("#tesla_soc90"), "Tesla", NOW)

    assert ev._targets[0].target_soc == 90.0


def test_deadline_remembers_which_calendar_wrote_it() -> None:
    """Several calendars can carry #soc tags — the panel must say which one."""
    ev = _module()
    ev._parse_keyword_event(_event("#kotek_soc80"), "Kotek", NOW)

    assert ev._targets[0].calendar == "calendar.rodzina"


def test_trip_target_remembers_the_chain_head_calendar() -> None:
    """The deadline IS the head's departure, so it is the head's calendar."""
    ev = _module()
    ev._apply_chain_targets([_trip(soc_target=None)], NOW)

    assert ev._trip_targets[0].calendar == "calendar.rodzina"
