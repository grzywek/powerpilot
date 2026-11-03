"""PPE 1's cap regime over replayed real price weeks (ADR 003).

On 2026-11-02 the plan gave PPE 1's November up on one averaged tail price
path taken as certain; single real weeks disagree near the limit (October: 3
of 32 replays bound, the month ended at 0.562). The regime is now the share of
four replayed real weeks in which the month binds.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.cap_outlook import (
    PriceScenario,
    cap_outlooks,
    replay_scenarios,
)
from custom_components.powerpilot.const import (
    PPE_1,
    PRICE_TYPE_CERTAIN,
    PRICE_TYPE_FORECAST,
)
from custom_components.powerpilot.models import (
    BillingPeriod,
    CapOutlook,
    CapScenario,
    Forecast,
    HourSlot,
    MonthTail,
    PriceCapTerms,
)
from custom_components.powerpilot.modules.prices import PriceArchive
from custom_components.powerpilot.optimizer import ChargeCurve, Optimizer, OptimizerConfig

H0 = dt_util.now().replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
WEEK = timedelta(days=7)
LIMIT = 0.61
TERMS = PriceCapTerms(
    name="Tarcza",
    valid_from=date(2025, 1, 1),
    valid_to=date(2099, 12, 31),
    limit_kwh=LIMIT,
    handling_fee_included=False,
)


def _archive(prices: dict[datetime, float], kind: str = PRICE_TYPE_CERTAIN) -> PriceArchive:
    archive = PriceArchive()
    for hour, price in prices.items():
        # Cap price = tge × 1.0 here (no fee in the average, no VAT): energy
        # carries a 0.10 markup the cap does not count.
        archive.record(
            hour,
            price + 0.10,
            kind,
            "pradcast",
            "t",
            formula={"tge": price, "markup": 0.10, "vat": 0.0},
        )
    return archive


def _slot(start: datetime, price: float | None = None, estimated: bool = False) -> HourSlot:
    return HourSlot(
        start=start,
        buy_price=price,
        cap_price_kwh=price,
        base_consumption_kwh=1.0,
        tags=["price_estimated"] if estimated else [],
    )


def _weeks(hours: list[datetime], weeks: int, price_of) -> dict[datetime, float]:
    return {h - k * WEEK: price_of(k) for h in hours for k in range(1, weeks + 1)}


def test_only_hours_without_a_source_price_are_replayed() -> None:
    sourced = _slot(H0, 0.40)
    estimated = _slot(H0 + timedelta(hours=1), 0.55, estimated=True)
    tail_hour = H0 + timedelta(hours=2)
    archive = _archive(_weeks([estimated.start, tail_hour], 4, lambda k: 0.2 * k))
    scenarios = replay_scenarios(
        Forecast(slots=[sourced, estimated]),
        MonthTail(slots=[_slot(tail_hour, 0.55, estimated=True)], ev_grid_kwh=[3.0]),
        archive,
        lambda day: TERMS,
    )
    assert [s.week for s in scenarios] == [1, 2, 3, 4]
    second = scenarios[1]
    assert second.dropped_reason is None
    # The source price stays; buy and cap price come from the same 2nd-latest
    # certain entry, the cap price under the hour's own edition.
    assert second.forecast.slots[0].buy_price == 0.40
    assert second.forecast.slots[1].buy_price == pytest.approx(0.50)
    assert second.forecast.slots[1].cap_price_kwh == pytest.approx(0.40)
    assert second.tail.slots[0].buy_price == pytest.approx(0.50)
    assert second.tail.ev_grid_kwh == [3.0]
    assert second.week_start == dt_util.as_local(estimated.start - 2 * WEEK).date()
    # The plan's own slots are untouched.
    assert estimated.buy_price == 0.55


def test_a_scenario_missing_a_price_is_dropped_and_named() -> None:
    tail = [_slot(H0 + timedelta(hours=i), 0.5, estimated=True) for i in range(3)]
    prices = _weeks([s.start for s in tail], 4, lambda k: 0.3)
    del prices[tail[2].start - 4 * WEEK]  # week 4 lacks one hour
    scenarios = replay_scenarios(
        Forecast(slots=[_slot(H0 - timedelta(hours=1), 0.5)]),
        MonthTail(slots=tail),
        _archive(prices),
        lambda day: TERMS,
    )
    assert [s.dropped_reason is None for s in scenarios] == [True, True, True, False]
    assert "tydzień 4" in scenarios[3].dropped_reason
    assert "1 h" in scenarios[3].dropped_reason


def test_a_forecast_entry_never_feeds_a_scenario() -> None:
    tail = [_slot(H0, 0.5, estimated=True)]
    prices = _weeks([H0], 4, lambda k: 0.3)
    archive = _archive(prices)
    archive.record(H0 - 5 * WEEK, 9.0, PRICE_TYPE_FORECAST, "pradcast", "t")
    archive_forecasts = _archive(prices, kind=PRICE_TYPE_FORECAST)
    assert all(
        s.dropped_reason is None
        for s in replay_scenarios(Forecast(slots=[]), MonthTail(slots=tail), archive, lambda d: TERMS)
    )
    assert all(
        s.dropped_reason is not None
        for s in replay_scenarios(
            Forecast(slots=[]), MonthTail(slots=tail), archive_forecasts, lambda d: TERMS
        )
    )


def test_an_hour_no_edition_covers_needs_no_cap_price() -> None:
    tail = [_slot(H0, 0.5, estimated=True)]
    tail[0].cap_price_kwh = None
    archive = PriceArchive()
    for k in range(1, 5):
        archive.record(H0 - k * WEEK, 0.4, PRICE_TYPE_CERTAIN, "sensor", "t")  # no formula
    scenarios = replay_scenarios(Forecast(slots=[]), MonthTail(slots=tail), archive, lambda d: None)
    assert all(s.dropped_reason is None for s in scenarios)
    assert scenarios[0].tail.slots[0].cap_price_kwh is None
    # Under an edition the same entries cannot be settled: dropped, not guessed.
    scenarios = replay_scenarios(Forecast(slots=[]), MonthTail(slots=tail), archive, lambda d: TERMS)
    assert all(s.dropped_reason is not None for s in scenarios)


def _period(month_start: datetime) -> BillingPeriod:
    return BillingPeriod(
        ppe=PPE_1,
        month_start=month_start,
        month_end=month_start + timedelta(days=30),
        limit_kwh=LIMIT,
    )


def test_two_of_four_binding_weeks_give_one_half() -> None:
    november, december = _period(H0), _period(H0 + timedelta(days=30))
    scenarios = [PriceScenario(week=k) for k in range(1, 5)]
    calls: list[int] = []

    def regimes(scenario):
        calls.append(scenario.week)
        binds = scenario.week in (1, 3)
        return [(november, 100.0, 80.0 if binds else 40.0, binds), (december, 10.0, 1.0, False)]

    outlooks = cap_outlooks([november, december], scenarios, regimes)
    assert calls == [1, 2, 3, 4]  # one solve per scenario, whatever the months
    assert outlooks[0].bind_probability == 0.5
    assert [s.average_kwh for s in outlooks[0].scenarios] == [0.8, 0.4, 0.8, 0.4]
    assert outlooks[1].bind_probability == 0.0


def test_probability_is_over_the_usable_scenarios_only() -> None:
    november = _period(H0)
    scenarios = [PriceScenario(week=1), PriceScenario(week=2, dropped_reason="tydzień 2: brak")]
    outlook = cap_outlooks(
        [november], scenarios, lambda s: [(november, 10.0, 8.0, True)]
    )[0]
    assert outlook.bind_probability == 1.0
    assert outlook.scenarios[1].dropped_reason == "tydzień 2: brak"


def test_no_usable_scenario_leaves_no_probability_and_says_why() -> None:
    outlook = CapOutlook(
        month_start=H0,
        scenarios=tuple(
            CapScenario(week=k, dropped_reason=f"tydzień {k}: brak") for k in range(1, 5)
        ),
    )
    assert outlook.bind_probability is None
    assert "żaden z 4" in outlook.unavailable_reason
    assert "tydzień 3: brak" in outlook.unavailable_reason


# ---------------------------------------------------------------- relaxation


def _battery() -> BatteryModel:
    return BatteryModel(
        capacity_kwh=10.0,
        charge_efficiency=1.0,
        discharge_efficiency=1.0,
        wear_cost=0.0,
        min_soc=0.0,
        max_soc=100.0,
        soc=0.0,
        energy_cost=0.0,
    )


def _optimizer() -> Optimizer:
    return Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=3.0,
            inverter_max_discharge_kw=3.0,
            charge_curve=ChargeCurve(default_kw=3.0),
            terminal_price=0.0,
        )
    )


@pytest.mark.parametrize(
    ("observed_cost", "binds"), [(100.0, True), (20.0, False)]
)
def test_the_relaxed_regime_follows_the_month(observed_cost: float, binds: bool) -> None:
    prices = [0.2, 0.2, 1.0, 1.0]
    forecast = Forecast(slots=[_slot(H0 + timedelta(hours=i), p) for i, p in enumerate(prices)])
    period = BillingPeriod(
        ppe=PPE_1,
        month_start=H0,
        month_end=H0 + timedelta(days=60),
        limit_kwh=LIMIT,
        energy_kwh=100.0,
        cost_pln=observed_cost,
        hours_expected=1,
        hours_covered=1,
    )
    optimizer = _optimizer()
    plan = optimizer.optimize(forecast, _battery(), billing=[period])
    solves = list(optimizer.last_solve_seconds)
    [(got, energy, cost, relaxed_binds)] = optimizer.cap_regimes(
        forecast, _battery(), billing=[period]
    )
    assert got is period
    assert relaxed_binds is binds
    assert plan.billing[0].cap_binds is binds
    assert energy == pytest.approx(plan.billing[0].energy_kwh)
    # The relaxation leaves the plan's diagnostics alone.
    assert optimizer.last_solve_seconds == solves
