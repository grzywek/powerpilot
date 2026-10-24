"""Apartment switch recommendations (stage 3, slice 3).

Cycles are runs of planned meter-2 hours; each proposed one is valued by
re-solving the same inputs with its hours held on the Victron (thresholds
excluded) and dropped when it saves less than the threshold; the running one
is valued against returning at the next allowed boundary. The recommendation
is the planned supply of the next hour. Stage 3 plan, "Slice 3 design
(binding)".
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    PPE_1,
    SUPPLY_METER_2,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.models import Forecast, SupplyPlanning
from custom_components.powerpilot.when import moment_label
from custom_components.powerpilot.supply_cycles import (
    REASON_NOT_ACTIVE,
    plan_cycles,
    recommendation,
    settle_cycles,
    switch_availability,
)

from tests.test_optimizer_supply import (
    H0,
    LIMIT,
    _battery,
    _on_meter_2,
    _optimizer,
    _period,
    _planning,
    _slots,
)


def _utc_id(moment) -> str:
    return dt_util.as_utc(moment).isoformat()


def _solver(forecast, battery_soc, billing, **optimizer):
    """``solve(planning)`` over fixed inputs, as the coordinator builds it."""
    opt = _optimizer(**optimizer)
    return lambda planning: opt.optimize(
        forecast, _battery(soc=battery_soc), billing=billing, supply=planning
    )


def _weak():
    """Two 2.00 hours the battery covers only one of (see test_optimizer_supply)."""
    forecast = Forecast(
        slots=_slots([0.3, 2.0, 2.0], base=[0.0, 1.0, 1.0], apartment=[0.0, 2.0, 2.0])
    )
    return _solver(forecast, 30.0, [_period(PPE_1, 100.0, 30.0)])


def _two_hours():
    """No battery; the apartment's hours 1 and 2 cost 2.00, hour 3 is cheap."""
    forecast = Forecast(
        slots=_slots([0.3, 2.0, 2.0, 0.3], base=[0.0] * 4, apartment=[0.0, 2.0, 2.0, 1.0])
    )
    return _solver(
        forecast, 0.0, [_period(PPE_1, 100.0, 30.0)], inverter_max_discharge_kw=0.0
    )


# ---------------------------------------------------------------- cycles


def test_a_planned_cycle_has_its_start_as_id_return_hours_and_kwh() -> None:
    solve = _two_hours()
    planning = _planning(cycle_threshold_pln=0.5)
    plan = settle_cycles(solve, planning)

    assert _on_meter_2(plan) == [0, 1, 1, 0]
    [cycle] = plan.supply_cycles
    assert cycle.id == _utc_id(H0 + timedelta(hours=1))
    assert cycle.start == H0 + timedelta(hours=1)
    assert cycle.planned_return == H0 + timedelta(hours=3)
    assert cycle.hours == 2.0
    assert abs(cycle.apartment_kwh - 4.0) < 1e-9
    assert cycle.running is False
    assert cycle.victron_hours == {H0 + timedelta(hours=1), H0 + timedelta(hours=2)}
    # 4 kWh at 2.00 on PPE 1 against 4 × 0.61 capped on PPE 2.
    assert abs(cycle.saving_pln - (8.0 - 4 * LIMIT)) < 1e-6
    assert plan.supply_resolves == 1
    assert plan.supply_dropped == []


def test_a_cycle_to_the_horizon_end_has_no_planned_return() -> None:
    plan = settle_cycles(_weak(), _planning())
    [cycle] = plan.supply_cycles
    assert cycle.planned_return is None
    assert cycle.hours == 2.0  # to the end of the horizon


def test_the_running_cycle_is_named_by_the_actual_transition() -> None:
    """On meter 2 since 30 min before the horizon; back at the first allowed boundary."""
    forecast = Forecast(slots=_slots([2.0] * 4, base=[0.0] * 4, apartment=[2.0] * 4))
    solve = _solver(forecast, 100.0, [_period(PPE_1, 100.0, 30.0)])
    switched = H0 - timedelta(minutes=30)
    planning = _planning(
        ppe2=(100.0, 30.0), actual=SUPPLY_METER_2, min_stay_hours=2, last_transition_at=switched
    )
    plan = settle_cycles(solve, planning)

    assert _on_meter_2(plan) == [1, 1, 0, 0]
    [cycle] = plan.supply_cycles
    assert cycle.running is True
    assert cycle.id == _utc_id(switched)
    assert cycle.start == switched
    assert cycle.planned_return == H0 + timedelta(hours=2)
    assert cycle.hours == 2.5
    assert abs(cycle.apartment_kwh - 4.0) < 1e-9
    # The plan already returns at the next allowed boundary: nothing to compare.
    assert plan.supply_boundaries[0] == H0 + timedelta(hours=2)
    assert cycle.saving_pln == 0.0
    assert plan.supply_resolves == 0


# ---------------------------------------------------------- marginal value


def test_a_cycle_saving_equals_a_manual_resolve_without_it() -> None:
    solve = _weak()
    planning = _planning()
    plan = settle_cycles(solve, planning)
    [cycle] = plan.supply_cycles

    without = solve(_planning(victron_hours=cycle.victron_hours))
    assert _on_meter_2(without) == [0, 0, 0]
    assert abs(cycle.saving_pln - (without.comparable_cost_pln - plan.comparable_cost_pln)) < 1e-9
    # No wear, no terminal value: the saving is the settled difference.
    settled = sum(o.settled_cost_pln for o in without.billing) - sum(
        o.settled_cost_pln for o in plan.billing
    )
    assert abs(cycle.saving_pln - settled) < 1e-6
    assert abs(cycle.saving_pln - (6.0 - 4 * LIMIT)) < 1e-6


def test_the_threshold_is_not_part_of_the_comparable_cost() -> None:
    solve = _weak()
    low = solve(_planning(cycle_threshold_pln=0.5))
    high = solve(_planning(cycle_threshold_pln=3.0))
    assert _on_meter_2(low) == _on_meter_2(high) == [0, 1, 1]
    assert abs(low.comparable_cost_pln - high.comparable_cost_pln) < 1e-9


def test_a_running_cycle_is_valued_against_returning_at_the_next_boundary() -> None:
    """On meter 2 and staying pays: the saving is a re-solve returning at hour 1."""
    solve = _weak()
    planning = _planning(actual=SUPPLY_METER_2)
    plan = settle_cycles(solve, planning)
    assert _on_meter_2(plan) == [1, 1, 1]
    [cycle] = plan.supply_cycles
    assert cycle.running is True and cycle.planned_return is None

    back = solve(
        _planning(
            actual=SUPPLY_METER_2,
            victron_hours=frozenset({H0 + timedelta(hours=1), H0 + timedelta(hours=2)}),
        )
    )
    assert _on_meter_2(back) == [1, 0, 0]
    assert abs(cycle.saving_pln - (back.comparable_cost_pln - plan.comparable_cost_pln)) < 1e-9
    assert abs(cycle.saving_pln - (6.0 - 4 * LIMIT)) < 1e-6
    assert plan.supply_resolves == 1


def test_a_sub_threshold_cycle_is_dropped_and_the_plan_re_solved() -> None:
    """The plan's best cycle (hour 5) beats the one at hour 1 by less than 1 zł.

    Without the hour-5 cycle the plan opens the hour-1 one instead, so the
    hour-5 cycle's own saving is under the threshold: it is dropped, the plan
    becomes that re-solve, and the hour-1 cycle — valued with hour 5 still
    held — clears the threshold.
    """
    forecast = Forecast(
        slots=_slots(
            [0.3, 2.0, 3.0, 0.1, 0.6, 2.0],
            base=[0.0, 0.0, 1.0, 1.0, 0.0, 0.0],
            apartment=[0.0, 2.0, 0.0, 5.0, 5.0, 2.0],
        )
    )
    solve = _solver(forecast, 30.0, [_period(PPE_1, 100.0, 60.0)])
    planning = _planning(cycle_threshold_pln=1.0)

    first = solve(planning)
    assert _on_meter_2(first) == [0, 0, 0, 0, 0, 1]
    [late] = plan_cycles(first, None)
    instead = solve(_planning(cycle_threshold_pln=1.0, victron_hours=late.victron_hours))
    assert _on_meter_2(instead) == [0, 1, 0, 0, 0, 0]
    assert instead.comparable_cost_pln - first.comparable_cost_pln < 1.0

    plan = settle_cycles(solve, planning)
    assert plan.supply_dropped == [late.id]
    assert _on_meter_2(plan) == [0, 1, 0, 0, 0, 0]
    [cycle] = plan.supply_cycles
    nothing = solve(
        _planning(cycle_threshold_pln=1.0, victron_hours=late.victron_hours | cycle.victron_hours)
    )
    # Without it either, the best the plan finds is another, weaker cycle.
    assert _on_meter_2(nothing) == [0, 0, 0, 1, 1, 0]
    assert abs(cycle.saving_pln - (nothing.comparable_cost_pln - plan.comparable_cost_pln)) < 1e-9
    assert cycle.saving_pln >= 1.0
    # One re-solve per valuation; the dropped cycle's re-solve became the plan.
    assert plan.supply_resolves == 2


def test_held_hours_stay_on_the_victron() -> None:
    solve = _two_hours()
    plan = solve(
        _planning(cycle_threshold_pln=0.5, victron_hours=frozenset({H0 + timedelta(hours=1)}))
    )
    assert _on_meter_2(plan) == [0, 0, 1, 0]


def test_the_running_hour_cannot_be_held_while_on_meter_2() -> None:
    solve = _weak()
    with pytest.raises(ValueError):
        solve(_planning(actual=SUPPLY_METER_2, victron_hours=frozenset({H0})))


def test_without_switching_there_is_one_plan_and_no_cycles() -> None:
    calls = []
    solve = _weak()

    def counting(planning):
        calls.append(planning)
        return solve(planning)

    plan = settle_cycles(counting, _planning(actual=SUPPLY_METER_2, switching_reason="wyłączone"))
    assert len(calls) == 1
    assert plan.supply_cycles == [] and plan.supply_resolves == 0


# ----------------------------------------------------------- recommendation


NOW = H0 - timedelta(minutes=20)


def test_the_recommendation_is_the_next_hour_and_a_switch_is_required() -> None:
    planning = _planning()
    plan = settle_cycles(_weak(), planning)
    rec = recommendation(plan, planning, SUPPLY_VICTRON, planning.last_transition_at, NOW)

    assert rec.reason is None
    assert rec.recommended == plan.decisions[1].supply == SUPPLY_METER_2
    assert rec.switch_required is True
    cycle = plan.supply_cycles[0]
    assert rec.notice == (cycle.id, SUPPLY_METER_2)
    # The notification outlives the hour it was written in: the moment names its day.
    at = moment_label(H0 + timedelta(hours=1))
    assert rec.notice_message.startswith(f"Przełącz mieszkanie na licznik 2 do {at}")
    assert "powrót po horyzoncie planu" in rec.notice_message
    assert "oszczędność ~3,56 zł" in rec.notice_message
    assert rec.next_switch == H0 + timedelta(hours=1)
    assert rec.next_target == SUPPLY_METER_2
    assert rec.next_cycle is cycle and rec.cycle is cycle
    assert [h["supply"] for h in rec.hourly] == [SUPPLY_VICTRON, SUPPLY_METER_2, SUPPLY_METER_2]


def test_the_switch_is_no_longer_required_once_the_actual_state_follows() -> None:
    planning = _planning()
    plan = settle_cycles(_weak(), planning)
    rec = recommendation(plan, planning, SUPPLY_METER_2, NOW, NOW)
    assert rec.recommended == SUPPLY_METER_2
    assert rec.switch_required is False
    assert rec.notice is None


def test_a_planned_return_requires_the_switch_back_until_it_happens() -> None:
    forecast = Forecast(slots=_slots([2.0] * 4, base=[0.0] * 4, apartment=[2.0] * 4))
    solve = _solver(forecast, 100.0, [_period(PPE_1, 100.0, 30.0)])
    switched = H0 - timedelta(days=1)
    planning = _planning(
        ppe2=(100.0, 30.0), actual=SUPPLY_METER_2, min_stay_hours=2, last_transition_at=switched
    )
    plan = settle_cycles(solve, planning)
    assert _on_meter_2(plan) == [1, 0, 0, 0]

    rec = recommendation(plan, planning, SUPPLY_METER_2, switched, NOW)
    assert rec.recommended == SUPPLY_VICTRON
    assert rec.switch_required is True
    assert rec.notice == (_utc_id(switched), SUPPLY_VICTRON)
    assert "z powrotem na Victron" in rec.notice_message
    assert f"od {moment_label(switched)}." in rec.notice_message
    assert rec.next_target == SUPPLY_VICTRON
    assert rec.next_cycle.running is True

    after = recommendation(plan, planning, SUPPLY_VICTRON, H0, NOW)
    assert after.switch_required is False and after.notice is None


def test_a_re_plan_without_the_cycle_turns_the_switch_off() -> None:
    planning = _planning(cycle_threshold_pln=10.0)  # the cycle no longer pays
    plan = settle_cycles(_weak(), planning)
    assert plan.supply_cycles == []
    rec = recommendation(plan, planning, SUPPLY_VICTRON, None, NOW)
    assert rec.reason is None
    assert rec.recommended == SUPPLY_VICTRON
    assert rec.switch_required is False
    assert rec.next_switch is None and rec.cycle is None


def test_switching_not_modelled_gives_the_reason_and_no_recommendation() -> None:
    planning = _planning(switching_reason="rekomendacje przełączania są wyłączone")
    plan = settle_cycles(_weak(), planning)
    rec = recommendation(plan, planning, SUPPLY_VICTRON, None, NOW)
    assert rec.reason == "rekomendacje przełączania są wyłączone"
    assert rec.recommended is None and rec.switch_required is None
    assert rec.cycles == [] and rec.next_switch is None and rec.notice is None
    assert rec.available is True  # the user could switch; the plan does not model it

    inactive = recommendation(None, None, None, None, NOW)
    assert inactive.reason == REASON_NOT_ACTIVE
    assert inactive.available is None


# --------------------------------------------------------------- available


def _local_at(hour: int):
    return dt_util.start_of_local_day(dt_util.now() + timedelta(days=2)) + timedelta(
        hours=hour, minutes=15
    )


def test_switch_available_outside_the_quiet_window_while_present() -> None:
    planning = SupplyPlanning(actual=SUPPLY_VICTRON, quiet_start_hour=0, quiet_end_hour=9)
    assert switch_availability(planning, _local_at(10)) == (True, None)


def test_switch_not_available_in_the_quiet_window() -> None:
    planning = SupplyPlanning(actual=SUPPLY_VICTRON, quiet_start_hour=0, quiet_end_hour=9)
    available, why = switch_availability(planning, _local_at(8))
    assert available is False and "okno ciszy 00:00–09:00" == why


def test_switch_not_available_on_a_trip() -> None:
    moment = _local_at(12)
    planning = SupplyPlanning(
        actual=SUPPLY_VICTRON,
        quiet_start_hour=0,
        quiet_end_hour=9,
        unavailable=[(moment - timedelta(hours=1), moment + timedelta(hours=1))],
    )
    available, why = switch_availability(planning, moment)
    assert available is False and why == "wyjazd w kalendarzu"


def test_switch_not_available_when_a_calendar_cannot_be_read() -> None:
    planning = SupplyPlanning(actual=SUPPLY_VICTRON, calendar_error="calendar.work: gone")
    available, why = switch_availability(planning, _local_at(12))
    assert available is False and "calendar.work: gone" in why


# --------------------------------------------------------------- entities


def _entities(rec, plan=None):
    from custom_components.powerpilot.binary_sensor import (
        ApartmentSwitchAvailableBinarySensor,
        ApartmentSwitchRequiredBinarySensor,
    )
    from custom_components.powerpilot.sensor import (
        ApartmentCycleSavingsSensor,
        ApartmentNextSwitchSensor,
        ApartmentSupplyPlanSensor,
        ApartmentSupplyRecommendedSensor,
    )

    coordinator = SimpleNamespace(
        data=plan, supply_recommendation=lambda plan=None: rec, last_update_success=True
    )
    entry = SimpleNamespace(entry_id="entry")
    return [
        cls(coordinator, entry)
        for cls in (
            ApartmentSupplyRecommendedSensor,
            ApartmentSupplyPlanSensor,
            ApartmentNextSwitchSensor,
            ApartmentCycleSavingsSensor,
            ApartmentSwitchRequiredBinarySensor,
            ApartmentSwitchAvailableBinarySensor,
        )
    ]


def test_every_recommendation_entity_shows_the_reason_while_not_modelled() -> None:
    planning = _planning(switching_reason="rekomendacje przełączania są wyłączone")
    plan = settle_cycles(_weak(), planning)
    rec = recommendation(plan, planning, SUPPLY_VICTRON, None, NOW)
    entities = _entities(rec, plan)
    for entity in entities:
        assert entity.extra_state_attributes["reason"] == rec.reason, type(entity).__name__
    # Everything planned is unknown; whether the user could switch is still known.
    assert [e.native_value for e in entities[:4]] == [None] * 4
    assert entities[4].is_on is None
    assert entities[5].is_on is True


def test_the_recommendation_entities_while_modelled() -> None:
    planning = _planning()
    plan = settle_cycles(_weak(), planning)
    rec = recommendation(plan, planning, SUPPLY_VICTRON, None, NOW)
    recommended, plan_sensor, next_switch, savings, required, available = _entities(rec, plan)
    cycle = plan.supply_cycles[0]
    assert recommended.native_value == SUPPLY_METER_2
    assert plan_sensor.native_value == 1
    attributes = plan_sensor.extra_state_attributes
    assert attributes["cycles"] == [cycle.as_dict()]
    assert attributes["resolves"] == 1 and attributes["dropped"] == []
    assert len(attributes["hours"]) == 3
    assert next_switch.native_value == H0 + timedelta(hours=1)
    assert next_switch.extra_state_attributes == {
        "reason": None,
        "target": SUPPLY_METER_2,
        "planned_return": None,
        "cycle_id": cycle.id,
    }
    assert abs(savings.native_value - (6.0 - 4 * LIMIT)) < 1e-6
    assert required.is_on is True
    assert required.extra_state_attributes["cycle_id"] == cycle.id
    assert available.is_on is True
