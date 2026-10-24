"""``Plan.objective_pln``: the horizon's spend in złoty, whatever steers the solver.

A missed plan action is costed by solving the same inputs twice — reality and
the plan followed — and subtracting (ADR 002). That only means PLN if the
number compared is money: the solver's own objective prices only what it can
move (the house's demand and the EV's planned load are constants it never
sees) and carries steering terms worth far more than any price (the
reserve-recovery penalty, the ``#ess`` bonus).
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.models import ESSRequest, Forecast, HourSlot
from custom_components.powerpilot.optimizer import ChargeCurve, Optimizer, OptimizerConfig

WEAR = 0.05
ETA = 0.95


def _start():
    # Whole future hours only: the running hour is scaled to its remaining minutes.
    return dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _forecast(prices: list[float], demand: float = 0.6) -> Forecast:
    start = _start()
    return Forecast(
        slots=[
            HourSlot(
                start=start + timedelta(hours=i),
                buy_price=price,
                distribution_price_kwh=0.1,
                base_consumption_kwh=demand,
            )
            for i, price in enumerate(prices)
        ]
    )


def _battery(soc: float, recovering: bool = False) -> BatteryModel:
    return BatteryModel(
        capacity_kwh=10.0,
        charge_efficiency=ETA,
        discharge_efficiency=ETA,
        wear_cost=WEAR,
        min_soc=10.0,
        max_soc=100.0,
        soc=soc,
        energy_cost=0.5,
        recovering=recovering,
    )


def _optimizer() -> Optimizer:
    return Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=1.0,
            inverter_max_discharge_kw=3.0,
            charge_curve=ChargeCurve(default_kw=1.0),
            # No value for energy left at the end: the spend is then the grid
            # bill plus wear, which the test can add up from the decisions.
            terminal_price=0.0,
        )
    )


def _spend(plan) -> float:
    """The plan's bill from its own decisions: grid at the hour's price, plus wear."""
    total = 0.0
    for decision, slot in zip(plan.decisions, plan.forecast.slots):
        total += decision.grid_buy_kwh * slot.total_price_kwh
        # ``battery_charge_kwh`` is the stored (post-loss) energy.
        total += WEAR * (decision.battery_charge_kwh + decision.battery_discharge_kwh)
    return total


def test_the_objective_in_pln_is_the_bill() -> None:
    # Cheap first, dear after: the plan charges the pack, then runs on it.
    prices = [0.1, 2.0, 0.2, 2.0, 2.0, 2.0]
    plan = _optimizer().optimize(_forecast(prices), _battery(soc=15.0))
    assert any(d.battery_charge_kwh > 0.5 for d in plan.decisions)
    assert any(d.battery_discharge_kwh > 0.5 for d in plan.decisions)
    assert plan.objective_pln == pytest.approx(_spend(plan), abs=1e-3)
    # The solver's objective leaves the house's own demand out entirely.
    assert plan.comparable_cost_pln < plan.objective_pln - 1.0


def test_the_recovery_penalty_is_not_money() -> None:
    # 1 kW from 2 % of 10 kWh: the reserve target (min + margin) is hours
    # away, so the penalty is paid in every one of them.
    plan = _optimizer().optimize(_forecast([0.4, 0.4, 0.4, 0.4]), _battery(soc=2.0))
    assert plan.comparable_cost_pln - plan.objective_pln > 100.0
    assert plan.objective_pln == pytest.approx(_spend(plan), abs=1e-3)


def test_the_ess_bonus_is_not_money() -> None:
    forecast = _forecast([0.9, 0.9, 0.9])
    forced = {forecast.slots[1].start}
    plan = _optimizer().optimize(
        forecast, _battery(soc=40.0), ess_request=ESSRequest(forced_hours=forced)
    )
    assert plan.decisions[1].battery_charge_kwh > 0.5
    assert plan.comparable_cost_pln < -500.0
    assert plan.objective_pln == pytest.approx(_spend(plan), abs=1e-3)


def test_the_car_charged_in_a_dearer_hour_costs_the_price_difference() -> None:
    """The EV's load is a constant to the solver, so its own objective cannot
    tell a car charged at 0.30 from one charged at 1.50 — the bill can."""
    from custom_components.powerpilot.modules.ev import EVRequest

    forecast = _forecast([0.3, 1.5, 1.5], demand=0.0)
    hours = [slot.start for slot in forecast.slots]

    def plan_with(available: set):
        request = EVRequest(
            enabled=True,
            required_kwh=2.0,
            charger_kw=2.0,
            phases=1,
            battery_kwh=10.0,
            current_soc=0.0,
            available_hours=available,
        )
        return _optimizer().optimize(forecast, _battery(soc=10.0), request)

    cheap = plan_with({hours[0], hours[1]})
    dear = plan_with({hours[1]})
    assert cheap.decisions[0].ev_charge_kwh == pytest.approx(2.0)
    assert dear.decisions[1].ev_charge_kwh == pytest.approx(2.0)
    assert dear.objective_pln - cheap.objective_pln == pytest.approx(2.0 * 1.2, abs=1e-3)
