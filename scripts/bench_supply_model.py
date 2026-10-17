"""Solve time of the apartment-switching model (stage 3, decision 7).

Builds one realistic case — a 168 h horizon from the running hour plus a
~500 h month tail, a 30 kWh battery with its efficiency curve, charge-power
taper and minimum charge power, an EV to charge, the apartment's demand
(0.3–2 kW) apart from the Victron's own, prices with a daily shape and both
meters under the price cap — and times ``Optimizer.optimize`` for:

* no switching (the plan as before stage 3),
* switching with ``T_sw`` = 168 h (the whole horizon),
* switching with ``T_sw`` = 72 h.

Three runs each; prints the median seconds and what the plan did.
``--winter`` doubles the apartment's demand, raises the evening prices by
half and starts PPE 1's month at 0.60 zł/kWh, so moving the apartment pays
and the search really branches on the supply columns.
Run from the repository root with the project's venv:

    .venv/bin/python scripts/bench_supply_model.py [--winter]
"""

from __future__ import annotations

import random
import statistics
import sys
import time
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from homeassistant.util import dt as dt_util  # noqa: E402

from custom_components.powerpilot import optimizer as optimizer_module  # noqa: E402
from custom_components.powerpilot.battery import BatteryModel  # noqa: E402
from custom_components.powerpilot.const import (  # noqa: E402
    PPE_1,
    PPE_2,
    SUPPLY_METER_2,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.models import (  # noqa: E402
    BillingPeriod,
    Forecast,
    HourSlot,
    MonthTail,
    SupplyPlanning,
)
from custom_components.powerpilot.modules.ev import EVRequest  # noqa: E402
from custom_components.powerpilot.optimizer import (  # noqa: E402
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

HORIZON_H = 168
TAIL_H = 500
RUNS = 3
LIMIT = 0.61
WINTER = "--winter" in sys.argv[1:]

# Energy price shape by local hour (zł/kWh, gross) and distribution by band.
_PRICE = [0.30] * 6 + [0.75] * 3 + [0.45] * 7 + [1.10] * 5 + [0.55] * 3
_DIST = [0.10] * 6 + [0.30] * 16 + [0.10] * 2
# The apartment (0.3–2 kW) and the Victron's own demand without it (kWh/h).
_APARTMENT = [0.3] * 6 + [1.2] * 3 + [0.6] * 7 + [2.0] * 5 + [0.8] * 3
_VICTRON = [0.4] * 6 + [0.7] * 3 + [0.5] * 7 + [1.0] * 5 + [0.6] * 3
if WINTER:
    _APARTMENT = [2 * kwh for kwh in _APARTMENT]
    _PRICE = [p * 1.5 if 16 <= h <= 20 else p for h, p in enumerate(_PRICE)]


def _slots(start, hours: int, rng: random.Random) -> list[HourSlot]:
    slots = []
    day_factor = 1.0
    for i in range(hours):
        moment = start + timedelta(hours=i)
        hour = dt_util.as_local(moment).hour
        if hour == 0 or i == 0:
            day_factor = rng.uniform(0.8, 1.3)
        price = round(_PRICE[hour] * day_factor * rng.uniform(0.95, 1.05), 4)
        slots.append(
            HourSlot(
                start=moment,
                buy_price=price,
                cap_price_kwh=price,
                distribution_price_kwh=_DIST[hour],
                distribution_fixed_hourly=0.05,
                base_consumption_kwh=_VICTRON[hour] * rng.uniform(0.8, 1.2),
                apartment_kwh=_APARTMENT[hour] * rng.uniform(0.8, 1.2),
            )
        )
    return slots


def _case():
    rng = random.Random(20261017)
    start = dt_util.now().replace(minute=0, second=0, microsecond=0)  # running hour
    slots = _slots(start, HORIZON_H + TAIL_H, rng)
    forecast = Forecast(slots=slots[:HORIZON_H])
    tail = MonthTail(slots=slots[HORIZON_H:], ev_grid_kwh=120.0, ev_charger_kw=11.0)
    month_start = start - timedelta(days=17)
    month_end = start + timedelta(hours=HORIZON_H + TAIL_H)

    def period(ppe: str, energy: float, cost: float) -> BillingPeriod:
        return BillingPeriod(
            ppe=ppe,
            month_start=month_start,
            month_end=month_end,
            limit_kwh=LIMIT,
            energy_kwh=energy,
            cost_pln=cost,
            hours_expected=17 * 24,
            hours_covered=17 * 24,
        )

    billing = [period(PPE_1, 420.0, 420.0 * (0.60 if WINTER else 0.55))]
    ppe2 = [period(PPE_2, 25.0, 25.0 * 0.70)]
    night = {
        s.start for s in forecast.slots if dt_util.as_local(s.start).hour in range(0, 7)
    }
    ev = EVRequest(
        enabled=True,
        required_kwh=25.0,
        charger_kw=3.7,
        phases=3,
        battery_kwh=60.0,
        current_soc=40.0,
        available_hours=night,
        charge_efficiency=0.9,
    )
    return forecast, tail, billing, ppe2, ev


def _battery() -> BatteryModel:
    return BatteryModel(
        capacity_kwh=30.0,
        charge_efficiency=0.95,
        discharge_efficiency=0.95,
        wear_cost=0.05,
        min_soc=10.0,
        max_soc=100.0,
        soc=50.0,
        energy_cost=0.4,
    )


def _optimizer() -> Optimizer:
    return Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=5.0,
            inverter_max_discharge_kw=5.0,
            charge_curve=ChargeCurve(
                default_kw=5.0,
                segments=[
                    {"soc_from": 0, "soc_to": 80, "max_kw": 5.0},
                    {"soc_from": 80, "soc_to": 95, "max_kw": 3.0},
                    {"soc_from": 95, "soc_to": 101, "max_kw": 1.0},
                ],
            ),
            min_charge_power_kw=1.0,
            connection_power_kw=3 * 230 * 25 / 1000,
            phase_capacity_kw=230 * 25 / 1000,
            charge_efficiency_curve=[
                {"kw": 0.5, "eff": 0.85},
                {"kw": 1.5, "eff": 0.94},
                {"kw": 3.0, "eff": 0.95},
                {"kw": 5.0, "eff": 0.92},
            ],
        )
    )


def _run(label: str, t_sw: int, switching: bool) -> float:
    optimizer_module.SUPPLY_SWITCH_HOURS = t_sw
    times = []
    plan = None
    for _ in range(RUNS):
        forecast, tail, billing, ppe2, ev = _case()
        planning = SupplyPlanning(
            actual=SUPPLY_VICTRON,
            switching_reason=None if switching else "bench: bez przełączania",
            ppe2_periods=ppe2,
            last_transition_at=forecast.slots[0].start - timedelta(days=3),
        )
        began = time.perf_counter()
        plan = _optimizer().optimize(
            forecast, _battery(), ev, billing=billing, tail=tail, supply=planning
        )
        times.append(time.perf_counter() - began)
    median = statistics.median(times)
    supplies = [d.supply for d in plan.decisions]
    switches = sum(1 for a, b in zip(supplies, supplies[1:]) if a != b)
    on_meter_2 = sum(1 for s in supplies if s == SUPPLY_METER_2)
    settled = sum(o.settled_cost_pln for o in plan.billing)
    print(
        f"{label:<28} median {median:7.2f} s  runs "
        + ", ".join(f"{t:.2f}" for t in times)
        + f"  | switches {switches}, {on_meter_2} h on meter 2, "
        f"settled {settled:.2f} zł",
        flush=True,
    )
    return median


def main() -> None:
    print(
        f"horizon {HORIZON_H} h + tail {TAIL_H} h, {RUNS} runs per case"
        + (" (winter)" if WINTER else ""),
        flush=True,
    )
    _run("no switching (today)", 168, switching=False)
    full = _run("switching, T_sw = 168 h", 168, switching=True)
    short = _run("switching, T_sw = 72 h", 72, switching=True)
    print(f"168 h / 72 h solve-time ratio: {full / short:.2f}")


if __name__ == "__main__":
    main()
