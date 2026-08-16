"""House-battery charging steered by ``#ess`` tags.

``#ess_socNN`` is a deadline — be at NN % by the event's start. The battery
never leaves, so unlike the car there is no departure to aim at.

Bare ``#ess`` asks for charging in the hours the event covers. It is a
*preference*, not a hard constraint: a hard one could collide with the pack's
own SoC band and make the whole LP infeasible, which would take down the entire
house plan, not just that hour.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.models import ESSRequest, Forecast, HourSlot
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

H0 = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _h(i: int):
    return H0 + timedelta(hours=i)


def _battery(soc: float = 20.0) -> BatteryModel:
    return BatteryModel(
        capacity_kwh=10.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        wear_cost=0.0,
        min_soc=10.0,
        max_soc=100.0,
        soc=soc,
        energy_cost=0.0,
    )


def _forecast(prices: list[float]) -> Forecast:
    return Forecast(
        slots=[
            HourSlot(
                start=_h(i),
                buy_price=p,
                distribution_price_kwh=0.0,
                base_consumption_kwh=0.0,
            )
            for i, p in enumerate(prices)
        ]
    )


def _optimizer() -> Optimizer:
    return Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=3.0,
            inverter_max_discharge_kw=3.0,
            charge_curve=ChargeCurve(default_kw=3.0),
            terminal_price=0.0,  # no end-of-horizon incentive to hoard
        )
    )


def _soc_after(plan, index: int, battery: BatteryModel) -> float:
    """SoC (%) at the end of hour ``index``."""
    energy = battery.soc / 100.0 * battery.capacity_kwh
    for decision in plan.decisions[: index + 1]:
        energy += decision.battery_charge_kwh - decision.battery_discharge_kwh
    return energy / battery.capacity_kwh * 100.0


def test_soc_deadline_is_reached_by_the_deadline_hour() -> None:
    """Prices rise all day, so without the tag nothing would charge early."""
    battery = _battery(soc=20.0)
    plan = _optimizer().optimize(
        _forecast([0.50, 0.50, 0.50, 0.10]),
        battery,
        ess_request=ESSRequest(targets=[(_h(3), 80.0, "#ess_soc80")]),
    )

    assert _soc_after(plan, 2, battery) >= 80.0 - 1e-6


def test_no_request_leaves_the_plan_alone() -> None:
    battery = _battery(soc=20.0)
    plan = _optimizer().optimize(_forecast([0.50, 0.50, 0.50, 0.10]), battery)

    assert _soc_after(plan, 2, battery) < 80.0


def test_target_above_max_soc_is_clamped_not_infeasible() -> None:
    battery = _battery(soc=20.0)
    battery.max_soc = 60.0
    plan = _optimizer().optimize(
        _forecast([0.50, 0.50, 0.50, 0.10]),
        battery,
        ess_request=ESSRequest(targets=[(_h(3), 100.0, "#ess_soc100")]),
    )

    assert plan.decisions  # a plan came back at all
    assert _soc_after(plan, 2, battery) <= 60.0 + 1e-6


def test_unreachable_deadline_still_returns_a_plan() -> None:
    """3 kW into a 10 kWh pack cannot go 20 % → 100 % in one hour."""
    battery = _battery(soc=20.0)
    plan = _optimizer().optimize(
        _forecast([0.50, 0.50]),
        battery,
        ess_request=ESSRequest(targets=[(_h(1), 100.0, "#ess_soc100")]),
    )

    assert plan.decisions


def test_forced_hour_charges_even_when_it_is_the_priciest() -> None:
    battery = _battery(soc=20.0)
    plan = _optimizer().optimize(
        _forecast([0.10, 0.10, 9.99, 0.10]),
        battery,
        ess_request=ESSRequest(forced_hours={_h(2)}),
    )

    assert plan.decisions[2].battery_charge_kwh > 0.0


def test_forced_hour_on_a_full_battery_does_not_break_the_plan() -> None:
    """The pack has no room; the tag must not make the LP infeasible."""
    battery = _battery(soc=100.0)
    plan = _optimizer().optimize(
        _forecast([0.10, 0.10, 0.10]),
        battery,
        ess_request=ESSRequest(forced_hours={_h(0), _h(1), _h(2)}),
    )

    assert plan.decisions
    assert _soc_after(plan, 2, battery) <= 100.0 + 1e-6


def test_forced_hour_cost_is_reported_honestly() -> None:
    """The preference is an objective nudge; it must not leak into the cost."""
    battery = _battery(soc=20.0)
    plan = _optimizer().optimize(
        _forecast([0.10, 9.99]),
        battery,
        ess_request=ESSRequest(forced_hours={_h(1)}),
    )

    hour = plan.decisions[1]
    assert hour.hour_cost >= 0.0
    assert hour.energy_cost >= 0.0
