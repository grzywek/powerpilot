"""EV charging-current control + efficiency curve in the allocator.

With ``current_control`` the top-off hour runs at the smallest configured
current whose full-hour yield covers the remainder (gentle top-ups instead of
a full-power burst), and the efficiency curve converts pack-side ("added")
energy into the grid-side draw the costs are paid on.
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
    _ev_efficiency_at,
)

H0 = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)

# 3 × 230 V charger: 5 A → 3.45 kW … 16 A → 11.04 kW.
CURVE = [
    {"kw": 3.45, "eff": 0.80},
    {"kw": 11.04, "eff": 0.90},
]


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
        phases=3,
        battery_kwh=60.0,
        current_soc=0.0,
        available_hours={H0 + timedelta(hours=i) for i in range(n_hours)},
        current_control=True,
        min_current_a=5,
        max_current_a=16,
        voltage=230.0,
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


def test_efficiency_interpolation() -> None:
    assert _ev_efficiency_at([], 5.0) == 1.0  # no curve → lossless (legacy)
    assert _ev_efficiency_at(CURVE, 1.0) == 0.80  # clamped below the range
    assert _ev_efficiency_at(CURVE, 20.0) == 0.90  # clamped above the range
    mid = _ev_efficiency_at(CURVE, (3.45 + 11.04) / 2)
    assert abs(mid - 0.85) < 1e-6  # linear between the samples


def test_small_top_up_fits_the_smallest_current() -> None:
    # 2 kWh into the pack: 5 A (3.45 kW) covers it in one gentle hour instead
    # of a full-power burst.
    alloc = _optimizer()._plan_ev(_forecast([0.10, 0.20]), _request(2.0, 2))
    assert list(alloc.added.values()) == [2.0]
    hour = next(iter(alloc.added))
    assert alloc.amps[hour] == 5
    assert alloc.grid[hour] == 2.0  # no curve on the request → lossless


def test_efficiency_curve_scales_the_grid_side() -> None:
    # 2 kWh added at the 5 A fit (3.45 kW → η 0.80) draws 2.5 kWh from the grid.
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.20]), _request(2.0, 2, efficiency_curve=CURVE)
    )
    hour = next(iter(alloc.added))
    assert alloc.amps[hour] == 5
    assert abs(alloc.added[hour] - 2.0) < 1e-6
    assert abs(alloc.grid[hour] - 2.0 / 0.80) < 1e-6


def test_full_hours_run_at_max_current() -> None:
    # A full-hour yield at 16 A / η 0.90 is 11.04 × 0.90 = 9.936 kWh added, so
    # 12 kWh needs a full cheapest hour plus a small top-off elsewhere.
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.30, 0.12]), _request(12.0, 3, efficiency_curve=CURVE)
    )
    full_yield = 11.04 * 0.90
    assert abs(sum(alloc.added.values()) - 12.0) < 1e-6
    fulls = [h for h, kwh in alloc.added.items() if abs(kwh - full_yield) < 1e-6]
    assert len(fulls) == 1
    assert alloc.amps[fulls[0]] == 16
    top_off = next(h for h in alloc.added if h not in fulls)
    # The ~2.06 kWh remainder fits the smallest current again.
    assert alloc.amps[top_off] == 5


def test_legacy_mode_keeps_full_power_and_no_amps() -> None:
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
    assert alloc.amps == {}
    assert alloc.grid == alloc.added
