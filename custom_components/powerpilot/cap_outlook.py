"""PPE 1's price-cap regime over replayed real price weeks (ADR 003).

The plan prices the hours without a source price — the horizon's estimated
hours and the month's tail — on one averaged path. Whether PPE 1's month binds
is judged instead on ``CAP_SCENARIOS`` scenarios: scenario ``k`` prices every
such hour with the ``k``-th latest certain price of its weekday and hour (the
``k``-th latest real week repeated), buy and cap price from that one archived
entry, the cap price under the hour's own edition. Forecast entries never feed
a scenario. Each usable scenario is solved with the plan's own inputs and
model as an LP relaxation over the regime combinations
(:meth:`Optimizer.cap_regimes`); the share of them in which a month binds is
its bind probability. A scenario missing any price is dropped and named.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import date, datetime

from homeassistant.util import dt as dt_util

from .const import CAP_SCENARIOS
from .models import (
    BillingPeriod,
    CapOutlook,
    CapScenario,
    Forecast,
    HourSlot,
    MonthTail,
    PriceCapTerms,
)
from .modules.prices import PriceArchive, cap_price_of
from .when import moment_label


@dataclass
class PriceScenario:
    """The plan's horizon and tail with replayed prices, or why it is dropped."""

    week: int
    forecast: Forecast | None = None
    tail: MonthTail | None = None
    week_start: date | None = None
    dropped_reason: str | None = None


def replay_scenarios(
    forecast: Forecast,
    tail: MonthTail | None,
    archive: PriceArchive,
    terms_on: Callable[[date], PriceCapTerms | None],
    count: int = CAP_SCENARIOS,
) -> list[PriceScenario]:
    """The ``count`` replayed-week scenarios of the plan's inputs.

    Replaced: horizon slots whose price is estimated (no source price) and
    every tail slot. ``terms_on`` gives the edition in force on a day (``None``:
    no cap, the slot keeps no cap price).
    """
    tail_slots = tail.slots if tail is not None else []
    scenarios: list[PriceScenario] = []
    for k in range(1, count + 1):
        missing: list[datetime] = []
        sampled: list[datetime] = []

        def replayed(slot: HourSlot, k: int = k) -> HourSlot:
            hour = slot.start
            buy, at = archive.replay(hour, k, lambda entry: entry.get("energy"))
            edition = terms_on(dt_util.as_local(hour).date())
            cap = None
            if edition is not None:
                cap, _ = archive.replay(
                    hour, k, lambda entry, edition=edition: cap_price_of(entry, edition)
                )
            if buy is None or (edition is not None and cap is None):
                missing.append(hour)
                return slot
            sampled.append(at)
            return replace(slot, buy_price=buy, cap_price_kwh=cap, tags=list(slot.tags))

        slots = [
            replayed(slot) if "price_estimated" in slot.tags else slot
            for slot in forecast.slots
        ]
        new_tail = [replayed(slot) for slot in tail_slots]
        week_start = min((dt_util.as_local(at).date() for at in sampled), default=None)
        if missing:
            scenarios.append(
                PriceScenario(
                    week=k,
                    week_start=week_start,
                    dropped_reason=(
                        f"tydzień {k}: brak {k}. pewnej ceny tej godziny tygodnia dla "
                        f"{len(missing)} h (od {moment_label(missing[0])})"
                    ),
                )
            )
            continue
        scenarios.append(
            PriceScenario(
                week=k,
                forecast=Forecast(slots=slots),
                tail=(
                    MonthTail(slots=new_tail, ev_grid_kwh=list(tail.ev_grid_kwh))
                    if tail is not None
                    else None
                ),
                week_start=week_start,
            )
        )
    return scenarios


def cap_outlooks(
    periods: list[BillingPeriod],
    scenarios: list[PriceScenario],
    regimes: Callable[[PriceScenario], list[tuple[BillingPeriod, float, float, bool]]],
) -> list[CapOutlook]:
    """Each PPE 1 period's month over the scenarios, one outlook per period.

    ``regimes`` solves one usable scenario (:meth:`Optimizer.cap_regimes`);
    it runs once per scenario, whatever the number of periods.
    """
    verdicts: dict[int, dict[datetime, tuple[float, float, bool]]] = {}
    for scenario in scenarios:
        if scenario.dropped_reason is None:
            verdicts[scenario.week] = {
                period.month_start: (energy, cost, binds)
                for period, energy, cost, binds in regimes(scenario)
            }
    outlooks: list[CapOutlook] = []
    for period in periods:
        entries: list[CapScenario] = []
        for scenario in scenarios:
            if scenario.dropped_reason is not None:
                entries.append(
                    CapScenario(
                        week=scenario.week,
                        week_start=scenario.week_start,
                        dropped_reason=scenario.dropped_reason,
                    )
                )
                continue
            energy, cost, binds = verdicts[scenario.week][period.month_start]
            entries.append(
                CapScenario(
                    week=scenario.week,
                    week_start=scenario.week_start,
                    average_kwh=cost / energy if energy > 0 else None,
                    binds=binds,
                )
            )
        outlooks.append(CapOutlook(month_start=period.month_start, scenarios=tuple(entries)))
    return outlooks
