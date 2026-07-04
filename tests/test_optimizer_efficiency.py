"""Charge-efficiency curve, EV/ESS co-charging and earliness tie-break.

Three behaviours added on top of the base LP:

* the battery may charge **alongside** the EV — the cap during EV hours comes
  from real electrics (phase fuse minus the EV charger's per-phase draw), not
  from blanket-subtracting the EV power from the inverter limit (which zeroed
  battery charging whenever the EV charger was at least as strong);
* among equally priced hours the LP charges the **earliest** ones, so a
  shortfall extends into the remaining cheap hours instead of expensive ones;
* a power→efficiency curve turns stored energy into a concave piecewise-linear
  function of charge power, so the LP spreads charging over the efficiency
  sweet spot instead of always slamming full power.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.ev import EVRequest
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

H0 = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _forecast(prices: list[float], demand: float = 0.0) -> Forecast:
    return Forecast(
        slots=[
            HourSlot(
                start=H0 + timedelta(hours=i),
                buy_price=p,
                distribution_price_kwh=0.0,
                base_consumption_kwh=demand,
            )
            for i, p in enumerate(prices)
        ]
    )


def _battery(capacity: float = 10.0, soc: float = 0.0) -> BatteryModel:
    return BatteryModel(
        capacity_kwh=capacity,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        wear_cost=0.0,
        min_soc=0.0,
        max_soc=100.0,
        soc=soc,
        energy_cost=0.0,
    )


def _config(**overrides) -> OptimizerConfig:
    kwargs = dict(
        inverter_max_charge_kw=3.0,
        inverter_max_discharge_kw=3.0,
        grid_disconnect_soc=0.0,
        charge_curve=ChargeCurve(default_kw=3.0),
        terminal_price=2.0,  # high → filling the pack is always worthwhile
    )
    kwargs.update(overrides)
    return OptimizerConfig(**kwargs)


def _ev(charger_kw: float = 3.5) -> EVRequest:
    return EVRequest(
        enabled=True,
        required_kwh=charger_kw,  # exactly one full-power hour
        charger_kw=charger_kw,
        phases=1,
        battery_kwh=50.0,
        current_soc=0.0,
        available_hours={H0, H0 + timedelta(hours=1)},
    )


def test_battery_charges_alongside_ev_with_phase_headroom() -> None:
    # Cheap hour 0 gets the EV; the phase fuse (7.36 kW) leaves 3.86 kW of
    # headroom next to the 3.5 kW charger → the battery charges there too.
    plan = Optimizer(_config(phase_capacity_kw=7.36)).optimize(
        _forecast([0.10, 0.50]), _battery(), _ev()
    )
    assert plan.decisions[0].ev_charge_kwh > 3.0
    assert plan.decisions[0].battery_charge_kwh > 2.9


def test_without_fuse_data_legacy_subtraction_splits_hours() -> None:
    # No phase capacity → legacy cap max(0, 3 − 3.5) = 0: battery can't join
    # the EV hour and is pushed to the pricier one.
    plan = Optimizer(_config(phase_capacity_kw=0.0)).optimize(
        _forecast([0.10, 0.50]), _battery(), _ev()
    )
    assert plan.decisions[0].battery_charge_kwh == 0.0
    assert plan.decisions[1].battery_charge_kwh > 2.9


def test_equal_prices_charge_earliest_hour() -> None:
    # Only ~one hour of headroom; three equally cheap hours → pick the first.
    plan = Optimizer(_config()).optimize(
        _forecast([0.10, 0.10, 0.10]), _battery(capacity=3.0)
    )
    charges = [d.battery_charge_kwh for d in plan.decisions]
    assert charges[0] > 2.9
    assert charges[1] < 0.01 and charges[2] < 0.01


def test_efficiency_segments_flat_without_curve() -> None:
    segs = Optimizer(_config())._efficiency_segments(0.95)
    assert segs == [(3.0, 0.95)]


def test_efficiency_segments_concave_from_curve() -> None:
    opt = Optimizer(
        _config(
            inverter_max_charge_kw=6.0,
            charge_efficiency_curve=[
                {"kw": 3.0, "eff": 0.93},
                {"kw": 6.0, "eff": 0.80},
            ],
        )
    )
    segs = opt._efficiency_segments(0.95)
    widths = [w for w, _ in segs]
    effs = [e for _, e in segs]
    assert abs(sum(widths) - 6.0) < 1e-9
    # Marginal efficiencies non-increasing (concave hull) and ≤ 1.
    assert all(effs[i] >= effs[i + 1] for i in range(len(effs) - 1))
    assert all(e <= 1.0 for e in effs)
    # First band is the 93 % sweet spot; the tail marginal is (4.8−2.79)/3.
    assert abs(effs[0] - 0.93) < 1e-6
    assert abs(effs[-1] - 0.67) < 1e-6


def test_curve_reduces_stored_energy_at_full_power() -> None:
    # One cheap hour, full 6 kW draw → stored is 4.8 kWh (6 × 0.80 average),
    # not 6 × flat η. Verifies the LP and the replay agree on the curve.
    plan = Optimizer(
        _config(
            inverter_max_charge_kw=6.0,
            charge_efficiency_curve=[
                {"kw": 3.0, "eff": 0.93},
                {"kw": 6.0, "eff": 0.80},
            ],
        )
    ).optimize(_forecast([0.10]), _battery(capacity=20.0))
    d = plan.decisions[0]
    assert abs(d.battery_charge_kwh - 4.8) < 0.05
    assert abs(d.charge_power_kw - 6.0) < 0.05


def test_surplus_above_sweet_spot_lands_in_earliest_hours() -> None:
    # Three equally cheap hours, pack fits 12 kWh stored. The sweet-spot band
    # (3 kW @ 93 %) fills in ALL hours first (8.37 kWh stored); the remaining
    # 3.63 kWh must use the 0.67-marginal tail — and the tie-break puts that
    # surplus in the EARLIEST hours, so powers descend over time and any
    # shortfall later can still extend into a cheap hour. Regression: with
    # HiGHS' default MIP gap the near-ties resolved arbitrarily and the
    # surplus landed in random hours.
    plan = Optimizer(
        _config(
            inverter_max_charge_kw=6.0,
            charge_efficiency_curve=[
                {"kw": 3.0, "eff": 0.93},
                {"kw": 6.0, "eff": 0.80},
            ],
        )
    ).optimize(_forecast([0.10, 0.10, 0.10]), _battery(capacity=12.0))
    kw = [d.charge_power_kw for d in plan.decisions]
    # h0 slams full power, h1 takes the rest of the tail, h2 stays at the
    # sweet spot: 6.0 / ~5.42 / 3.0.
    assert abs(kw[0] - 6.0) < 0.05
    assert abs(kw[1] - 5.42) < 0.1
    assert abs(kw[2] - 3.0) < 0.05
    assert kw[0] >= kw[1] >= kw[2]  # descending in time
    stored_total = sum(d.battery_charge_kwh for d in plan.decisions)
    assert abs(stored_total - 12.0) < 0.05


def test_curve_spreads_charging_over_sweet_spot() -> None:
    # Two equally cheap hours, pack fits 6 kWh: filling one hour at 6 kW wastes
    # energy in the 0.67-marginal tail, so the LP uses both hours' 93 % bands.
    plan = Optimizer(
        _config(
            inverter_max_charge_kw=6.0,
            charge_efficiency_curve=[
                {"kw": 3.0, "eff": 0.93},
                {"kw": 6.0, "eff": 0.80},
            ],
        )
    ).optimize(_forecast([0.10, 0.10]), _battery(capacity=6.0))
    kw = [d.charge_power_kw for d in plan.decisions]
    assert kw[0] > 0.5 and kw[1] > 0.5  # both hours participate
    assert max(kw) < 4.5  # nobody slams full 6 kW
    stored_total = sum(d.battery_charge_kwh for d in plan.decisions)
    assert abs(stored_total - 6.0) < 0.05
