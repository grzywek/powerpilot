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

import pytest
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


# ---------------------------------------------------------------------------
# Earliness outranks contiguity, day before hour ("charge Saturday before
# Sunday"). Two-day forecasts anchored to a real local-midnight boundary.
# ---------------------------------------------------------------------------

BASE = dt_util.start_of_local_day(dt_util.now()) + timedelta(days=1, hours=8)


def _two_day_forecast(day_a: list[float], day_b: list[float]) -> Forecast:
    """Consecutive hours from BASE (day A) and from BASE+1 day (day B)."""
    slots = [
        HourSlot(
            start=BASE + timedelta(hours=i),
            buy_price=p,
            distribution_price_kwh=0.0,
            base_consumption_kwh=0.0,
        )
        for i, p in enumerate(day_a)
    ]
    slots += [
        HourSlot(
            start=BASE + timedelta(days=1, hours=i),
            buy_price=p,
            distribution_price_kwh=0.0,
            base_consumption_kwh=0.0,
        )
        for i, p in enumerate(day_b)
    ]
    return Forecast(slots=slots)


def _two_day_request(
    forecast: Forecast, required_kwh: float = 2.0, **prefs
) -> EVRequest:
    return EVRequest(
        enabled=True,
        required_kwh=required_kwh,
        charger_kw=1.0,
        phases=1,
        battery_kwh=10.0,
        current_soc=0.0,
        available_hours={s.start for s in forecast.slots},
        **prefs,
    )


def _two_day_hours(allocation: dict) -> set[int]:
    return {
        int((start - BASE).total_seconds() // 3600) for start in allocation
    }


def test_early_before_contiguous_keeps_todays_cheap_hour() -> None:
    """Regression: a contiguous block on the NEXT day must not outbid a
    still-cheap hour today (the 2026-07-18 "plan jumped to tomorrow" bug).

    Day A has one cheap hour (0.21) then an expensive evening; day B has a
    contiguous 0.24/0.24 block. The scattered optimum {A0, B0} costs 0.45,
    the all-B block 0.48 (+6.7 % — inside the old 15 % contiguity budget,
    which used to move ALL charging to day B). With earliness applied first
    at a 0 % budget, only cost-optimal placements survive, so the cheap
    hour today stays in the plan.
    """
    forecast = _two_day_forecast([0.21, 0.60], [0.24, 0.24])
    alloc = _optimizer()._plan_ev(
        forecast,
        _two_day_request(
            forecast,
            prefer_contiguous=True,
            contiguous_max_extra_pct=15.0,
            prefer_early=True,
            early_max_extra_pct=0.0,
        ),
    )
    assert _two_day_hours(alloc) == {0, 24}


def test_early_day_outranks_cheaper_next_day_within_budget() -> None:
    # Day A costs 0.22, day B 0.20 — within the 15 % early budget the plan
    # finishes a whole day sooner ("Saturday before Sunday").
    forecast = _two_day_forecast([0.11, 0.11], [0.10, 0.10])
    alloc = _optimizer()._plan_ev(
        forecast,
        _two_day_request(forecast, prefer_early=True, early_max_extra_pct=15.0),
    )
    assert _two_day_hours(alloc) == {0, 1}


def test_early_day_yields_when_next_day_is_cheaper_beyond_budget() -> None:
    # Day A is 3× the price of day B — far over the 10 % early budget, so
    # the genuinely cheaper day wins despite being later.
    forecast = _two_day_forecast([0.30, 0.30], [0.10, 0.10])
    alloc = _optimizer()._plan_ev(
        forecast,
        _two_day_request(forecast, prefer_early=True, early_max_extra_pct=10.0),
    )
    assert _two_day_hours(alloc) == {24, 25}


def test_contiguity_still_rearranges_within_the_day() -> None:
    # Within the chosen (earliest) day the contiguity budget still closes
    # gaps: {A0, A1} (+9.5 %) beats the scattered {A0, A2} optimum.
    forecast = _two_day_forecast([0.10, 0.13, 0.11], [0.30, 0.30])
    alloc = _optimizer()._plan_ev(
        forecast,
        _two_day_request(
            forecast,
            prefer_contiguous=True,
            contiguous_max_extra_pct=15.0,
            prefer_early=True,
            early_max_extra_pct=10.0,
        ),
    )
    assert _two_day_hours(alloc) == {0, 1}


def _day_a_energy(allocation) -> float:
    """Energy allocated on day A (the earlier of the two forecast days)."""
    midnight = dt_util.start_of_local_day(BASE) + timedelta(days=1)
    return sum(kwh for start, kwh in allocation.items() if start < midnight)


def test_early_keeps_todays_cheap_hours_when_the_block_no_longer_fits() -> None:
    """Regression: earliness must front-LOAD, not merely finish early.

    Day A has 2 cheap hours left, day B a 4-hour cheap run, and 4 hours of
    charging are needed — so no placement fits on day A any more. The old
    "earliest finishing day" filter therefore kept every variant (they all
    finish on day B) and contiguity moved the whole session to day B, leaving
    day A's equally cheap hours unused (the 2026-07-25 report). The split
    {A0, A1, B0, B1} and the block {B0..B3} cost exactly the same, so the
    one that charges earlier has to win.
    """
    forecast = _two_day_forecast([0.20, 0.20, 0.60, 0.60], [0.20, 0.20, 0.20, 0.20])
    alloc = _optimizer()._plan_ev(
        forecast,
        _two_day_request(
            forecast,
            required_kwh=4.0,
            prefer_contiguous=True,
            contiguous_max_extra_pct=5.0,
            prefer_early=True,
            early_max_extra_pct=5.0,
        ),
    )
    assert _two_day_hours(alloc) == {0, 1, 24, 25}


def test_early_budget_is_monotone() -> None:
    """A bigger early budget may only move energy EARLIER, never later.

    Under the finish-day filter this knob was non-monotone — with a cheap run
    long enough to hold the whole session, a *larger* tolerance admitted the
    marginally dearer contiguous block on day B and handed the plan to
    contiguity, so raising "prefer early" pushed the charging later. Here the
    budget must only ever buy more of day A.
    """
    forecast = _two_day_forecast([0.20, 0.20, 0.60, 0.60], [0.20, 0.20, 0.20, 0.20])
    early = [
        _day_a_energy(
            _optimizer()._plan_ev(
                forecast,
                _two_day_request(
                    forecast,
                    required_kwh=4.0,
                    prefer_contiguous=True,
                    contiguous_max_extra_pct=5.0,
                    prefer_early=True,
                    early_max_extra_pct=pct,
                ),
            )
        )
        for pct in (0.0, 5.0, 15.0)
    ]
    assert early == sorted(early)
    # 0 % already has day A's two cheap hours for free — never fewer.
    assert early[0] == pytest.approx(2.0)


def test_full_allocation_energy_is_preserved() -> None:
    alloc = _optimizer()._plan_ev(
        _forecast([0.10, 0.13, 0.11]),
        _request(3, prefer_contiguous=True, contiguous_max_extra_pct=15.0),
    )
    assert abs(sum(alloc.values()) - 2.0) < 1e-6
