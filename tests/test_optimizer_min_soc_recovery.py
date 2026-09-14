"""Reserve recovery: a pack below its minimum SoC is charged back before use.

A real pack can end up under ``min_soc`` (the plan was not executed, the BMS
drifted, the inverter kept discharging). The optimizer must then plan from the
real level — not pretend the pack sits at the minimum — and charge it back to
``min_soc + RECOVERY_MARGIN_SOC`` as fast as physically possible, whatever the
price, never discharging on the way. The coordinator latches the recovery until
that margin is reached, so the pack does not hover on the minimum.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.battery import (
    RECOVERY_MARGIN_SOC,
    BatteryModel,
    next_recovery_state,
)
from custom_components.powerpilot.const import InverterMode
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

MIN_SOC = 6.0
TARGET = MIN_SOC + RECOVERY_MARGIN_SOC


def _battery(soc: float, recovering: bool = False) -> BatteryModel:
    return BatteryModel(
        capacity_kwh=30.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        wear_cost=0.05,
        min_soc=MIN_SOC,
        max_soc=100.0,
        soc=soc,
        energy_cost=1.0,
        recovering=recovering,
    )


def _forecast(prices: list[float], demand: float = 0.5) -> Forecast:
    # Whole future hours only: the running hour would be scaled to its
    # remaining minutes and make the assertions time-of-run dependent.
    start = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(
        hours=1
    )
    return Forecast(
        slots=[
            HourSlot(
                start=start + timedelta(hours=i),
                buy_price=price,
                distribution_price_kwh=0.0,
                base_consumption_kwh=demand,
            )
            for i, price in enumerate(prices)
        ]
    )


def _config() -> OptimizerConfig:
    return OptimizerConfig(
        inverter_max_charge_kw=7.3,
        inverter_max_discharge_kw=7.0,
        # Low-SoC band charges slower than the inverter maximum — recovery must
        # respect it rather than ask for an unreachable level.
        charge_curve=ChargeCurve(
            default_kw=7.3,
            segments=[
                {"soc_from": 0, "soc_to": 11, "max_kw": 6.0},
                {"soc_from": 11, "soc_to": 101, "max_kw": 7.3},
            ],
        ),
        min_charge_power_kw=1.0,
    )


def test_empty_pack_recharges_immediately_even_when_expensive() -> None:
    # Expensive now, cheap later: economics alone would wait. Recovery must not.
    prices = [2.0, 2.0, 2.0, 0.2, 0.2, 2.5, 2.5, 2.5]
    plan = Optimizer(_config()).optimize(_forecast(prices), _battery(soc=0.0))

    first = plan.decisions[0]
    assert first.inverter_mode == InverterMode.CHARGE
    assert first.trace["soc_before"] == 0.0
    # 6 kW (curve band) × η 0.95 = 5.7 kWh → 19 % of 30 kWh: past the target
    # in the very first hour.
    assert first.battery_soc >= TARGET - 1e-6
    assert "rezerw" in first.trace["reason"]


def test_plan_starts_from_the_real_level_not_the_minimum() -> None:
    plan = Optimizer(_config()).optimize(_forecast([1.0] * 4), _battery(soc=2.0))
    assert plan.decisions[0].trace["soc_before"] == 2.0


def test_no_discharge_until_the_margin_is_reached() -> None:
    # Pricey hours everywhere: discharging would pay off, but not from a
    # pack that is still under the recovery target.
    prices = [3.0] * 6
    plan = Optimizer(_config()).optimize(_forecast(prices), _battery(soc=1.0))
    for decision in plan.decisions:
        if decision.trace["soc_before"] < TARGET - 1e-6:
            assert decision.battery_discharge_kwh == 0.0
    assert min(d.battery_soc for d in plan.decisions) >= TARGET - 1e-6


def test_latched_recovery_above_minimum_still_tops_up_to_margin() -> None:
    # 7 % is above the 6 % minimum but the latch is still on (came from below).
    prices = [3.0] * 4
    plan = Optimizer(_config()).optimize(
        _forecast(prices), _battery(soc=7.0, recovering=True)
    )
    assert plan.decisions[0].inverter_mode == InverterMode.CHARGE
    assert plan.decisions[0].battery_discharge_kwh == 0.0
    assert min(d.battery_soc for d in plan.decisions) >= TARGET - 1e-6


def test_without_latch_the_normal_band_applies() -> None:
    # Between the minimum and the margin, no latch: the pack may be used down
    # to the plain minimum (expensive now, cheap later → discharge pays off).
    prices = [3.0, 3.0, 0.5, 0.5]
    plan = Optimizer(_config()).optimize(
        _forecast(prices, demand=0.3), _battery(soc=8.0)
    )
    assert any(d.battery_discharge_kwh > 0 for d in plan.decisions)
    lowest = min(d.battery_soc for d in plan.decisions)
    assert MIN_SOC - 1e-6 <= lowest < TARGET


def test_latch_sets_below_minimum_and_clears_at_margin() -> None:
    assert next_recovery_state(5.9, MIN_SOC, was_recovering=False) is True
    assert next_recovery_state(6.0, MIN_SOC, was_recovering=False) is False
    # Hysteresis: between the minimum and the margin the previous state holds.
    assert next_recovery_state(7.5, MIN_SOC, was_recovering=True) is True
    assert next_recovery_state(7.5, MIN_SOC, was_recovering=False) is False
    assert next_recovery_state(TARGET, MIN_SOC, was_recovering=True) is False
