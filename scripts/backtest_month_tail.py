"""Backtest the month-tail price estimate behind PPE 1's price-cap regime.

The plan settles each billing month as ``min(C, L·E)``; whether PPE 1's month
"binds" (costs ``L·E``) is decided from the observed month so far, the
planning horizon and the month's tail beyond it, whose prices are estimated
(``PriceArchive._estimate``). This script asks how well that decision would
have been made in past months, against what the month really came to.

For every as-of moment (local 12:00, after the next day's prices are
published) it plans PPE 1's month with the real :class:`Optimizer` and the
installation's battery: the horizon runs to the end of the next day on the
real prices, the tail to the month's end on prices from one of these methods:

* ``avg3`` — the integration's estimate: same weekday+hour weighted over the
  three latest archived weeks (0.5 / 0.3 / 0.2),
* ``week-k`` — one real archived week replayed as it happened (``k`` = 1 is
  the latest), no averaging,
* ``real`` — the prices that really came (perfect foresight).

Demand is the Victron's real load (perfect foresight, so only prices
differ) and the EV's real import is spread by the LP over the tail
(``MonthTail.ev_grid_kwh``). For each run it prints the month's forecast
average and whether the cap binds, next to the month's real average from the
meter.

Usage, from the repository root with the project's venv::

    .venv/bin/python scripts/backtest_month_tail.py export.json [--every 3]

``export.json`` comes from the ``powerpilot/series`` websocket command (past
hours) and holds consecutive UTC hours from ``first``::

    {"first": "2026-08-01T00:00:00+02:00",
     "gaps_after_utc": ["2026-10-25T00:00:00Z"],   # one missing hour after each
     "fields": ["price", "dist", "consumption", "ev", "grid", "ppe2"],
     "scale": 1000,
     "rows": ["951,102,373,0,0,", ...]}            # ints ÷ scale, "" = null

``price`` is ``buy_price`` (gross energy, equal to the cap price under the
configured edition), ``dist`` ``distribution_price_kwh``, ``consumption``
``consumption_real`` (the Victron's load), ``ev`` / ``grid`` / ``ppe2`` the
``realized`` EV, PPE 1 and PPE 2 imports. A missing hour (the series skips the
repeated hour of the autumn clock change) is filled with the hour before and
counted; a missing distribution price takes the most common one of its
weekday-type and hour, also counted.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.powerpilot.battery import BatteryModel  # noqa: E402
from custom_components.powerpilot.const import (  # noqa: E402
    ESTIMATE_WEEKLY_WEIGHTS,
    PPE_1,
)
from custom_components.powerpilot.models import (  # noqa: E402
    BillingPeriod,
    Forecast,
    HourSlot,
    MonthTail,
)
from custom_components.powerpilot.optimizer import (  # noqa: E402
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

LIMIT = 0.61
TZ = "Europe/Warsaw"
HOUR = timedelta(hours=1)
WEEK = timedelta(days=7)
REPLAY_WEEKS = 4
# How far back an estimate may look (``prices._ESTIMATE_MAX_WEEKS_BACK``).
MAX_WEEKS_BACK = 12

# The installation (HA options, 2026-11-02).
EV_CHARGER_KW = 11.04


def _optimizer() -> Optimizer:
    return Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=7.3,
            inverter_max_discharge_kw=11.0,
            charge_curve=ChargeCurve(
                default_kw=7.3,
                segments=[
                    {"soc_from": 0, "soc_to": 11, "max_kw": 6.0},
                    {"soc_from": 11, "soc_to": 71, "max_kw": 7.3},
                    {"soc_from": 71, "soc_to": 91, "max_kw": 7.0},
                    {"soc_from": 91, "soc_to": 101, "max_kw": 2.7},
                ],
            ),
            min_charge_power_kw=1.0,
            connection_power_kw=3 * 230 * 32 / 1000,
            phase_capacity_kw=230 * 32 / 1000,
            charge_efficiency_curve=[
                {"kw": 0.18, "eff": 0.711},
                {"kw": 0.394, "eff": 0.838},
                {"kw": 0.76, "eff": 0.895},
                {"kw": 1.228, "eff": 0.92},
                {"kw": 2.006, "eff": 0.927},
                {"kw": 2.78, "eff": 0.928},
                {"kw": 4.744, "eff": 0.919},
                {"kw": 6.0, "eff": 0.907},
                {"kw": 7.52, "eff": 0.886},
            ],
        )
    )


def _battery() -> BatteryModel:
    # The pack's real level at each as-of moment is not in the export; mid
    # band is a 15 kWh error at most against a month of ~700 kWh.
    return BatteryModel(
        capacity_kwh=30.0,
        charge_efficiency=0.92,
        discharge_efficiency=0.96,
        wear_cost=0.01,
        min_soc=6.0,
        max_soc=100.0,
        soc=50.0,
    )


class Hours:
    """The export as hourly records keyed by their UTC start."""

    def __init__(self, data: dict) -> None:
        scale = float(data["scale"])
        fields = data["fields"]
        first = dt_util.as_utc(datetime.fromisoformat(data["first"]))
        gaps = {dt_util.as_utc(datetime.fromisoformat(g)) for g in data.get("gaps_after_utc", [])}
        self.rows: dict[datetime, dict[str, float | None]] = {}
        self.filled_hours = 0
        moment = first
        previous: dict[str, float | None] | None = None
        for raw in data["rows"]:
            values = raw.split(",")
            row = {
                name: (int(values[i]) / scale if i < len(values) and values[i] != "" else None)
                for i, name in enumerate(fields)
            }
            self.rows[moment] = row
            previous = row
            if moment in gaps:
                moment += HOUR
                self.rows[moment] = dict(previous)
                self.filled_hours += 1
            moment += HOUR
        self.end = moment
        missing = [m for m, r in self.rows.items() if r["price"] is None]
        if missing:
            raise ValueError(f"{len(missing)} hours without a price, first {missing[0]}")
        # Prices already published past the last measured hour, and the rest
        # of that month without one: the running month's tail.
        self.prices_end = self.end
        for raw in data.get("future_prices", []):
            start, price, dist = raw.split(",")
            moment = dt_util.as_utc(datetime.fromisoformat(start))
            self.rows[moment] = self._future(moment, int(price) / scale, int(dist) / scale)
            self.prices_end = max(self.prices_end, moment + HOUR)
        _, month_end = _month(dt_util.as_local(self.end - HOUR))
        for moment in _hours(self.prices_end, month_end):
            self.rows[moment] = self._future(moment, None, None)
        days = 28
        self.ev_daily_kwh = sum(
            max(0.0, self.rows[m]["ev"] or 0.0)
            for m in _hours(self.end - days * 24 * HOUR, self.end)
        ) / days
        self.filled_dist = self._fill_distribution()

    def _future(self, moment: datetime, price: float | None, dist: float | None) -> dict:
        """An hour past the measurements: the loads of the latest same hour measured."""
        past = moment - WEEK
        while past >= self.end:
            past -= WEEK
        return {
            "price": price,
            "dist": dist,
            "consumption": self.rows[past]["consumption"],
            "ev": self.rows[past]["ev"],
            "grid": None,
            "ppe2": None,
        }

    def _fill_distribution(self) -> int:
        modes: dict[tuple[bool, int], Counter] = {}
        for moment, row in self.rows.items():
            if row["dist"] is not None:
                modes.setdefault(_daytype(moment), Counter())[row["dist"]] += 1
        filled = 0
        for moment, row in self.rows.items():
            if row["dist"] is None:
                row["dist"] = modes[_daytype(moment)].most_common(1)[0][0]
                filled += 1
        return filled

    def price(self, moment: datetime) -> float | None:
        return self.rows[moment]["price"]


def _daytype(moment: datetime) -> tuple[bool, int]:
    local = dt_util.as_local(moment)
    return local.weekday() < 5, local.hour


def _hours(start: datetime, end: datetime) -> list[datetime]:
    out = []
    moment = start
    while moment < end:
        out.append(moment)
        moment += HOUR
    return out


def avg3(hours: Hours, moment: datetime, known_end: datetime) -> float | None:
    """``PriceArchive._estimate`` over the archive as it stood at ``known_end``."""
    samples: list[float | None] = []
    weeks = 0
    while len(samples) < len(ESTIMATE_WEEKLY_WEIGHTS) and weeks < MAX_WEEKS_BACK:
        weeks += 1
        past = moment - weeks * WEEK
        if past >= known_end:
            continue  # past the archive's reach
        samples.append(hours.price(past) if past in hours.rows else None)
    weights = [
        (w, v) for w, v in zip(ESTIMATE_WEEKLY_WEIGHTS, samples) if v is not None
    ]
    total = sum(w for w, _ in weights)
    return sum(w * v for w, v in weights) / total if total > 0 else None


def week_k(k: int):
    """The ``k``-th latest archived week replayed hour for hour."""

    def method(hours: Hours, moment: datetime, known_end: datetime) -> float | None:
        found = 0
        for weeks in range(1, MAX_WEEKS_BACK + 1):
            past = moment - weeks * WEEK
            if past >= known_end or past not in hours.rows:
                continue
            found += 1
            if found == k:
                return hours.price(past)
        return None

    return method


def real(hours: Hours, moment: datetime, known_end: datetime) -> float | None:
    return hours.price(moment)


def _month(local: datetime) -> tuple[datetime, datetime]:
    start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    nxt = (start + timedelta(days=32)).replace(day=1)
    return dt_util.as_utc(start), dt_util.as_utc(nxt)


def settle_real(hours: Hours, start: datetime, end: datetime) -> tuple[float, float]:
    """PPE 1's real (energy, pre-cap cost) over ``[start, end)``."""
    energy = cost = 0.0
    for moment in _hours(start, end):
        row = hours.rows[moment]
        grid = row["grid"] or 0.0
        energy += grid
        cost += grid * row["price"]
    return energy, cost


def plan_month(
    hours: Hours,
    as_of: datetime,
    method,
    known_end: datetime | None = None,
    ev: str = "month",
) -> tuple[float, float, bool] | None:
    """PPE 1's month forecast at ``as_of`` with the tail priced by ``method``.

    The archive reaches ``known_end`` (default: the end of the next day).
    The EV's import over the tail (``ev``):

    * ``month`` — spread by the LP over the tail's cheapest hours, as the plan
      does (past the measurements: its daily average of the last 28 days, the
      horizon's share included),
    * ``day`` — each day's real energy in that day's cheapest hours,
    * ``hour`` — at the hours the car really charged,

    past the measurements taking the latest same hour measured, and priced at
    the tail's estimate. In the horizon the car charges at its real hours
    (``month``: only while measured).
    """
    local = dt_util.as_local(as_of)
    month_start, month_end = _month(local)
    if known_end is None:
        known_end = dt_util.as_utc(
            (local + timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        )
    if known_end >= month_end or (month_end - HOUR) not in hours.rows:
        return None
    energy, cost = settle_real(hours, month_start, as_of)
    slots = []
    tail_ev = 0.0
    for moment in _hours(as_of, known_end):
        row = hours.rows[moment]
        if moment >= hours.end and ev == "month":
            tail_ev += hours.ev_daily_kwh / 24
        else:
            kwh = max(0.0, row["ev"] or 0.0)
            energy += kwh
            cost += kwh * row["price"]
        slots.append(_slot(moment, row, row["price"]))
    tail_slots = []
    days: dict[object, list[tuple[float, float]]] = {}
    for moment in _hours(known_end, month_end):
        row = hours.rows[moment]
        price = method(hours, moment, known_end)
        if price is None:
            return None
        kwh = max(0.0, row["ev"] or 0.0)
        if ev == "month":
            tail_ev += hours.ev_daily_kwh / 24 if moment >= hours.end else kwh
        elif ev == "day":
            days.setdefault(dt_util.as_local(moment).date(), []).append((price, kwh))
        else:
            energy += kwh
            cost += kwh * price
        tail_slots.append(_slot(moment, row, price))
    for day_hours in days.values():
        left = sum(kwh for _, kwh in day_hours)
        for price, _ in sorted(day_hours):
            take = min(left, EV_CHARGER_KW)
            energy += take
            cost += take * price
            left -= take
            if left <= 0:
                break
    so_far = int((as_of - month_start) / HOUR)
    period = BillingPeriod(
        ppe=PPE_1,
        month_start=month_start,
        month_end=month_end,
        limit_kwh=LIMIT,
        energy_kwh=energy,
        cost_pln=cost,
        hours_expected=so_far,
        hours_covered=so_far,
    )
    dt_util.now = lambda time_zone=None: as_of
    plan = _optimizer().optimize(
        Forecast(slots=slots),
        _battery(),
        billing=[period],
        tail=MonthTail(slots=tail_slots, ev_grid_kwh=tail_ev, ev_charger_kw=EV_CHARGER_KW),
    )
    outcome = plan.billing[0]
    return outcome.energy_kwh, outcome.cost_pln, bool(outcome.cap_binds)


def _slot(moment: datetime, row: dict, price: float) -> HourSlot:
    return HourSlot(
        start=moment,
        buy_price=price,
        price_confirmed=True,
        cap_price_kwh=price,
        distribution_price_kwh=row["dist"],
        distribution_fixed_hourly=0.0,
        base_consumption_kwh=max(0.0, row["consumption"] or 0.0),
    )


def _fmt(result: tuple[float, float, bool] | None) -> str:
    if result is None:
        return "     —      "
    energy, cost, binds = result
    return f"{cost / energy:5.3f} {'BIND' if binds else 'pod '}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("export", type=Path)
    parser.add_argument("--every", type=int, default=3, help="days between as-of moments")
    parser.add_argument("--first", default=None, help="first as-of day, YYYY-MM-DD")
    parser.add_argument(
        "--ev",
        choices=("month", "day", "hour"),
        default="month",
        help="where the EV's tail import goes (see plan_month)",
    )
    args = parser.parse_args()
    dt_util.set_default_time_zone(dt_util.get_time_zone(TZ))
    hours = Hours(json.loads(args.export.read_text()))
    print(
        f"{len(hours.rows)} h; filled: {hours.filled_hours} missing hour(s), "
        f"{hours.filled_dist} distribution price(s)"
    )

    first_local = dt_util.as_local(min(hours.rows))
    months = sorted({(dt_util.as_local(m).year, dt_util.as_local(m).month) for m in hours.rows})
    print("\nReal PPE 1 months (meter, pre-cap average):")
    for year, month in months:
        start, end = _month(datetime(year, month, 1, tzinfo=dt_util.DEFAULT_TIME_ZONE))
        end = min(end, hours.end)
        if start < min(hours.rows):
            continue
        energy, cost = settle_real(hours, start, end)
        complete = "" if end == _month(datetime(year, month, 1, tzinfo=dt_util.DEFAULT_TIME_ZONE))[1] else " (incomplete)"
        print(f"  {year}-{month:02d}: {energy:6.1f} kWh @ {cost / energy:5.3f}{complete}")

    methods = [("avg3", avg3)] + [(f"week-{k}", week_k(k)) for k in range(1, REPLAY_WEEKS + 1)]
    methods.append(("real", real))
    start_day = (
        datetime.fromisoformat(args.first).replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        if args.first
        else (first_local + timedelta(days=21)).replace(hour=0, minute=0)
    )
    print(
        "\nas-of (12:00)    real month   "
        + "  ".join(f"{name:^12}" for name, _ in methods)
    )
    day = start_day
    last = dt_util.as_local(hours.end - HOUR)
    while day <= last:
        as_of = dt_util.as_utc(day.replace(hour=12))
        local = dt_util.as_local(as_of)
        month_start, month_end = _month(local)
        if month_end <= hours.end:
            energy, cost = settle_real(hours, month_start, month_end)
            truth = f"{cost / energy:5.3f} {'BIND' if cost > LIMIT * energy else 'pod '}"
        else:
            truth = "  (trwa)  "
        results = [
            plan_month(hours, as_of, method, ev=args.ev) for _, method in methods
        ]
        print(
            f"{local:%a %d.%m}        {truth}  "
            + "  ".join(f"{_fmt(r):^12}" for r in results),
            flush=True,
        )
        day += timedelta(days=args.every)

    # The running month from the last measured hour, with every price
    # published so far (forecasts included) in the archive, as the plan has.
    as_of = hours.end
    results = [
        plan_month(hours, as_of, method, known_end=hours.prices_end, ev=args.ev)
        for _, method in methods
    ]
    print(
        f"{dt_util.as_local(as_of):%a %d.%m %H:%M}    (trwa)     "
        + "  ".join(f"{_fmt(r):^12}" for r in results)
    )
    print(f"  EV past the measurements: {hours.ev_daily_kwh:.1f} kWh/day")


if __name__ == "__main__":
    main()
