"""Minimum-charge-power (semi-continuous charge) behaviour in the optimizer.

A nearly-full battery on a very cheap hour wants to top up the last sliver of
headroom — producing a trivial sub-kW charge. The ``min_charge_power_kw`` floor
should turn that into "don't bother": charge 0 or ≥ the floor, never in between.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)


def _battery() -> BatteryModel:
    # 98 % of a 10 kWh pack → 0.2 kWh headroom to the 100 % ceiling.
    return BatteryModel(
        capacity_kwh=10.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        wear_cost=0.0,
        min_soc=10.0,
        max_soc=100.0,
        soc=98.0,
        energy_cost=0.0,
    )


def _forecast() -> Forecast:
    # One cheap hour, no household demand → charging is purely opportunistic.
    # The NEXT clock hour: the running hour would be scaled to its remaining
    # minutes and make the assertions time-of-run dependent.
    start = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    )
    slot = HourSlot(
        start=start,
        buy_price=0.10,
        distribution_price_kwh=0.0,
        base_consumption_kwh=0.0,
    )
    return Forecast(slots=[slot])


def _config(min_charge: float) -> OptimizerConfig:
    return OptimizerConfig(
        inverter_max_charge_kw=3.0,
        inverter_max_discharge_kw=3.0,
        charge_curve=ChargeCurve(default_kw=3.0),
        # High terminal value makes topping up the last sliver worthwhile.
        terminal_price=2.0,
        min_charge_power_kw=min_charge,
    )


def test_without_floor_a_tiny_charge_appears() -> None:
    plan = Optimizer(_config(min_charge=0.0)).optimize(_forecast(), _battery())
    charge = plan.decisions[0].battery_charge_kwh
    # Only the 0.2 kWh headroom can be stored — a "silly" sub-kW dribble.
    assert 0.0 < charge <= 0.21


def test_floor_suppresses_the_dribble() -> None:
    plan = Optimizer(_config(min_charge=0.5)).optimize(_forecast(), _battery())
    charge = plan.decisions[0].battery_charge_kwh
    # The 0.2 kWh top-up is below the 0.5 kW floor and cannot reach it (SoC
    # ceiling caps at 0.2) → the optimizer declines to charge at all.
    assert charge == 0.0


def test_floor_allows_charge_when_headroom_exceeds_it() -> None:
    # Half-full pack: plenty of headroom, cheap hour → charges well above floor.
    battery = _battery()
    battery.soc = 50.0
    plan = Optimizer(_config(min_charge=0.5)).optimize(_forecast(), battery)
    charge = plan.decisions[0].battery_charge_kwh
    assert charge >= 0.5
