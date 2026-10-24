"""The rules of missed plan actions (``misses.py``, ADR 002).

The plan is recomputed all the time, so an action it asked for and nobody took
vanishes with the next plan. These pin what counts as a miss, what it cost, and
that the record of it is never lost or rewritten.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.powerpilot.misses import (
    KIND_ESS,
    KIND_EV_UNPLUGGED,
    NOTE_NO_EV_STATS,
    NOTE_NOT_COSTED,
    Ask,
    Counterfactual,
    MissLedger,
    PlanAsks,
    Settlement,
    ask_from_plan,
    counterfactual,
    ess_record,
    ev_left_unplugged,
    ev_off_plan,
    ev_unplugged_record,
    followed_transition_at,
    miss_id,
    settle_ev_unplugged,
    settle_supply,
    supply_not_followed,
    supply_record,
    switch_moment,
)
from custom_components.powerpilot.models import Decision, Forecast, HourSlot, Plan

H = datetime(2026, 10, 24, 13, 0, tzinfo=timezone.utc)


def _ask(hour: datetime = H, **fields) -> Ask:
    values = {
        "at": hour - timedelta(minutes=20),
        "ev_kwh": 5.5,
        "ev_minutes": 60,
        "supply": None,
        "mode": "passthrough",
        "charge_power_kw": 0.0,
        "price_kwh": 0.45,
        **fields,
    }
    return Ask(hour=hour, **values)


# ------------------------------------------------------------------ the ask


def test_the_hour_begins_with_the_last_ask_made_for_it() -> None:
    asks = PlanAsks()
    asks.offer(_ask(ev_kwh=1.0))
    asks.offer(_ask(ev_kwh=5.5))  # a later plan before the boundary
    assert asks.begin(H).ev_kwh == 5.5
    # A re-plan inside the hour offers the NEXT hour; this hour's ask stays.
    asks.offer(_ask(H + timedelta(hours=1), ev_kwh=0.0))
    assert asks.begin(H).ev_kwh == 5.5
    assert asks.begin(H + timedelta(hours=1)).ev_kwh == 0.0


def test_an_ask_for_another_hour_is_no_ask() -> None:
    """Home Assistant down across the boundary: the offer was for an hour that
    has passed, and an older plan's view is not borrowed."""
    asks = PlanAsks(upcoming=_ask(H - timedelta(hours=1)))
    assert asks.begin(H) is None


def test_asks_survive_a_restart() -> None:
    asks = PlanAsks(current=_ask(), upcoming=_ask(H + timedelta(hours=1), supply="meter_2"))
    restored = PlanAsks.from_dict(asks.to_dict())
    assert restored.current == asks.current
    assert restored.upcoming == asks.upcoming


def test_an_unreadable_ask_is_an_error_not_a_silent_gap() -> None:
    with pytest.raises(ValueError):
        PlanAsks.from_dict({"current": {"hour": "noon"}})


def _plan(*, supply_switching: bool) -> Plan:
    slots = [
        HourSlot(start=H + timedelta(hours=i), buy_price=0.3, distribution_price_kwh=0.15)
        for i in range(2)
    ]
    decisions = [
        Decision(start=slots[0].start, inverter_mode="passthrough", supply="victron"),
        Decision(
            start=slots[1].start,
            inverter_mode="charge",
            charge_power_kw=3.0,
            ev_charge=True,
            ev_charge_kwh=5.5,
            ev_charge_minutes=60,
            supply="meter_2",
        ),
    ]
    return Plan(
        forecast=Forecast(slots=slots), decisions=decisions, supply_switching=supply_switching
    )


def test_the_ask_is_the_plans_row_for_that_hour() -> None:
    ask = ask_from_plan(_plan(supply_switching=True), H + timedelta(hours=1), H)
    assert (ask.ev_kwh, ask.ev_minutes, ask.supply, ask.mode) == (5.5, 60, "meter_2", "charge")
    assert ask.price_kwh == pytest.approx(0.45)
    assert ask_from_plan(_plan(supply_switching=True), H + timedelta(hours=5), H) is None


def test_a_plan_not_free_to_switch_asks_nothing_of_the_apartment() -> None:
    ask = ask_from_plan(_plan(supply_switching=False), H + timedelta(hours=1), H)
    assert ask.supply is None


# ------------------------------------------------------------------ a miss


def test_the_car_left_unplugged_at_home_is_a_miss() -> None:
    assert ev_left_unplugged(_ask(), plugged=False, home=True, calendar_available=True)


@pytest.mark.parametrize(
    ("ask", "plugged", "home", "available"),
    [
        (_ask(ev_kwh=0.04), False, True, True),  # nothing asked
        (_ask(), True, True, True),  # on the cable
        (_ask(), None, True, True),  # plug unknown
        (_ask(), False, None, True),  # whereabouts unknown
        (_ask(), False, False, True),  # away: nobody could plug it in
        (_ask(), False, True, False),  # the calendar has it away
    ],
)
def test_no_miss_unless_every_reading_says_the_cable_could_have_gone_in(
    ask, plugged, home, available
) -> None:
    assert not ev_left_unplugged(ask, plugged=plugged, home=home, calendar_available=available)


def test_the_apartment_on_the_other_meter_is_a_miss() -> None:
    assert supply_not_followed(_ask(supply="meter_2"), "victron")
    assert not supply_not_followed(_ask(supply="meter_2"), "meter_2")
    assert not supply_not_followed(_ask(supply=None), "victron")  # nothing asked
    assert not supply_not_followed(_ask(supply="meter_2"), None)  # actual unknown


def _transition(state: str, at: datetime):
    return SimpleNamespace(state=state, at=at)


def test_not_switched_when_asked_counts_as_switched_at_the_hours_start() -> None:
    log = [_transition("victron", H - timedelta(days=3))]
    assert followed_transition_at(log, "meter_2", H) == H


def test_switched_away_against_the_ask_counts_as_never_switched() -> None:
    before = H - timedelta(days=3)
    log = [_transition("victron", before), _transition("meter_2", H + timedelta(minutes=20))]
    assert followed_transition_at(log, "victron", H) == before


def test_the_switch_moment_is_the_first_logged_switch_to_the_asked_meter() -> None:
    found = H + timedelta(minutes=1)
    log = [
        _transition("meter_2", H - timedelta(days=1)),
        _transition("victron", H + timedelta(minutes=12)),
        _transition("meter_2", H + timedelta(minutes=40)),
    ]
    assert switch_moment(log, "victron", found, H + timedelta(hours=1)) == H + timedelta(
        minutes=12
    )
    assert switch_moment(log, "victron", H + timedelta(minutes=13), H + timedelta(hours=1)) is None


def _off_plan(**overrides) -> bool:
    values = {
        "realized_kwh": 3.0,
        "planned_kwh": 0.0,
        "planned_before_kwh": 0.0,
        "watched": True,
        "away": False,
        "spill_kwh": 1.8,
    }
    return ev_off_plan(**{**values, **overrides})


def test_charging_no_plan_asked_for_is_off_plan() -> None:
    assert _off_plan()


def test_off_plan_needs_a_plan_to_contradict() -> None:
    assert not _off_plan(planned_kwh=None)  # no vintage
    assert not _off_plan(watched=False)  # older than revision tracking
    assert not _off_plan(planned_kwh=2.0)  # a plan of the hour asked for it
    assert not _off_plan(away=True)  # charged on a trip
    assert not _off_plan(realized_kwh=None)  # not measured


def test_a_session_overrunning_a_planned_hour_is_not_off_plan() -> None:
    assert not _off_plan(realized_kwh=1.5, planned_before_kwh=5.0)
    assert _off_plan(realized_kwh=1.9, planned_before_kwh=5.0)
    assert _off_plan(realized_kwh=1.5, planned_before_kwh=0.0)


# ------------------------------------------------------------------ the cost


def _record_with_cf(record: dict, delta: float | None, ev_kwh: float | None = None, error=None):
    record["cf"] = Counterfactual(delta, ev_kwh, error).to_dict()
    return record


def test_the_cost_is_reality_minus_the_plan_followed() -> None:
    reality = SimpleNamespace(objective_pln=12.40)
    followed = SimpleNamespace(
        objective_pln=11.85,
        decision_at=lambda hour: SimpleNamespace(ev_charge_kwh=5.2),
    )
    cf = counterfactual(reality, followed, H)
    assert cf.delta_pln == pytest.approx(0.55)
    assert cf.ev_kwh == 5.2


def test_a_car_never_plugged_in_costs_the_whole_difference() -> None:
    record = _record_with_cf(ev_unplugged_record(_ask(), H), 0.55, ev_kwh=5.5)
    settled = settle_ev_unplugged(record, delivered_after_kwh=0.0, delivered_hour_kwh=0.0)
    assert settled.cost_pln == 0.55
    assert settled.actual["ev_kwh"] == 0.0


def test_plugging_in_later_in_the_hour_scales_the_cost() -> None:
    record = _record_with_cf(ev_unplugged_record(_ask(), H), 0.60, ev_kwh=5.5)
    settled = settle_ev_unplugged(record, delivered_after_kwh=3.85, delivered_hour_kwh=3.85)
    assert settled.cost_pln == pytest.approx(0.18)  # 30 % of the energy stayed missed


def test_a_counterfactual_that_charges_nothing_keeps_its_cost() -> None:
    record = _record_with_cf(ev_unplugged_record(_ask(), H), 0.12, ev_kwh=0.0)
    assert settle_ev_unplugged(record, 0.0, 0.0).cost_pln == 0.12


def test_a_negative_cost_is_kept() -> None:
    """By the time the hour came, following the plan would have cost more."""
    record = _record_with_cf(ev_unplugged_record(_ask(), H), -0.20, ev_kwh=5.5)
    assert settle_ev_unplugged(record, 0.0, 0.0).cost_pln == -0.20


def test_no_cost_without_its_basis_and_the_note_says_why() -> None:
    assert settle_ev_unplugged(ev_unplugged_record(_ask(), H), 0.0, 0.0) == Settlement(
        None, NOTE_NOT_COSTED, {"plugged": False, "home": True, "ev_kwh": 0.0}
    )
    failed = _record_with_cf(ev_unplugged_record(_ask(), H), None, error="HiGHS: infeasible")
    assert "HiGHS: infeasible" in settle_ev_unplugged(failed, 0.0, 0.0).note
    unread = _record_with_cf(ev_unplugged_record(_ask(), H), 0.5, ev_kwh=5.5)
    assert settle_ev_unplugged(unread, None, None).note == NOTE_NO_EV_STATS


def test_a_late_switch_costs_the_minutes_on_the_wrong_meter() -> None:
    found = H + timedelta(minutes=0)
    record = _record_with_cf(supply_record(_ask(supply="meter_2"), found, "victron"), 1.00)
    assert settle_supply(record, H + timedelta(minutes=15)).cost_pln == 0.25
    assert settle_supply(record, None).cost_pln == 1.00
    assert settle_supply(record, None).actual == {"supply": "victron", "switched_at": None}


# ------------------------------------------------------------------ the ledger


def test_a_record_is_added_once_and_never_overwritten() -> None:
    ledger = MissLedger()
    first = ev_unplugged_record(_ask(), H)
    assert ledger.add(first)
    assert not ledger.add(ev_unplugged_record(_ask(ev_kwh=9.0), H + timedelta(minutes=30)))
    assert ledger.get(miss_id(KIND_EV_UNPLUGGED, H))["asked"]["ev_kwh"] == 5.5


def test_a_record_is_settled_once() -> None:
    ledger = MissLedger()
    record = ev_unplugged_record(_ask(), H)
    ledger.add(record)
    assert ledger.set_counterfactual(record["id"], Counterfactual(0.5, 5.5))
    assert not ledger.set_counterfactual(record["id"], Counterfactual(9.9, 5.5))
    later = H + timedelta(hours=2)
    assert ledger.settle(record["id"], Settlement(0.5, None, {"ev_kwh": 0.0}), later)
    assert not ledger.settle(record["id"], Settlement(7.0, "x", {}), later)
    assert not ledger.touch(record["id"], ev_kwh=3.0)
    assert ledger.get(record["id"])["cost_pln"] == 0.5
    assert ledger.open_records() == []


def test_the_ledger_survives_a_restart_whole() -> None:
    ledger = MissLedger(off_plan_through=H)
    ledger.add(ev_unplugged_record(_ask(), H))
    ledger.add(ess_record(H + timedelta(minutes=7), "charge", 3.0, "ESS oddał 0.30 kWh"))
    restored = MissLedger.from_dict(ledger.to_dict())
    assert restored.records() == ledger.records()
    assert restored.off_plan_through == H
    assert restored.get(miss_id(KIND_ESS, H + timedelta(minutes=7)))["kind"] == KIND_ESS


@pytest.mark.parametrize(
    "stored",
    [
        "not a ledger",
        {"records": {}},
        {"records": [{"id": "x"}]},
        {"records": [{**ev_unplugged_record(_ask(), H), "kind": "mystery"}]},
        {"records": [{**ev_unplugged_record(_ask(), H), "hour": "noon"}]},
    ],
)
def test_an_unreadable_ledger_is_refused_whole(stored) -> None:
    """A record dropped quietly on load would be gone at the next save."""
    with pytest.raises(ValueError):
        MissLedger.from_dict(stored)


def test_the_ask_finds_the_repeated_hour_when_the_clocks_go_back() -> None:
    """25.10.2026 has two 02:00s in Warsaw; the plan's next hour after the first
    is the second, which wall-clock arithmetic would skip."""
    from zoneinfo import ZoneInfo

    warsaw = ZoneInfo("Europe/Warsaw")
    first = datetime(2026, 10, 25, 0, 0, tzinfo=timezone.utc)  # 02:00 CEST
    second = first + timedelta(hours=1)  # 02:00 CET
    slots = [
        HourSlot(start=(first + timedelta(hours=i)).astimezone(warsaw), buy_price=0.3)
        for i in range(3)
    ]
    plan = Plan(
        forecast=Forecast(slots=slots),
        decisions=[Decision(start=slot.start, inverter_mode="passthrough") for slot in slots],
    )
    ask = ask_from_plan(plan, second, first)
    assert ask is not None and ask.hour == second
    assert ask.hour.astimezone(warsaw).hour == 2
    # Begun from either local 02:00, each hour gets its own ask.
    asks = PlanAsks(current=ask_from_plan(plan, first, first), upcoming=ask)
    assert asks.begin(slots[1].start) is ask
