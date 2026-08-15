"""The EV pack ceiling is a constraint on every *prefix* of the plan.

The car's level at any hour is ``start + charged so far − driven off so far``.
Energy planned above the ceiling is never delivered — the charger stops there —
yet it is still priced into the plan and still shows up as house load in the
LP, so an allocator that ignores the running level buys energy that goes
nowhere AND misses the target it was buying for.

The regression these pin down: the allocator credited the whole horizon's trip
drain as room that already existed, so a post-trip deficit could be bought in
the cheap hours *before* the trip, where the pack is still full.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.ev import EVChargeTarget, EVRequest
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

H0 = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)


def _h(i: int):
    return H0 + timedelta(hours=i)


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
        )
    )


def _levels(alloc, request: EVRequest, hours: int) -> list[float]:
    """Projected pack level (% SoC) at the end of each hour, unclamped.

    Deliberately does NOT clamp at the ceiling — that clamp is what used to
    hide the over-allocation on the SoC line while the bars showed it.
    """
    soc = request.current_soc / 100.0 * request.battery_kwh
    out = []
    for i in range(hours):
        soc = max(0.0, soc - request.drain_kwh.get(_h(i), 0.0))
        soc += alloc.added.get(_h(i), 0.0)
        out.append(soc / request.battery_kwh * 100.0)
    return out


# A 100 kWh pack at 70 % with a 90 % ceiling: 20 kWh of room. A 40 kWh trip
# drains at hour 10 and the car must be back at 90 % by hour 12. Charger is
# 10 kW at η=1.0, so a full hour adds exactly 10 kWh.
def _trip_request(**over) -> EVRequest:
    kwargs = dict(
        enabled=True,
        charger_kw=10.0,
        phases=1,
        battery_kwh=100.0,
        current_soc=70.0,
        charge_efficiency=1.0,
        charge_ceiling_soc=90.0,
        available_hours={_h(i) for i in range(13)} - {_h(10)},
        drain_kwh={_h(10): 40.0},
        targets=[EVChargeTarget(deadline=_h(12), target_soc=90.0, label="cel")],
    )
    kwargs.update(over)
    return EVRequest(**kwargs)


def test_pre_trip_charging_never_breaches_the_pack_ceiling() -> None:
    request = _trip_request()
    alloc = _optimizer()._plan_ev(_forecast([0.10] * 13), request)

    assert max(_levels(alloc, request, 13)) <= 90.0 + 1e-6


def test_pre_trip_charging_is_capped_at_the_room_that_exists() -> None:
    """Only 20 kWh fits before the trip, however cheap the hours are."""
    request = _trip_request()
    alloc = _optimizer()._plan_ev(_forecast([0.10] * 13), request)

    before_trip = sum(kwh for start, kwh in alloc.added.items() if start < _h(10))
    assert before_trip <= 20.0 + 1e-6


def test_post_trip_deficit_is_bought_after_the_trip() -> None:
    """The hour after the drain is where the energy physically fits."""
    request = _trip_request()
    alloc = _optimizer()._plan_ev(_forecast([0.10] * 13), request)

    assert alloc.added.get(_h(11), 0.0) > 0.0


def test_cheap_hours_still_win_within_the_room_that_exists() -> None:
    """Capping the pack must not cost the allocator its price sense.

    Hour 5 is the cheap one; the 20 kWh of pre-trip room belongs there and on
    the next-cheapest hour, not on whichever hour happens to come first.
    """
    prices = [0.90] * 13
    prices[5] = 0.10
    prices[7] = 0.20
    request = _trip_request()
    alloc = _optimizer()._plan_ev(_forecast(prices), request)

    assert alloc.added.get(_h(5), 0.0) > 9.0
    assert alloc.added.get(_h(7), 0.0) > 9.0


def test_trip_target_already_met_buys_nothing() -> None:
    """Drive drain in the hour the car LEAVES in happens after the deadline.

    A trip target's ``target_soc`` already covers the round trip (reserve +
    trip energy). Counting the departure hour's drain against it too charges
    the same journey twice: a car sitting above the target gets told to buy
    another packful before it leaves.
    """
    request = EVRequest(
        enabled=True,
        charger_kw=10.0,
        phases=1,
        battery_kwh=100.0,
        current_soc=74.0,
        charge_efficiency=1.0,
        charge_ceiling_soc=95.0,
        available_hours={_h(i) for i in range(6)},
        # Car leaves at 05:08 and drives for the rest of that hour.
        drain_kwh={_h(5): 20.0},
        targets=[
            EVChargeTarget(
                deadline=_h(5) + timedelta(minutes=8),
                target_soc=73.0,
                label="Wyjazd",
                source="trip",
            )
        ],
    )
    alloc = _optimizer()._plan_ev(_forecast([0.10] * 6), request)

    assert sum(alloc.added.values()) == 0.0


def test_ceiling_respected_without_any_trip() -> None:
    """Baseline: a plain top-up stops at the ceiling."""
    request = EVRequest(
        enabled=True,
        charger_kw=10.0,
        phases=1,
        battery_kwh=100.0,
        current_soc=70.0,
        charge_efficiency=1.0,
        charge_ceiling_soc=90.0,
        available_hours={_h(i) for i in range(6)},
        required_kwh=50.0,
    )
    alloc = _optimizer()._plan_ev(_forecast([0.10] * 6), request)

    assert sum(alloc.added.values()) <= 20.0 + 1e-6


def test_forced_hours_stop_at_the_ceiling_too() -> None:
    """A forced window can't push the pack past what it can hold either."""
    request = EVRequest(
        enabled=True,
        charger_kw=10.0,
        phases=1,
        battery_kwh=100.0,
        current_soc=70.0,
        charge_efficiency=1.0,
        charge_ceiling_soc=90.0,
        available_hours={_h(i) for i in range(6)},
        forced_hours={_h(0), _h(1), _h(2), _h(3)},
    )
    alloc = _optimizer()._plan_ev(_forecast([0.10] * 6), request)

    assert sum(alloc.added.values()) <= 20.0 + 1e-6
