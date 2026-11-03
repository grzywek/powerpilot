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
differ); where the car's import goes in the tail is ``--ev`` (see
:func:`plan_month`), ``profile`` being the integration's own rule. For each
run it prints the month's forecast average and whether the cap binds, next to
the month's real average from the meter.

Usage, from the repository root with the project's venv::

    .venv/bin/python scripts/backtest_month_tail.py export.json [--every 3] \
        [--ev profile] [--first 2026-09-01]

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
``realized`` EV, PPE 1 and PPE 2 imports. ``ev`` is the car's energy-added
counter (pack side); PPE 1's ``grid`` holds it divided by the EV charge
efficiency, so every EV kWh here is divided by ``--ev-efficiency`` (the
installation's 0.91 by default; 1 reproduces the table of 2026-11-02). A missing hour (the series skips the
repeated hour of the autumn clock change) is filled with the hour before and
counted; a missing distribution price takes the most common one of its
weekday-type and hour, also counted.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.powerpilot.battery import BatteryModel  # noqa: E402
from custom_components.powerpilot.const import (  # noqa: E402
    CONF_EV_ENERGY_ADDED_SENSOR,
    ESTIMATE_WEEKLY_WEIGHTS,
    PPE_1,
)
from custom_components.powerpilot.models import (  # noqa: E402
    BillingPeriod,
    Forecast,
    HourSlot,
    MonthTail,
)
from custom_components.powerpilot.modules.ev import EVModule  # noqa: E402
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

# The installation (HA options, 2026-11-02; the EV charge efficiency from the
# debug export of 2026-09-15).
EV_CHARGER_KW = 11.04
EV_CHARGE_EFFICIENCY = 0.91


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
        # The measured EV keyed like the recorder's hourly reader (local hour;
        # the autumn clock change's repeated hour keeps the later one).
        self.ev_local = {
            dt_util.as_local(moment).replace(minute=0): row["ev"]
            for moment, row in sorted(self.rows.items())
            if moment < self.end and row["ev"] is not None
        }

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
    efficiency: float = EV_CHARGE_EFFICIENCY,
) -> tuple[float, float, bool] | None:
    """PPE 1's month forecast at ``as_of`` with the tail priced by ``method``.

    The archive reaches ``known_end`` (default: the end of the next day).
    The EV's import over the tail (``ev``):

    * ``month`` — the tail's cheapest hours, as the LP placed it before
      ADR 003 (past the measurements: its daily average of the last 28 days,
      the horizon's share included), ≤ the charger's power per hour,
    * ``day`` — each day's real energy in that day's cheapest hours,
    * ``hour`` — at the hours the car really charged,
    * ``profile`` — the integration's rule: each tail hour the mean of its
      weekday and hour over the 28 complete days before ``as_of``
      (:meth:`EVModule._learn_charge_profile`), handed to the optimizer as
      ``MonthTail.ev_grid_kwh``; ``None`` without 28 such days,

    past the measurements taking the latest same hour measured, and priced at
    the tail's estimate. In the horizon the car charges at its real hours
    (``month``: only while measured). Every EV kWh is divided by
    ``efficiency`` to the grid side.
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
            tail_ev += hours.ev_daily_kwh / 24 / efficiency
        else:
            kwh = max(0.0, row["ev"] or 0.0) / efficiency
            energy += kwh
            cost += kwh * row["price"]
        slots.append(_slot(moment, row, row["price"]))
    tail_slots = []
    # Where ``month`` and ``day`` place the car: (tail prices, kWh) per group.
    groups: dict[object, tuple[list[float], list[float]]] = {}
    for moment in _hours(known_end, month_end):
        row = hours.rows[moment]
        price = method(hours, moment, known_end)
        if price is None:
            return None
        kwh = max(0.0, row["ev"] or 0.0) / efficiency
        if ev == "month":
            if moment >= hours.end:
                kwh = hours.ev_daily_kwh / 24 / efficiency
            prices, energies = groups.setdefault("month", ([], [tail_ev]))
            prices.append(price)
            energies.append(kwh)
        elif ev == "day":
            prices, energies = groups.setdefault(dt_util.as_local(moment).date(), ([], []))
            prices.append(price)
            energies.append(kwh)
        elif ev == "hour":
            energy += kwh
            cost += kwh * price
        tail_slots.append(_slot(moment, row, price))
    for prices, energies in groups.values():
        placed, placed_cost = _cheapest_hours(prices, sum(energies))
        energy += placed
        cost += placed_cost
    ev_grid: list[float] = []
    if ev == "profile":
        profile = charge_profile(hours, as_of)
        if profile is None:
            return None
        ev_grid = [kwh / efficiency for kwh in profile([slot.start for slot in tail_slots])]
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
        tail=MonthTail(slots=tail_slots, ev_grid_kwh=ev_grid),
    )
    outcome = plan.billing[0]
    return outcome.energy_kwh, outcome.cost_pln, bool(outcome.cap_binds)


def _cheapest_hours(prices: list[float], kwh: float) -> tuple[float, float]:
    """``kwh`` in the cheapest of ``prices``, ≤ the charger's power each: (kWh, cost)."""
    placed = cost = 0.0
    for price in sorted(prices):
        if kwh - placed <= 0:
            break
        take = min(kwh - placed, EV_CHARGER_KW)
        placed += take
        cost += take * price
    return placed, cost


def charge_profile(hours: Hours, as_of: datetime):
    """The EV module's charging profile learned at ``as_of`` from the export.

    The export's measured ``ev`` stands in for the recorder's hourly reader,
    keyed by local hour like it. Returns the profile's per-hour lookup, or
    ``None`` (with the module's reason printed once) without enough days.
    """
    module = EVModule.__new__(EVModule)
    module.config = {CONF_EV_ENERGY_ADDED_SENSOR: "export.ev"}

    async def reader(entity_id, start, end):
        return {hour: kwh for hour, kwh in hours.ev_local.items() if start <= hour < end}

    module.coordinator = SimpleNamespace(consumption=SimpleNamespace(async_range_kwh=reader))
    module._charge_profile = None
    module._charge_profile_through = None
    asyncio.run(module._learn_charge_profile(as_of))
    if module.charge_profile_reason is not None:
        if module.charge_profile_reason not in _REPORTED:
            _REPORTED.add(module.charge_profile_reason)
            print(f"  {dt_util.as_local(as_of):%d.%m}: {module.charge_profile_reason}")
        return None
    return module.charge_profile_kwh


_REPORTED: set[str] = set()


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


def _every(first: datetime, last: datetime, step: int) -> list[datetime]:
    days = []
    while first <= last:
        days.append(first)
        first += timedelta(days=step)
    return days


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("export", type=Path)
    parser.add_argument("--every", type=int, default=3, help="days between as-of moments")
    parser.add_argument("--first", default=None, help="first as-of day, YYYY-MM-DD")
    parser.add_argument(
        "--days",
        default=None,
        help="as-of days instead of --first/--every, YYYY-MM-DD,YYYY-MM-DD,…",
    )
    parser.add_argument(
        "--ev",
        choices=("month", "day", "hour", "profile"),
        default="month",
        help="where the EV's tail import goes (see plan_month)",
    )
    parser.add_argument(
        "--ev-efficiency",
        type=float,
        default=EV_CHARGE_EFFICIENCY,
        help="EV charge efficiency: pack-side ev ÷ this = the grid side",
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
    last = dt_util.as_local(hours.end - HOUR)
    as_of_days = (
        [
            datetime.fromisoformat(day).replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            for day in args.days.split(",")
        ]
        if args.days
        else _every(start_day, last, args.every)
    )
    for day in as_of_days:
        as_of = dt_util.as_utc(day.replace(hour=12))
        local = dt_util.as_local(as_of)
        month_start, month_end = _month(local)
        if month_end <= hours.end:
            energy, cost = settle_real(hours, month_start, month_end)
            truth = f"{cost / energy:5.3f} {'BIND' if cost > LIMIT * energy else 'pod '}"
        else:
            truth = "  (trwa)  "
        results = [
            plan_month(hours, as_of, method, ev=args.ev, efficiency=args.ev_efficiency)
            for _, method in methods
        ]
        print(
            f"{local:%a %d.%m}        {truth}  "
            + "  ".join(f"{_fmt(r):^12}" for r in results),
            flush=True,
        )

    # The running month from the last measured hour, with every price
    # published so far (forecasts included) in the archive, as the plan has.
    as_of = hours.end
    results = [
        plan_month(
            hours,
            as_of,
            method,
            known_end=hours.prices_end,
            ev=args.ev,
            efficiency=args.ev_efficiency,
        )
        for _, method in methods
    ]
    print(
        f"{dt_util.as_local(as_of):%a %d.%m %H:%M}    (trwa)     "
        + "  ".join(f"{_fmt(r):^12}" for r in results)
    )
    print(f"  EV past the measurements: {hours.ev_daily_kwh:.1f} kWh/day")


if __name__ == "__main__":
    main()
