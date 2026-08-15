"""The EV control surface has to say when to STOP, not just when to start.

``ev_charge_minutes`` is a duration inside one clock hour, so an automation
needs an anchor for it. For a future hour that anchor is the top of the hour.
For the hour already running it is not: the allocator sizes that hour against
the minutes it has LEFT, so "3 minutes" published at :57 means the last three
minutes of the hour, not the first three — which have long since passed.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.coordinator import PowerPilotCoordinator
from custom_components.powerpilot.models import Decision, Forecast, Plan

HOUR = dt_util.now().replace(minute=0, second=0, microsecond=0)


class _EV:
    """Just the surface ``ev_control`` reads off the EV module."""

    enabled = True
    soc = 74.0

    def soc_limit_now(self) -> float:
        return 95.0


class _Coordinator:
    """``ev_control`` only touches the plan, the EV module and the clock."""

    def __init__(self, plan: Plan) -> None:
        self.data = plan
        self.ev = _EV()

    def current_decision(self, plan=None, moment=None):
        return (plan or self.data).decision_at(moment or dt_util.now())

    def control(self) -> dict:
        return PowerPilotCoordinator.ev_control(self)


def _plan(charging: dict[int, int], created_at) -> Plan:
    """``charging`` maps hour offset → planned charging minutes."""
    decisions = []
    for offset in range(6):
        decision = Decision(start=HOUR + timedelta(hours=offset))
        if offset in charging:
            decision.ev_charge = True
            decision.ev_charge_kwh = 1.0
            decision.ev_charge_minutes = charging[offset]
        decisions.append(decision)
    return Plan(forecast=Forecast(slots=[]), decisions=decisions, created_at=created_at)


def test_running_hour_window_starts_where_the_plan_was_made() -> None:
    """Three minutes planned at :57 are the hour's LAST three."""
    made_at = HOUR + timedelta(minutes=57)
    control = _Coordinator(_plan({0: 3}, made_at)).control()

    assert control["charge_start"] == made_at.isoformat()
    assert control["charge_until"] == (HOUR + timedelta(hours=1)).isoformat()


def test_running_hour_window_never_spills_into_the_next_hour() -> None:
    """A stale budget is clipped by the hour, not carried across it."""
    made_at = HOUR + timedelta(minutes=50)
    control = _Coordinator(_plan({0: 30}, made_at)).control()

    assert control["charge_until"] == (HOUR + timedelta(hours=1)).isoformat()


def test_future_hour_window_starts_at_the_top_of_the_hour() -> None:
    """Hours the car has not reached yet are planned whole."""
    control = _Coordinator(_plan({2: 20}, HOUR + timedelta(minutes=5))).control()

    assert control["charge_start"] == (HOUR + timedelta(hours=2)).isoformat()
    assert control["charge_until"] == (
        HOUR + timedelta(hours=2, minutes=20)
    ).isoformat()


def test_no_planned_charging_has_no_window() -> None:
    control = _Coordinator(_plan({}, HOUR)).control()

    assert control["charge_start"] is None
    assert control["charge_until"] is None
    assert control["charging_now"] is False
