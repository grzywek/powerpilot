"""Partial-current-hour modelling in the optimizer.

A plan computed mid-hour can only use the REMAINDER of the running clock hour:
the charge/discharge caps, the household demand and the EV charger yield of
slot 0 all scale with the remaining minutes, and ``charge_power_kw`` stays a
true power (energy ÷ remaining window). This is what makes mid-hour re-plans
(the EV getting unplugged, a calendar edit, a restart) physically consistent —
and what replaced the old committed-decision freeze.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.ev import EVRequest
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

NOW = datetime(2026, 8, 9, 14, 30, tzinfo=timezone.utc)  # half past the hour


def _battery(soc: float = 50.0) -> BatteryModel:
    return BatteryModel(
        capacity_kwh=20.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        wear_cost=0.0,
        min_soc=10.0,
        max_soc=100.0,
        soc=soc,
        energy_cost=0.0,
    )


def _optimizer(**overrides) -> Optimizer:
    cfg = dict(
        inverter_max_charge_kw=3.0,
        inverter_max_discharge_kw=3.0,
        charge_curve=ChargeCurve(default_kw=3.0),
        # A high terminal value makes charging worthwhile in every cheap hour.
        terminal_price=2.0,
    )
    cfg.update(overrides)
    return Optimizer(OptimizerConfig(**cfg))


def _forecast(prices: list[float], consumption: float = 0.0) -> Forecast:
    start = NOW.replace(minute=0, second=0, microsecond=0)
    return Forecast(
        slots=[
            HourSlot(
                start=start + timedelta(hours=i),
                buy_price=price,
                distribution_price_kwh=0.0,
                base_consumption_kwh=consumption,
            )
            for i, price in enumerate(prices)
        ]
    )


def test_running_hour_charge_energy_scales_with_remaining_minutes(freezer) -> None:
    freezer.move_to(NOW)
    plan = _optimizer().optimize(_forecast([0.10]), _battery())
    d = plan.decisions[0]
    # 30 of 60 minutes left → at most half the hourly cap lands in the pack…
    assert abs(d.battery_charge_kwh - 1.5) < 1e-6
    # …but the setpoint is a true power: full 3 kW for the remaining window.
    assert abs(d.charge_power_kw - 3.0) < 1e-6
    assert d.trace["hour_fraction"] == 0.5


def test_at_the_hour_boundary_the_full_hour_is_plannable(freezer) -> None:
    freezer.move_to(NOW.replace(minute=0, second=0, microsecond=0))
    plan = _optimizer().optimize(_forecast([0.10]), _battery())
    d = plan.decisions[0]
    assert abs(d.battery_charge_kwh - 3.0) < 1e-6
    assert abs(d.charge_power_kw - 3.0) < 1e-6
    assert "hour_fraction" not in d.trace


def test_future_hours_keep_their_full_capacity(freezer) -> None:
    freezer.move_to(NOW)
    plan = _optimizer().optimize(_forecast([0.10, 0.10]), _battery())
    assert abs(plan.decisions[0].battery_charge_kwh - 1.5) < 1e-6
    assert abs(plan.decisions[1].battery_charge_kwh - 3.0) < 1e-6


def test_running_hour_demand_scales_too(freezer) -> None:
    freezer.move_to(NOW)
    # Expensive hour, empty-ish battery → passthrough; the grid covers only
    # the remaining half of the hour's forecast consumption.
    plan = _optimizer(terminal_price=0.0).optimize(
        _forecast([5.0], consumption=1.0), _battery(soc=10.0)
    )
    d = plan.decisions[0]
    assert abs(d.grid_buy_kwh - 0.5) < 1e-6


def test_min_charge_floor_scales_with_the_remaining_window(freezer) -> None:
    freezer.move_to(NOW)
    # 0.4 kWh of headroom left. A 0.5 kW floor over 30 min needs only
    # 0.25 kWh, so the top-up stays allowed mid-hour…
    battery = _battery(soc=98.0)
    plan = _optimizer(min_charge_power_kw=0.5).optimize(_forecast([0.10]), battery)
    assert plan.decisions[0].battery_charge_kwh > 1e-6


def test_ev_allocation_caps_the_running_hour(freezer) -> None:
    freezer.move_to(NOW)
    fc = _forecast([0.10])
    now_hour = fc.slots[0].start
    request = EVRequest(
        enabled=True,
        charger_kw=7.0,
        battery_kwh=60.0,
        current_soc=20.0,
        available_hours={now_hour},
        forced_hours={now_hour},
    )
    alloc = _optimizer()._plan_ev(fc, request, first_hour_frac=0.5)
    # Half the hour left → half the full-power yield, delivered in 30 minutes.
    assert abs(alloc.added[now_hour] - 3.5) < 1e-6
    assert alloc.minutes[now_hour] == 30
