"""Monthly price cap (Tarcza) inside the optimizer's objective.

The cap settles the WHOLE month: ``min(C, limit × E)``. When it binds, every
extra kWh costs the limit whatever the hour's price — so energy arbitrage with
the battery stops paying and only distribution differences (and wear) matter.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.const import PPE_1, InverterMode
from custom_components.powerpilot.models import (
    BillingPeriod,
    Forecast,
    HourSlot,
    MonthTail,
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
    demand: float,
    start=None,
    distribution: list[float] | None = None,
) -> list[HourSlot]:
    start = start or H0
    distribution = distribution or [0.0] * len(prices)
    return [
        HourSlot(
            start=start + timedelta(hours=i),
            buy_price=p,
            cap_price_kwh=p,  # no excise in these fixtures
            distribution_price_kwh=distribution[i],
            base_consumption_kwh=demand,
        )
        for i, p in enumerate(prices)
    ]


def _period(
    energy: float = 0.0,
    cost: float = 0.0,
    limit: float = LIMIT,
    start=None,
    days: int = 60,
) -> BillingPeriod:
    start = start or H0
    return BillingPeriod(
        ppe=PPE_1,
        month_start=start,
        month_end=start + timedelta(days=days),
        limit_kwh=limit,
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
        inverter_max_charge_kw=3.0,
        inverter_max_discharge_kw=3.0,
        charge_curve=ChargeCurve(default_kw=3.0),
        terminal_price=0.0,  # stored energy has no value past the horizon
    )
    cfg.update(overrides)
    return Optimizer(OptimizerConfig(**cfg))


def _modes(plan) -> list[str]:
    return [d.inverter_mode for d in plan.decisions]


# ----------------------------------------------------------------- regimes


def test_non_binding_cap_leaves_the_plan_untouched() -> None:
    forecast = Forecast(slots=_slots([0.2, 0.2, 1.0, 1.0], demand=3.0))
    plain = _optimizer().optimize(forecast, _battery())
    capped = _optimizer().optimize(
        forecast, _battery(), billing=[_period(limit=5.0)]
    )
    assert _modes(plain) == [
        InverterMode.CHARGE,
        InverterMode.CHARGE,
        InverterMode.DISCHARGE,
        InverterMode.DISCHARGE,
    ]
    assert _modes(capped) == _modes(plain)
    assert [round(d.battery_charge_kwh, 6) for d in capped.decisions] == [
        round(d.battery_charge_kwh, 6) for d in plain.decisions
    ]
    outcome = capped.billing[0]
    assert outcome.cap_binds is False
    assert outcome.available
    # Grid import: 12 kWh demand + 6 kWh charged, all inside the horizon.
    assert abs(outcome.energy_kwh - 12.0) < 1e-6
    assert abs(outcome.planned_energy_kwh - 12.0) < 1e-6
    assert outcome.tail_energy_kwh == 0.0
    assert abs(outcome.discount_pln) < 1e-9


def test_binding_cap_removes_energy_arbitrage() -> None:
    """Month so far averages 1.00 PLN/kWh → the cap binds → every kWh costs 0.61."""
    forecast = Forecast(slots=_slots([0.2, 0.2, 1.0, 1.0], demand=3.0))
    period = _period(energy=100.0, cost=100.0)
    plan = _optimizer().optimize(forecast, _battery(), billing=[period])

    assert _modes(plan) == [InverterMode.PASSTHROUGH] * 4
    assert all(d.battery_charge_kwh == 0.0 for d in plan.decisions)
    outcome = plan.billing[0]
    assert outcome.cap_binds is True
    assert abs(outcome.energy_kwh - 112.0) < 1e-6
    assert abs(outcome.cost_pln - (100.0 + 0.2 * 6 + 1.0 * 6)) < 1e-6
    assert abs(outcome.settled_cost_pln - LIMIT * 112.0) < 1e-6
    assert abs(outcome.discount_pln - (107.2 - LIMIT * 112.0)) < 1e-6


def test_binding_cap_still_arbitrates_on_distribution() -> None:
    forecast = Forecast(
        slots=_slots(
            [0.2, 0.2, 1.0, 1.0], demand=3.0, distribution=[0.0, 0.0, 0.5, 0.5]
        )
    )
    period = _period(energy=100.0, cost=100.0)
    plan = _optimizer().optimize(forecast, _battery(), billing=[period])
    assert _modes(plan) == [
        InverterMode.CHARGE,
        InverterMode.CHARGE,
        InverterMode.DISCHARGE,
        InverterMode.DISCHARGE,
    ]
    assert plan.billing[0].cap_binds is True


def test_cap_binding_is_decided_by_the_whole_month_not_the_hour() -> None:
    """Cheap history pulls the average under the limit even in a 1.00 hour."""
    forecast = Forecast(slots=_slots([1.0, 1.0], demand=3.0))
    period = _period(energy=100.0, cost=20.0)  # 0.20 average so far
    plan = _optimizer().optimize(forecast, _battery(), billing=[period])
    outcome = plan.billing[0]
    assert outcome.cap_binds is False
    assert abs(outcome.average_kwh - 26.0 / 106.0) < 1e-9
    assert abs(outcome.settled_cost_pln - 26.0) < 1e-9


# ------------------------------------------------------------------- tail


def test_tail_hours_enter_the_settlement_but_not_the_decisions() -> None:
    forecast = Forecast(slots=_slots([0.5, 0.5], demand=1.0))
    tail = MonthTail(slots=_slots([0.7, 0.7, 0.7], demand=2.0, start=H0 + timedelta(hours=2)))
    # No battery movement, so the settlement is pure demand × price.
    plan = _optimizer(inverter_max_charge_kw=0.0, inverter_max_discharge_kw=0.0).optimize(
        forecast, _battery(), billing=[_period()], tail=tail
    )
    assert len(plan.decisions) == 2
    outcome = plan.billing[0]
    # E = 2 + 6 = 8 kWh, C = 1.0 + 4.2 = 5.2 → 0.65 avg > 0.61 → binds.
    assert abs(outcome.planned_energy_kwh - 2.0) < 1e-6
    assert abs(outcome.tail_energy_kwh - 6.0) < 1e-6
    assert abs(outcome.energy_kwh - 8.0) < 1e-6
    assert abs(outcome.cost_pln - 5.2) < 1e-6
    assert outcome.cap_binds is True
    assert abs(outcome.settled_cost_pln - LIMIT * 8.0) < 1e-6


def test_battery_charged_in_the_horizon_serves_the_tail() -> None:
    """The tail carries battery decisions too — arbitrage across the boundary."""
    forecast = Forecast(slots=_slots([0.5, 0.5], demand=1.0))
    tail = MonthTail(slots=_slots([0.7, 0.7, 0.7], demand=2.0, start=H0 + timedelta(hours=2)))
    plan = _optimizer().optimize(forecast, _battery(), billing=[_period()], tail=tail)
    assert _modes(plan) == [InverterMode.CHARGE, InverterMode.CHARGE]
    outcome = plan.billing[0]
    # 6 kWh bought at 0.50 inside the horizon cover the whole tail demand.
    assert abs(outcome.planned_energy_kwh - 8.0) < 1e-6
    assert abs(outcome.tail_energy_kwh) < 1e-6
    assert abs(outcome.cost_pln - 4.0) < 1e-6
    assert outcome.cap_binds is False


def test_tail_ev_import_stays_in_the_hours_the_car_charges() -> None:
    """Incident 2026-11-02: the LP placed the tail's EV import in the month's
    cheapest hours, so October's PPE 1 settled at 0.31 zł/kWh in the model
    against 0.56 on the meter. The learned charging keeps its own hours — a
    very cheap tail hour the car does not charge in attracts none of it — and
    the settlement includes it."""
    forecast = Forecast(slots=_slots([0.5], demand=0.0))
    tail = MonthTail(
        slots=_slots([0.9, 0.05, 0.9], demand=0.0, start=H0 + timedelta(hours=1)),
        ev_grid_kwh=[2.0, 0.0, 2.0],
    )
    plan = _optimizer().optimize(forecast, _battery(), billing=[_period()], tail=tail)
    outcome = plan.billing[0]
    assert abs(outcome.tail_energy_kwh - 4.0) < 1e-6
    assert abs(outcome.cost_pln - 4 * 0.9) < 1e-6
    assert outcome.cap_binds is True
    assert abs(outcome.settled_cost_pln - LIMIT * 4.0) < 1e-6


def test_tail_ev_import_needs_one_value_per_tail_hour() -> None:
    forecast = Forecast(slots=_slots([0.5], demand=0.0))
    tail = MonthTail(
        slots=_slots([0.9, 0.9], demand=0.0, start=H0 + timedelta(hours=1)),
        ev_grid_kwh=[2.0],
    )
    with pytest.raises(ValueError):
        _optimizer().optimize(forecast, _battery(), billing=[_period()], tail=tail)


def test_tail_without_billing_is_ignored() -> None:
    forecast = Forecast(slots=_slots([0.5, 0.5], demand=1.0))
    tail = MonthTail(slots=_slots([0.7], demand=2.0, start=H0 + timedelta(hours=2)))
    plan = _optimizer().optimize(forecast, _battery(), tail=tail)
    assert plan.billing == []
    assert len(plan.decisions) == 2


# --------------------------------------------------------------- periods


def test_horizon_crossing_a_month_boundary_settles_two_periods() -> None:
    boundary = H0 + timedelta(hours=2)
    forecast = Forecast(slots=_slots([1.0, 1.0, 1.0, 1.0], demand=1.0))
    september = BillingPeriod(
        ppe=PPE_1,
        month_start=H0 - timedelta(days=10),
        month_end=boundary,
        limit_kwh=LIMIT,
        energy_kwh=10.0,
        cost_pln=10.0,
        hours_expected=1,
        hours_covered=1,
    )
    october = BillingPeriod(
        ppe=PPE_1,
        month_start=boundary,
        month_end=boundary + timedelta(days=31),
        limit_kwh=LIMIT,
    )
    plan = _optimizer().optimize(
        forecast, _battery(), billing=[september, october]
    )
    assert [o.period.month_start for o in plan.billing] == [
        september.month_start,
        october.month_start,
    ]
    first, second = plan.billing
    assert abs(first.energy_kwh - 12.0) < 1e-6 and first.cap_binds is True
    assert abs(second.energy_kwh - 2.0) < 1e-6 and second.cap_binds is True
    assert abs(second.settled_cost_pln - LIMIT * 2.0) < 1e-6


def test_hours_of_an_uncapped_month_keep_their_hourly_energy_price() -> None:
    """The horizon runs from a capped month into one no edition covers.

    The capped month binds, so each of its kWh costs the limit (0.61); the next
    month's 2.00 hours are billed at their hourly price. The battery's two kWh
    belong in those hours. Regression: an hour without a cap price used to
    abort the whole cap, and any cap price on it was moved into a settlement
    that does not include the hour — pricing its energy as free.
    """
    boundary = H0 + timedelta(hours=2)
    slots = _slots([1.0, 1.0, 2.0, 2.0], demand=1.0)
    for slot in slots[2:]:
        slot.cap_price_kwh = None  # no edition of the terms covers that month
    capped = BillingPeriod(
        ppe=PPE_1,
        month_start=H0 - timedelta(days=10),
        month_end=boundary,
        limit_kwh=LIMIT,
        energy_kwh=100.0,
        cost_pln=100.0,
        hours_expected=1,
        hours_covered=1,
    )
    plan = _optimizer(inverter_max_charge_kw=0.0).optimize(
        Forecast(slots=slots), _battery(soc=20.0), billing=[capped]
    )
    assert _modes(plan) == [
        InverterMode.PASSTHROUGH,
        InverterMode.PASSTHROUGH,
        InverterMode.DISCHARGE,
        InverterMode.DISCHARGE,
    ]
    assert [o.period.month_start for o in plan.billing] == [capped.month_start]
    assert abs(plan.billing[0].planned_energy_kwh - 2.0) < 1e-6
    assert "cap_price" not in plan.decisions[2].trace


def test_missing_cap_price_is_refused_not_guessed() -> None:
    slots = _slots([0.5, 0.5], demand=1.0)
    slots[1].cap_price_kwh = None
    with pytest.raises(ValueError):
        _optimizer().optimize(Forecast(slots=slots), _battery(), billing=[_period()])


def test_unpriced_tail_hour_is_refused() -> None:
    forecast = Forecast(slots=_slots([0.5], demand=1.0))
    tail_slots = _slots([0.7, 0.7], demand=1.0, start=H0 + timedelta(hours=1))
    tail_slots[1].buy_price = None
    with pytest.raises(ValueError):
        _optimizer().optimize(
            forecast, _battery(), billing=[_period()], tail=MonthTail(slots=tail_slots)
        )


# ------------------------------------------- bind probability (ADR 003)
#
# PPE 1's month is priced as p·L·E + (1 − p)·C, p being the share of four
# replayed real weeks in which it binds. On 2026-11-02 one averaged tail path
# taken as certain gave November up; near the limit single weeks disagree.


def _same_plan(a, b) -> None:
    assert _modes(a) == _modes(b)
    assert [round(d.battery_charge_kwh, 6) for d in a.decisions] == [
        round(d.battery_charge_kwh, 6) for d in b.decisions
    ]
    assert a.comparable_cost_pln == pytest.approx(b.comparable_cost_pln, abs=1e-6)
    assert a.billing[0].energy_kwh == pytest.approx(b.billing[0].energy_kwh)
    assert a.billing[0].cost_pln == pytest.approx(b.billing[0].cost_pln)
    assert a.billing[0].cap_binds == b.billing[0].cap_binds


def test_certain_binding_odds_reproduce_todays_binding_plan() -> None:
    forecast = Forecast(
        slots=_slots([0.2, 0.2, 1.0, 1.0], demand=3.0, distribution=[0.0, 0.0, 0.5, 0.5])
    )
    period = _period(energy=100.0, cost=100.0)
    today = _optimizer().optimize(forecast, _battery(), billing=[period])
    assert today.billing[0].cap_binds is True
    odds = _optimizer().optimize(
        forecast, _battery(), billing=[period], bind_probability={period.month_start: 1.0}
    )
    _same_plan(odds, today)


def test_certain_non_binding_odds_reproduce_todays_non_binding_plan() -> None:
    forecast = Forecast(slots=_slots([0.2, 0.2, 1.0, 1.0], demand=3.0))
    period = _period(energy=100.0, cost=20.0)
    today = _optimizer().optimize(forecast, _battery(), billing=[period])
    assert today.billing[0].cap_binds is False
    odds = _optimizer().optimize(
        forecast, _battery(), billing=[period], bind_probability={period.month_start: 0.0}
    )
    _same_plan(odds, today)


def test_the_odds_price_the_kwh_while_the_outcome_keeps_the_plans_path() -> None:
    """p = 1 on a month whose own path stays under the limit: planned as
    binding (no energy arbitrage), reported as the path settles it."""
    forecast = Forecast(slots=_slots([0.2, 0.2, 1.0, 1.0], demand=3.0))
    period = _period(energy=100.0, cost=20.0)
    plan = _optimizer().optimize(
        forecast, _battery(), billing=[period], bind_probability={period.month_start: 1.0}
    )
    assert _modes(plan) == [InverterMode.PASSTHROUGH] * 4
    outcome = plan.billing[0]
    assert outcome.cap_binds is False
    assert outcome.settled_cost_pln == pytest.approx(outcome.cost_pln)


def test_half_odds_price_a_kwh_at_its_expected_cost() -> None:
    """At p = 0.5 a kWh of an hour priced 2.00 costs PPE 1
    0.5·0.61 + 0.5·2.00 = 1.305, plus distribution."""
    forecast = Forecast(slots=_slots([2.0], demand=1.0, distribution=[0.1]))
    plan = _optimizer(inverter_max_charge_kw=0.0, inverter_max_discharge_kw=0.0).optimize(
        forecast, _battery(), billing=[_period()], bind_probability={H0: 0.5}
    )
    assert plan.objective_pln == pytest.approx(1.305 + 0.1)


def test_half_odds_charge_in_the_cheaper_of_two_hours_of_one_zone() -> None:
    """At p = 1 every kWh costs the limit, so 1.12 and 2.23 cost the same and
    the battery may pick either; at p = 0.5 the 1.12 hour is cheaper."""
    slots = _slots([1.12, 2.23, 3.0], demand=0.0, distribution=[0.1, 0.1, 0.1])
    slots[2].base_consumption_kwh = 3.0
    plan = _optimizer().optimize(
        Forecast(slots=slots),
        _battery(),
        billing=[_period(energy=100.0, cost=100.0)],
        bind_probability={H0: 0.5},
    )
    assert _modes(plan) == [
        InverterMode.CHARGE,
        InverterMode.PASSTHROUGH,
        InverterMode.DISCHARGE,
    ]


def test_every_ppe1_month_needs_its_odds() -> None:
    forecast = Forecast(slots=_slots([0.5], demand=1.0))
    with pytest.raises(ValueError):
        _optimizer().optimize(
            forecast, _battery(), billing=[_period()], bind_probability={H0 - timedelta(days=1): 0.5}
        )
