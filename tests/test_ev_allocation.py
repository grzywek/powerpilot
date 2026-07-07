"""EV charging-hour placement preferences in the allocator (``_plan_ev``).

Baseline is pure cost (cheapest hours win, gaps allowed). The preference
options trade a bounded % of extra cost for an unbroken block
(``prefer_contiguous`` + ``contiguous_max_extra_pct``) or an earlier finish
(``prefer_early`` + ``early_max_extra_pct``).

Canonical example (from the feature request): prices 0.10 / 0.13 / 0.11 for
three consecutive hours, two hours of charging needed. The cheapest split is
{0.10, 0.11} with a gap; with contiguity preferred and a 15 % tolerance the
block {0.10, 0.13} wins (9.5 % dearer); with a 5 % tolerance the gap stays.
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


def _request(n_hours: int, **prefs) -> EVRequest:
    return EVRequest(
        enabled=True,
        required_kwh=2.0,
        charger_kw=1.0,
        phases=1,
        battery_kwh=10.0,
        current_soc=0.0,
        available_hours={H0 + timedelta(hours=i) for i in range(n_hours)},
        **prefs,
    )


def _optimizer() -> Optimizer:
    return Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=3.0,
            inverter_max_discharge_kw=3.0,
            charge_curve=ChargeCurve(default_kw=3.0),
        )
    )


def _hours(allocation: dict) -> set[int]:
    return {int((start - H0).total_seconds() // 3600) for start in allocation}


def test_baseline_picks_cheapest_hours_with_gap() -> None:
    alloc = _optimizer()._plan_ev(_forecast([0.10, 0.13, 0.11]), _request(3))
    assert _hours(alloc) == {0, 2}


def test_contiguous_within_tolerance_closes_the_gap() -> None:
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.13, 0.11]),
        _request(3, prefer_contiguous=True, contiguous_max_extra_pct=15.0),
    )
    # Block {0.10, 0.13} costs 0.23 vs the split's 0.21 → +9.5 % ≤ 15 %.
    assert _hours(alloc) == {0, 1}


def test_contiguous_over_tolerance_keeps_the_gap() -> None:
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.13, 0.11]),
        _request(3, prefer_contiguous=True, contiguous_max_extra_pct=5.0),
    )
    # +9.5 % > 5 % → the price difference justifies the gap.
    assert _hours(alloc) == {0, 2}


def test_contiguous_picks_cheapest_block() -> None:
    alloc = _optimizer()._plan_ev(
        _forecast([0.30, 0.10, 0.11, 0.30]),
        _request(4, prefer_contiguous=True, contiguous_max_extra_pct=15.0),
    )
    # Cheapest hours are already an unbroken block → no extra cost at all.
    assert _hours(alloc) == {1, 2}


def test_early_within_tolerance_finishes_sooner() -> None:
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.13, 0.11]),
        _request(3, prefer_early=True, early_max_extra_pct=10.0),
    )
    # Finishing at hour 1 costs 0.23 vs 0.21 → +9.5 % ≤ 10 % → earlier wins.
    assert _hours(alloc) == {0, 1}


def test_early_over_tolerance_stays_cheapest() -> None:
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.13, 0.11]),
        _request(3, prefer_early=True, early_max_extra_pct=5.0),
    )
    assert _hours(alloc) == {0, 2}


def test_full_allocation_energy_is_preserved() -> None:
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.13, 0.11]),
        _request(3, prefer_contiguous=True, contiguous_max_extra_pct=15.0),
    )
    assert abs(sum(alloc.values()) - 2.0) < 1e-6
