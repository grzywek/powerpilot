"""Apartment switching between PPE 1 (Victron) and PPE 2 inside the optimizer.

Each meter settles its month as ``min(C, L·E)``. The plan may move the
apartment (``HourSlot.apartment_kwh``) to meter 2 in whole hours, paying the
cycle threshold per opened cycle, keeping each source at least the minimum
stay, never in the quiet window or while the user is away. Stage 3 plan,
"Slice 2 model design (binding)".
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot import optimizer as optimizer_module
from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.const import (
    PPE_1,
    PPE_2,
    SUPPLY_METER_2,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.models import (
    BillingPeriod,
    Forecast,
    HourSlot,
    MonthTail,
    SupplyPlanning,
)
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

H0 = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
LIMIT = 0.61


def _slots(
    prices: list[float],
    base: list[float],
    apartment: list[float],
    start=None,
) -> list[HourSlot]:
    start = start or H0
    return [
        HourSlot(
            start=start + timedelta(hours=i),
            buy_price=p,
            cap_price_kwh=p,  # no excise in these fixtures
            distribution_price_kwh=0.0,
            base_consumption_kwh=base[i],
            apartment_kwh=apartment[i],
        )
        for i, p in enumerate(prices)
    ]


def _period(ppe: str, energy: float, cost: float, start=None) -> BillingPeriod:
    start = start or H0
    return BillingPeriod(
        ppe=ppe,
        month_start=start,
        month_end=start + timedelta(days=60),
        limit_kwh=LIMIT,
        energy_kwh=energy,
        cost_pln=cost,
        hours_expected=1,
        hours_covered=1,
    )


def _battery(soc: float = 0.0) -> BatteryModel:
    return BatteryModel(
        capacity_kwh=10.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        wear_cost=0.0,
        min_soc=0.0,
        max_soc=100.0,
        soc=soc,
        energy_cost=0.0,
    )


def _optimizer(**overrides) -> Optimizer:
    cfg = dict(
        inverter_max_charge_kw=0.0,  # no charging: the stored energy is all there is
        inverter_max_discharge_kw=3.0,
        charge_curve=ChargeCurve(default_kw=3.0),
        terminal_price=0.0,  # stored energy has no value past the horizon
    )
    cfg.update(overrides)
    return Optimizer(OptimizerConfig(**cfg))


def _planning(
    ppe2: tuple[float, float] = (0.0, 0.0),
    actual: str | None = SUPPLY_VICTRON,
    start=None,
    **overrides,
) -> SupplyPlanning:
    values = dict(
        actual=actual,
        ppe2_periods=[_period(PPE_2, *ppe2, start=start)],
        last_transition_at=(start or H0) - timedelta(days=10),
        cycle_threshold_pln=2.0,
        min_stay_hours=1,
        quiet_start_hour=0,
        quiet_end_hour=0,  # no quiet window unless a test sets one
    )
    values.update(overrides)
    return SupplyPlanning(**values)


def _on_meter_2(plan) -> list[int]:
    return [1 if d.supply == SUPPLY_METER_2 else 0 for d in plan.decisions]


def _outcome(plan, ppe: str):
    return next(o for o in plan.billing if o.period.ppe == ppe)


def _settled(plan) -> float:
    return sum(o.settled_cost_pln for o in plan.billing)


def _fixed(planning: SupplyPlanning) -> SupplyPlanning:
    """The same inputs with switching not modelled (the plan without cycles)."""
    planning.switching_reason = "test: bez przełączania"
    return planning


# ------------------------------------------------------------------ weak


def _weak(**planning):
    """PPE 1 well under the limit; two 2.00 hours the battery covers only one of.

    The battery holds 3 kWh and covers a whole hour (Victron demand 1 + 2
    apartment = 3 kWh) or, with the apartment on meter 2, the Victron's own
    1 kWh. Kept on the Victron: one hour from the grid, 3 kWh × 2.00 = 6 zł on
    PPE 1. Moved to meter 2 in both hours: the battery covers the Victron's 2
    kWh, PPE 2 takes 4 kWh at 2.00, capped at 4 × 0.61 = 2.44 zł. Gain 3.56 zł.
    """
    forecast = Forecast(
        slots=_slots([0.3, 2.0, 2.0], base=[0.0, 1.0, 1.0], apartment=[0.0, 2.0, 2.0])
    )
    return (
        forecast,
        _battery(soc=30.0),
        [_period(PPE_1, 100.0, 30.0)],
        _planning(**planning),
    )


def test_weak_regime_switches_when_the_gain_beats_the_threshold() -> None:
    forecast, battery, billing, planning = _weak()
    plan = _optimizer().optimize(forecast, battery, billing=billing, supply=planning)

    assert plan.supply_switching is True
    assert _on_meter_2(plan) == [0, 1, 1]
    assert [d.supply for d in plan.decisions] == [
        SUPPLY_VICTRON,
        SUPPLY_METER_2,
        SUPPLY_METER_2,
    ]
    # On meter 2 the battery covers only the Victron's own demand (the product
    # z·s takes the apartment's share off the discharge).
    assert [round(d.battery_discharge_kwh, 6) for d in plan.decisions] == [0.0, 1.0, 1.0]
    assert [round(d.grid_buy_kwh, 6) for d in plan.decisions] == [0.0, 0.0, 0.0]

    ppe1 = _outcome(plan, PPE_1)
    assert ppe1.cap_binds is False
    assert abs(ppe1.settled_cost_pln - 30.0) < 1e-6
    ppe2 = _outcome(plan, PPE_2)
    assert ppe2.period.ppe == PPE_2
    assert ppe2.cap_binds is True
    assert abs(ppe2.energy_kwh - 4.0) < 1e-6
    assert abs(ppe2.planned_energy_kwh - 4.0) < 1e-6
    assert ppe2.tail_energy_kwh == 0.0
    assert abs(ppe2.cost_pln - 8.0) < 1e-6
    assert abs(ppe2.settled_cost_pln - 4 * LIMIT) < 1e-6

    _, battery, billing, planning = _weak()
    fixed = _optimizer().optimize(forecast, battery, billing=billing, supply=_fixed(planning))
    assert _on_meter_2(fixed) == [0, 0, 0]
    assert abs((_settled(fixed) - _settled(plan)) - (6.0 - 4 * LIMIT)) < 1e-6


@pytest.mark.parametrize(("threshold", "switches"), [(4.0, False), (3.0, True)])
def test_the_cycle_threshold_blocks_a_smaller_gain(threshold: float, switches: bool) -> None:
    """The cycle gains 3.56 zł: a 4 zł threshold blocks it, a 3 zł one does not."""
    forecast, battery, billing, planning = _weak(cycle_threshold_pln=threshold)
    plan = _optimizer().optimize(forecast, battery, billing=billing, supply=planning)
    assert _on_meter_2(plan) == ([0, 1, 1] if switches else [0, 0, 0])


# ---------------------------------------------------------------- strong


def test_strong_regime_moves_ppe_1_under_the_limit() -> None:
    """PPE 1 alone ends above the limit; the apartment's 2.00 hours carry it there.

    Without a switch every PPE 1 kWh costs the limit. Moved to meter 2, those
    hours are capped there (PPE 2 averages 2.00) and PPE 1 pays its real, low
    average: the saving is ``E1'·(L − avg1')`` for the energy left on PPE 1.
    """
    forecast = Forecast(
        slots=_slots([0.3, 2.0, 2.0], base=[1.0, 0.0, 0.0], apartment=[0.0, 10.0, 10.0])
    )
    billing = [_period(PPE_1, 100.0, 40.0)]
    opt = _optimizer(inverter_max_discharge_kw=0.0)

    fixed = opt.optimize(forecast, _battery(), billing=billing, supply=_fixed(_planning()))
    assert _outcome(fixed, PPE_1).cap_binds is True

    plan = opt.optimize(forecast, _battery(), billing=billing, supply=_planning())
    assert _on_meter_2(plan) == [0, 1, 1]
    ppe1 = _outcome(plan, PPE_1)
    assert ppe1.cap_binds is False
    assert _outcome(plan, PPE_2).cap_binds is True
    expected = ppe1.energy_kwh * (LIMIT - ppe1.average_kwh)
    assert abs((_settled(fixed) - _settled(plan)) - expected) < 1e-6
    assert abs(expected - (101 * LIMIT - 40.3)) < 1e-6


def test_the_month_tail_keeps_the_last_planned_supply() -> None:
    forecast = Forecast(
        slots=_slots([0.3, 2.0, 2.0], base=[1.0, 0.0, 0.0], apartment=[0.0, 10.0, 10.0])
    )
    tail = MonthTail(
        slots=_slots([2.0, 2.0], base=[0.0, 0.0], apartment=[5.0, 5.0], start=H0 + timedelta(hours=3))
    )
    plan = _optimizer(inverter_max_discharge_kw=0.0).optimize(
        forecast,
        _battery(),
        billing=[_period(PPE_1, 100.0, 40.0)],
        tail=tail,
        supply=_planning(),
    )
    assert _on_meter_2(plan) == [0, 1, 1]
    ppe2 = _outcome(plan, PPE_2)
    assert abs(ppe2.planned_energy_kwh - 20.0) < 1e-6
    assert abs(ppe2.tail_energy_kwh - 10.0) < 1e-6
    assert abs(_outcome(plan, PPE_1).tail_energy_kwh) < 1e-6


# ------------------------------------------------------------------ none


def test_no_switch_when_both_meters_are_capped_whatever_happens() -> None:
    forecast, battery, billing, planning = _weak(ppe2=(50.0, 50.0))
    billing = [_period(PPE_1, 100.0, 100.0)]
    plan = _optimizer().optimize(forecast, battery, billing=billing, supply=planning)
    assert _on_meter_2(plan) == [0, 0, 0]
    assert _outcome(plan, PPE_1).cap_binds is True
    assert _outcome(plan, PPE_2).cap_binds is True


# ----------------------------------------------------------------- rules


def _stay_case(min_stay: int):
    """One 2.00 hour worth moving, then a big 0.10 hour that belongs on PPE 1.

    Switching for the 2.00 hour alone and back (returning is free) beats
    staying; staying through the 0.10 hour lowers PPE 2's average under the
    limit and costs more than never switching.
    """
    forecast = Forecast(
        slots=_slots(
            [0.3, 2.0, 0.1, 0.1],
            base=[0.0, 1.0, 0.0, 0.0],
            apartment=[0.0, 2.0, 10.0, 0.0],
        )
    )
    return _optimizer(inverter_max_discharge_kw=0.0).optimize(
        forecast,
        _battery(),
        billing=[_period(PPE_1, 100.0, 30.0)],
        supply=_planning(cycle_threshold_pln=0.5, min_stay_hours=min_stay),
    )


def test_minimum_stay_one_hour_allows_a_one_hour_cycle() -> None:
    assert _on_meter_2(_stay_case(1)) == [0, 1, 0, 0]


def test_minimum_stay_forbids_the_short_cycle() -> None:
    assert _on_meter_2(_stay_case(2)) == [0, 0, 0, 0]


def _day_start(hour: int):
    """A future local day at ``hour`` (the running hour is never involved)."""
    return dt_util.start_of_local_day(dt_util.now() + timedelta(days=2)) + timedelta(
        hours=hour
    )


def _morning(**planning):
    """07:00–10:00: the apartment's 08:00 and 09:00 hours cost 2.00."""
    start = _day_start(7)
    forecast = Forecast(
        slots=_slots(
            [0.3, 2.0, 2.0, 0.3],
            base=[0.0, 0.0, 0.0, 0.0],
            apartment=[0.0, 2.0, 2.0, 1.0],
            start=start,
        )
    )
    return _optimizer(inverter_max_discharge_kw=0.0).optimize(
        forecast,
        _battery(),
        billing=[_period(PPE_1, 100.0, 30.0, start=start)],
        supply=_planning(start=start, cycle_threshold_pln=0.5, **planning),
    )


def test_switches_at_the_first_useful_boundary_without_a_quiet_window() -> None:
    assert _on_meter_2(_morning()) == [0, 1, 1, 0]


def test_no_switch_inside_the_quiet_window() -> None:
    """00:00–09:00 is quiet: the 08:00 boundary is out, 09:00 is the first."""
    plan = _morning(quiet_start_hour=0, quiet_end_hour=9)
    assert _on_meter_2(plan) == [0, 0, 1, 0]


def test_a_quiet_window_wraps_past_midnight() -> None:
    plan = _morning(quiet_start_hour=22, quiet_end_hour=9)
    assert _on_meter_2(plan) == [0, 0, 1, 0]


def test_no_switch_while_the_user_is_away() -> None:
    start = _day_start(7)
    away = (start + timedelta(minutes=50), start + timedelta(hours=1, minutes=30))
    plan = _morning(unavailable=[away])
    assert _on_meter_2(plan) == [0, 0, 1, 0]


def test_a_calendar_read_error_makes_the_user_unavailable() -> None:
    plan = _morning(calendar_error="kalendarz nieczytelny")
    assert _on_meter_2(plan) == [0, 0, 0, 0]
    assert plan.supply_switching is True


def _return_case(**planning):
    """On meter 2 now; the full battery would cover the apartment on the Victron."""
    forecast = Forecast(
        slots=_slots([2.0] * 4, base=[0.0] * 4, apartment=[2.0] * 4)
    )
    return _optimizer().optimize(
        forecast,
        _battery(soc=100.0),
        billing=[_period(PPE_1, 100.0, 30.0)],
        supply=_planning(ppe2=(100.0, 30.0), actual=SUPPLY_METER_2, **planning),
    )


def test_returning_is_free_and_planned_at_once() -> None:
    plan = _return_case(min_stay_hours=2)
    assert _on_meter_2(plan) == [1, 0, 0, 0]


def test_the_actual_last_transition_counts_toward_the_minimum_stay() -> None:
    """Switched 30 minutes before the horizon: no return before 2 h have passed."""
    plan = _return_case(min_stay_hours=2, last_transition_at=H0 - timedelta(minutes=30))
    assert _on_meter_2(plan) == [1, 1, 0, 0]


def _late_case():
    """The 2.00 hours come after a big 0.10 hour that belongs on PPE 1.

    Moving the apartment at hour 2 pays (2 × 2 kWh capped at 0.61); moving it
    already at hour 1 and staying puts 20 kWh at 0.10 on PPE 2, pulls its
    average under the limit and gains nothing.
    """
    forecast = Forecast(
        slots=_slots(
            [0.3, 0.1, 2.0, 2.0], base=[0.0] * 4, apartment=[0.0, 20.0, 2.0, 2.0]
        )
    )
    return _optimizer(inverter_max_discharge_kw=0.0).optimize(
        forecast,
        _battery(),
        billing=[_period(PPE_1, 100.0, 30.0)],
        supply=_planning(cycle_threshold_pln=0.5),
    )


def test_switches_are_planned_inside_the_switch_horizon() -> None:
    assert _on_meter_2(_late_case()) == [0, 0, 1, 1]


def test_no_switch_is_planned_past_the_switch_horizon(monkeypatch) -> None:
    """Beyond ``T_sw`` every hour keeps the supply of hour ``T_sw − 1``."""
    monkeypatch.setattr(optimizer_module, "SUPPLY_SWITCH_HOURS", 2)
    # Only the boundary into hour 1 is a switch hour, and it does not pay.
    assert _on_meter_2(_late_case()) == [0, 0, 0, 0]


# ------------------------------------------------- switching not modelled


def _today_plan(forecast: Forecast, battery, billing, on_meter_2: bool):
    """Stage 2: the apartment in the Victron demand, or left out on meter 2."""
    slots = [
        HourSlot(
            start=s.start,
            buy_price=s.buy_price,
            cap_price_kwh=s.cap_price_kwh,
            distribution_price_kwh=s.distribution_price_kwh,
            base_consumption_kwh=s.base_consumption_kwh,
            extra_load_kwh=0.0 if on_meter_2 else s.apartment_kwh,
        )
        for s in forecast.slots
    ]
    return _optimizer().optimize(Forecast(slots=slots), battery, billing=billing)


def _same_decisions(plan, today) -> None:
    assert len(plan.decisions) == len(today.decisions)
    for got, want in zip(plan.decisions, today.decisions):
        assert got.inverter_mode == want.inverter_mode
        for attr in (
            "battery_charge_kwh",
            "battery_discharge_kwh",
            "grid_buy_kwh",
            "battery_soc",
            "hour_cost",
            "energy_cost",
        ):
            assert abs(getattr(got, attr) - getattr(want, attr)) < 1e-9, attr
        assert got.trace["demand_kwh"] == want.trace["demand_kwh"]
    got1, want1 = _outcome(plan, PPE_1), _outcome(today, PPE_1)
    assert got1.cap_binds == want1.cap_binds
    assert abs(got1.energy_kwh - want1.energy_kwh) < 1e-9
    assert abs(got1.cost_pln - want1.cost_pln) < 1e-9


def test_unknown_supply_has_no_switches_and_plans_like_stage_2() -> None:
    forecast, battery, billing, planning = _weak(actual=None)
    plan = _optimizer().optimize(forecast, battery, billing=billing, supply=planning)
    today = _today_plan(forecast, _battery(soc=30.0), billing, on_meter_2=False)
    assert plan.supply_switching is False
    assert [d.supply for d in plan.decisions] == [SUPPLY_VICTRON] * 3
    _same_decisions(plan, today)
    # PPE 2 is settled on what it has: nothing is planned on it.
    assert abs(_outcome(plan, PPE_2).planned_energy_kwh) < 1e-9


@pytest.mark.parametrize("actual", [SUPPLY_VICTRON, SUPPLY_METER_2])
def test_switching_not_modelled_keeps_the_actual_supply_like_stage_2(actual: str) -> None:
    forecast, battery, billing, planning = _weak(
        actual=actual, switching_reason="rozliczenie PPE 2 niekompletne"
    )
    plan = _optimizer().optimize(forecast, battery, billing=billing, supply=planning)
    today = _today_plan(
        forecast, _battery(soc=30.0), billing, on_meter_2=actual == SUPPLY_METER_2
    )
    assert plan.supply_switching is False
    assert plan.supply_reason == "rozliczenie PPE 2 niekompletne"
    assert [d.supply for d in plan.decisions] == [actual] * 3
    _same_decisions(plan, today)
    ppe2 = _outcome(plan, PPE_2)
    expected = 4.0 if actual == SUPPLY_METER_2 else 0.0
    assert abs(ppe2.planned_energy_kwh - expected) < 1e-9
    assert abs(ppe2.energy_kwh - expected) < 1e-9


def test_without_ppe_2_periods_no_ppe_2_outcome_is_invented() -> None:
    forecast, battery, billing, planning = _weak(
        switching_reason="rozliczenie PPE 2 niekompletne", ppe2_periods=[]
    )
    plan = _optimizer().optimize(forecast, battery, billing=billing, supply=planning)
    assert [o.period.ppe for o in plan.billing] == [PPE_1]
    assert _on_meter_2(plan) == [0, 0, 0]


def test_switching_without_both_settlements_is_refused() -> None:
    forecast, battery, billing, planning = _weak(ppe2_periods=[])
    with pytest.raises(ValueError):
        _optimizer().optimize(forecast, battery, billing=billing, supply=planning)
    _, battery, _, planning = _weak()
    with pytest.raises(ValueError):
        _optimizer().optimize(forecast, battery, supply=planning)


def test_without_supply_detection_decisions_carry_no_supply() -> None:
    forecast = Forecast(slots=_slots([0.3, 2.0], base=[1.0, 1.0], apartment=[0.0, 0.0]))
    plan = _optimizer().optimize(forecast, _battery(soc=30.0))
    assert [d.supply for d in plan.decisions] == [None, None]
    assert plan.supply_switching is False and plan.supply_reason is None


def test_the_plan_shows_victron_consumption_under_the_planned_supply() -> None:
    """Chart, snapshot and sensor read the Victron's demand per the planned supply."""
    forecast, battery, billing, planning = _weak()
    plan = _optimizer().optimize(forecast, battery, billing=billing, supply=planning)
    assert _on_meter_2(plan) == [0, 1, 1]
    rows = plan.as_dict()["forecast"]
    assert [row["consumption_kwh"] for row in rows] == [0.0, 1.0, 1.0]
    assert [row["apartment_kwh"] for row in rows] == [0.0, 2.0, 2.0]
    assert plan.as_dict()["hours"][1]["supply"] == SUPPLY_METER_2

    _, battery, billing, planning = _weak()
    fixed = _optimizer().optimize(forecast, battery, billing=billing, supply=_fixed(planning))
    assert [plan_row["consumption_kwh"] for plan_row in fixed.as_dict()["forecast"]] == [
        0.0,
        3.0,
        3.0,
    ]


def test_the_chart_ppe_2_import_is_what_the_plan_settles_on_meter_2() -> None:
    """The chart's PPE 2 import per hour adds up to PPE 2's planned energy.

    ``Plan.ppe2_import_kwh`` is the counterpart of ``victron_consumption_kwh``;
    together they are the hour's whole consumption, apartment included.
    """
    forecast, battery, billing, planning = _weak()
    plan = _optimizer().optimize(forecast, battery, billing=billing, supply=planning)
    assert _on_meter_2(plan) == [0, 1, 1]
    n = len(plan.decisions)
    assert [plan.ppe2_import_kwh(i) for i in range(n)] == [0.0, 2.0, 2.0]
    assert abs(
        sum(plan.ppe2_import_kwh(i) for i in range(n))
        - _outcome(plan, PPE_2).planned_energy_kwh
    ) < 1e-9
    for i, slot in enumerate(plan.forecast.slots[:n]):
        whole = slot.total_consumption_kwh + slot.apartment_kwh
        assert abs(plan.victron_consumption_kwh(i) + plan.ppe2_import_kwh(i) - whole) < 1e-9


def test_four_periods_across_a_month_boundary_match_a_brute_force() -> None:
    """Both meters over two months (4 periods, 16 cap-regime combinations).

    No battery, so the plan's only choice is the apartment's supply per hour.
    Every supply sequence is settled by hand — each month of each meter at
    ``min(C, L·E)`` plus the threshold per opened cycle — and the plan must
    pick the cheapest one with the same settlement and regimes.
    """
    from itertools import product

    boundary = H0 + timedelta(hours=2)
    prices = [1.5, 0.2, 1.2, 0.1]
    base = [1.0, 1.0, 1.0, 1.0]
    apartment = [8.0, 2.0, 3.0, 6.0]
    threshold = 0.3

    def month(ppe, start, end, energy, cost):
        return BillingPeriod(
            ppe=ppe,
            month_start=start,
            month_end=end,
            limit_kwh=LIMIT,
            energy_kwh=energy,
            cost_pln=cost,
            hours_expected=1,
            hours_covered=1,
        )

    first = H0 - timedelta(days=10)
    later = boundary + timedelta(days=31)
    ppe1 = [month(PPE_1, first, boundary, 100.0, 60.0), month(PPE_1, boundary, later, 0.0, 0.0)]
    ppe2 = [month(PPE_2, first, boundary, 10.0, 3.0), month(PPE_2, boundary, later, 0.0, 0.0)]

    def settle(seq: tuple[int, ...]) -> tuple[float, list[tuple[float, float]]]:
        totals = []
        for meter, periods in ((0, ppe1), (1, ppe2)):
            for index, period in enumerate(periods):
                energy, cost = period.energy_kwh, period.cost_pln
                for t, price in enumerate(prices):
                    if (t < 2) != (index == 0):
                        continue
                    on_2 = seq[t]
                    kwh = base[t] + (0.0 if on_2 else apartment[t]) if meter == 0 else (
                        apartment[t] if on_2 else 0.0
                    )
                    energy += kwh
                    cost += price * kwh
                totals.append((cost, LIMIT * energy))
        opens = sum(1 for a, b in zip(seq, seq[1:]) if b > a)
        return sum(min(c, cap) for c, cap in totals) + threshold * opens, totals

    candidates = sorted((settle((0, *rest))[0], (0, *rest)) for rest in product((0, 1), repeat=3))
    best_cost, best_seq = candidates[0]
    assert candidates[1][0] - best_cost > 1e-3  # a unique optimum to compare against
    _, totals = settle(best_seq)
    # The optimum mixes the regimes: PPE 1 binds only in the first month, PPE 2
    # only in the second.
    assert [cap < cost for cost, cap in totals] == [True, False, False, True]

    optimizer = _optimizer(inverter_max_discharge_kw=0.0)
    plan = optimizer.optimize(
        Forecast(slots=_slots(prices, base=base, apartment=apartment)),
        _battery(),
        billing=ppe1,
        supply=_planning(
            cycle_threshold_pln=threshold, ppe2_periods=ppe2, last_transition_at=first
        ),
    )
    assert optimizer.last_regime_combinations == 16
    assert 1 <= len(optimizer.last_solve_seconds) <= 16  # the rest pruned by bound
    assert tuple(_on_meter_2(plan)) == best_seq
    assert [(o.period.ppe, o.period.month_start) for o in plan.billing] == [
        (p.ppe, p.month_start) for p in (*ppe1, *ppe2)
    ]
    opens = sum(1 for a, b in zip(best_seq, best_seq[1:]) if b > a)
    assert abs(_settled(plan) + threshold * opens - best_cost) < 1e-6
    for outcome, (cost, cap) in zip(plan.billing, totals):
        assert abs(outcome.cost_pln - cost) < 1e-6
        assert abs(outcome.settled_cost_pln - min(cost, cap)) < 1e-6
        if abs(cost - cap) > 1e-6:
            assert outcome.cap_binds is (cap < cost)
