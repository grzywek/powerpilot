"""EV charging minutes + flat charging efficiency in the allocator.

The charger always runs at full power; each planned hour carries how many
minutes within it the charger should run (the user's automation stops it),
and the flat efficiency converts pack-side ("added") energy into the metered
grid draw the costs are paid on.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.ev import EVRequest
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

H0 = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

# 3 × 3.68 kW = 11.04 kW full charger power, 91 % efficient at full power.
CHARGER_KW = 3.68
PHASES = 3
EFF = 0.91
P_FULL = CHARGER_KW * PHASES
HOUR_YIELD = P_FULL * EFF  # ≈ 10.05 kWh added per full hour


def _forecast(prices: list[float]) -> Forecast:
    return Forecast(
        slots=[
            HourSlot(
                start=H0 + timedelta(hours=i),
                buy_price=p,
                distribution_price_kwh=0.0,
                base_consumption_kwh=0.0,
            )
            for i, p in enumerate(prices)
        ]
    )


def _request(required_kwh: float, n_hours: int, **kwargs) -> EVRequest:
    return EVRequest(
        enabled=True,
        required_kwh=required_kwh,
        charger_kw=CHARGER_KW,
        phases=PHASES,
        battery_kwh=60.0,
        current_soc=0.0,
        available_hours={H0 + timedelta(hours=i) for i in range(n_hours)},
        charge_efficiency=EFF,
        **kwargs,
    )


def _optimizer() -> Optimizer:
    return Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=3.0,
            inverter_max_discharge_kw=3.0,
            charge_curve=ChargeCurve(default_kw=3.0),
        )
    )


def test_flat_efficiency_scales_the_grid_side() -> None:
    # 2 kWh into the pack draws 2 / 0.91 ≈ 2.198 kWh from the grid.
    alloc = _optimizer()._plan_ev(_forecast([0.10, 0.20]), _request(2.0, 2))
    hour = next(iter(alloc.added))
    assert abs(alloc.added[hour] - 2.0) < 1e-6
    assert abs(alloc.grid[hour] - 2.0 / EFF) < 1e-6


def test_small_top_up_gets_short_minutes() -> None:
    # 2 kWh added at a ~10.05 kWh/h full-power yield ≈ 12 minutes of charging.
    alloc = _optimizer()._plan_ev(_forecast([0.10, 0.20]), _request(2.0, 2))
    hour = next(iter(alloc.added))
    assert alloc.minutes[hour] == 12


def test_full_hours_get_60_minutes_and_top_off_the_rest() -> None:
    # 12 kWh needs one full cheapest hour (60 min) plus a ~2 kWh top-off.
    alloc = _optimizer()._plan_ev(_forecast([0.10, 0.30, 0.12]), _request(12.0, 3))
    assert abs(sum(alloc.added.values()) - 12.0) < 1e-6
    fulls = [h for h, kwh in alloc.added.items() if abs(kwh - HOUR_YIELD) < 1e-6]
    assert len(fulls) == 1
    assert alloc.minutes[fulls[0]] == 60
    top_off = next(h for h in alloc.added if h not in fulls)
    assert 1 <= alloc.minutes[top_off] < 60


def test_lossless_default_keeps_grid_equal_to_added() -> None:
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.20]),
        EVRequest(
            enabled=True,
            required_kwh=2.0,
            charger_kw=3.5,
            phases=1,
            battery_kwh=60.0,
            current_soc=0.0,
            available_hours={H0, H0 + timedelta(hours=1)},
        ),
    )
    assert list(alloc.added.values()) == [2.0]
    assert alloc.grid == alloc.added
    hour = next(iter(alloc.added))
    # 2 kWh at 3.5 kW → ~35 minutes (rounded up).
    assert alloc.minutes[hour] == 35
