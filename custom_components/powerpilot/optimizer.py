"""Cost-minimising battery optimizer (HiGHS linear program).

The optimizer turns the hourly :class:`Forecast`, the live battery state and the
EV request into a per-hour :class:`Plan`. Instead of the old price-percentile
heuristic it solves a single linear program over the **whole horizon** with the
HiGHS solver, so charge/discharge decisions are globally cost-optimal and coupled
across days (the battery can be drained through an expensive day to reach a much
cheaper one and be refilled there).

Model (mixed-integer: continuous charge, all-or-nothing discharge)
------------------------------------------------------------------
For every hour ``t`` we solve for:

* ``c[t]`` – energy drawn from the grid to charge the battery (AC kWh), a
  continuous flow bounded by the inverter charge power.
* ``z[t]`` – a **binary** discharge switch: ``1`` means the battery covers the
  *whole* house demand that hour (``cap[t] = min(demand, inverter_max_discharge)``
  kWh delivered), ``0`` means it stays idle and the house runs off the grid.

Making discharge all-or-nothing means every hour resolves to exactly one
inverter mode (charge / battery / passthrough) and the optimizer never rations a
partial discharge across hours. This is a deliberate simplification: it trades a
marginally cheaper fractional plan for one that maps cleanly onto a single
per-hour inverter setpoint.

The stored energy (kWh) evolves as a prefix sum::

    E[t] = E0 + Σ_{k≤t} (c[k]·η_ch − d[k]/η_dis)

and is bounded by the usable SoC band ``[E_min, E_max]``. The household demand of
each hour is served either from the grid or from the battery (no export, so
``d[t] ≤ demand[t]``). The EV charger sits before the inverter and is always
grid-fed; its per-hour energy ``ev[t]`` is planned separately into the cheapest
available hours and enters the LP as a fixed extra grid load.

Grid energy imported in an hour is ``grid[t] = demand[t] − d[t] + c[t] + ev[t]``
and is capped by the physical connection power.

Objective – minimise total spend over the horizon::

    Σ_t  total_price[t]·grid[t]              (energy + distribution paid)
       + wear·(c[t]·η_ch + d[t])             (battery cycling wear)
       − v_terminal·E[T-1]                   (value of energy left at the end)

Buy-only (no export) plus round-trip losses (η_ch·η_dis < 1) and a positive wear
cost mean it is never optimal to charge and discharge in the same hour, so the
only integer variables are the discharge switches ``z[t]``. The energy already
in the battery at the start is a **sunk cost** – using it only costs wear – so
the optimizer naturally spends stored energy on the most expensive hours it
chooses to cover instead of hoarding it at a high SoC.

The terminal value ``v_terminal`` prices the energy left in the pack at the end
of the horizon (so the LP does not irrationally dump the battery on the last
hour). It is the discharge-adjusted average grid price of the horizon, i.e. the
realistic price at which that energy would otherwise be replaced.

The solver output is replayed through :class:`BatteryModel` so the reported
``battery_energy_cost`` / ``battery_use_cost`` and the per-hour cost breakdown
stay consistent with the rest of the integration.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime

import highspy
import numpy as np
from homeassistant.util import dt as dt_util

from .battery import BatteryModel
from .const import ChargePower, InverterMode
from .models import Decision, Forecast, Plan
from .modules.ev import EVRequest

_LOGGER = logging.getLogger(__name__)

_EPS = 1e-6


@dataclass
class ChargeCurve:
    """Maps SoC to the maximum allowed charge power (kW)."""

    default_kw: float
    segments: list[dict] = field(default_factory=list)  # {soc_from, soc_to, max_kw}

    def max_kw(self, soc: float) -> float:
        for seg in self.segments:
            if seg["soc_from"] <= soc < seg["soc_to"]:
                return float(seg["max_kw"])
        return self.default_kw


@dataclass
class OptimizerConfig:
    """Static, hardware-derived parameters for the LP."""

    inverter_max_charge_kw: float
    inverter_max_discharge_kw: float
    grid_disconnect_soc: float
    charge_curve: ChargeCurve
    # Maximum grid import power (kW). 0 disables the connection-power limit.
    connection_power_kw: float = 0.0
    # Explicit terminal price (PLN/kWh) for energy left at the end of the
    # horizon. ``None`` → use the horizon-average total grid price.
    terminal_price: float | None = None


def _upper_hull(points: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Upper (concave) hull of points sorted by x, via the monotone chain."""
    hull: list[tuple[float, float]] = []
    for p in points:
        while len(hull) >= 2:
            (x1, y1), (x2, y2) = hull[-2], hull[-1]
            # Pop while the last turn is not a right turn (keeps the upper hull).
            if (x2 - x1) * (p[1] - y1) - (y2 - y1) * (p[0] - x1) >= 0:
                hull.pop()
            else:
                break
        hull.append(p)
    return hull


def _charge_curve_cuts(
    curve: ChargeCurve, capacity_kwh: float
) -> list[tuple[float, float]]:
    """Affine upper bounds ``max_charge_kw(E) ≤ slope·E + intercept``.

    The SoC-dependent charge curve is represented by the concave envelope of its
    band corners, which keeps the model a pure LP. For a piecewise-constant band
    curve the envelope is a slightly *loose* upper bound (it bows above the steps
    between band corners by up to a fraction of a kW); it is tight at the corners
    and exactly captures the monotonic taper, which is what matters — the LP is
    re-solved every cycle against the measured SoC, so any small slack self-
    corrects. An empty curve returns no cuts (the flat inverter limit, applied as
    a simple variable bound, is enough).
    """
    if not curve.segments or capacity_kwh <= 0:
        return []

    points: set[tuple[float, float]] = {(0.0, curve.default_kw)}
    for seg in curve.segments:
        e_from = capacity_kwh * float(seg["soc_from"]) / 100.0
        e_to = capacity_kwh * float(seg["soc_to"]) / 100.0
        power = float(seg["max_kw"])
        points.add((e_from, power))
        points.add((e_to, power))
    # Pin the envelope to E=capacity using the last band's power. ``max_kw`` is
    # not usable here: its match is ``soc_from <= soc < soc_to``, so the last
    # band's *exclusive* upper edge never matches and would fall back to
    # ``default_kw`` — pulling the concave hull up and erasing the high-SoC
    # taper. The last segment's ``max_kw`` is the power that actually applies as
    # the battery approaches full.
    points.add((capacity_kwh, float(curve.segments[-1]["max_kw"])))

    hull = _upper_hull(sorted(points))
    cuts: list[tuple[float, float]] = []
    for (e1, p1), (e2, p2) in zip(hull, hull[1:]):
        if e2 - e1 <= _EPS:
            continue
        slope = (p2 - p1) / (e2 - e1)
        intercept = p1 - slope * e1
        cuts.append((slope, intercept))
    return cuts


class Optimizer:
    """Produces a cost-optimal :class:`Plan` via a HiGHS linear program."""

    def __init__(self, config: OptimizerConfig) -> None:
        self.config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def optimize(
        self,
        forecast: Forecast,
        battery: BatteryModel,
        ev_request: EVRequest | None = None,
        reminders: list[str] | None = None,
    ) -> Plan:
        battery = battery.copy()  # never mutate the live state
        slots = forecast.slots
        n = len(slots)
        if n == 0:
            return Plan(forecast=forecast, decisions=[], created_at=dt_util.now())

        ev_hours = self._plan_ev(forecast, ev_request)
        ev_charger_kw = ev_request.charger_kw if ev_request else 0.0

        # Per-hour prices and demand. Missing energy prices fall back to the
        # median so the SoC chain stays continuous.
        prices = [s.buy_price for s in slots if s.buy_price is not None]
        median_price = statistics.median(prices) if prices else 0.0
        energy_price = [
            s.buy_price if s.buy_price is not None else median_price for s in slots
        ]
        distribution = [s.distribution_price_kwh or 0.0 for s in slots]
        total_price = [energy_price[t] + distribution[t] for t in range(n)]
        demand = [max(0.0, s.total_consumption_kwh) for s in slots]
        ev_kwh = [ev_hours.get(slots[t].start, 0.0) for t in range(n)]

        charge, discharge = self._solve_lp(
            battery=battery,
            total_price=total_price,
            demand=demand,
            ev_kwh=ev_kwh,
            ev_charger_kw=ev_charger_kw,
        )

        return self._build_plan(
            forecast=forecast,
            battery=battery,
            charge=charge,
            discharge=discharge,
            energy_price=energy_price,
            distribution=distribution,
            total_price=total_price,
            demand=demand,
            ev_kwh=ev_kwh,
            reminders=reminders,
        )

    # ------------------------------------------------------------------
    # Linear program
    # ------------------------------------------------------------------
    def _solve_lp(
        self,
        battery: BatteryModel,
        total_price: list[float],
        demand: list[float],
        ev_kwh: list[float],
        ev_charger_kw: float,
    ) -> tuple[list[float], list[float]]:
        cfg = self.config
        n = len(total_price)
        ceff = max(battery.charge_efficiency, _EPS)
        deff = max(battery.discharge_efficiency, _EPS)
        wear = battery.wear_cost
        cap = battery.capacity_kwh
        e_min = cap * battery.min_soc / 100.0
        e_max = cap * battery.max_soc / 100.0
        e0 = min(max(battery.energy_kwh, e_min), e_max)

        p_term = (
            cfg.terminal_price
            if cfg.terminal_price is not None
            else (sum(total_price) / n if n else 0.0)
        )
        # Value of one stored kWh left at the end: it later delivers ``deff`` kWh
        # to the house, displacing grid energy worth ``p_term`` each.
        tv = deff * p_term

        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        inf = highspy.kHighsInf

        # Charge columns c[0..n-1] are continuous grid-draw power (kWh per hour).
        for t in range(n):
            charge_cap = cfg.inverter_max_charge_kw
            if ev_kwh[t] > 0:
                # EV shares a phase with the inverter; leave it head-room.
                charge_cap = max(0.0, charge_cap - ev_charger_kw)
            h.addVar(0.0, charge_cap)
        # Discharge columns d[0..n-1] are BINARY: each hour either covers the
        # whole house demand from the battery (z=1 → cap[t] kWh delivered) or not
        # at all (z=0). This makes every hour exactly one inverter mode and
        # forbids rationing a *partial* discharge across hours — a deliberate
        # simplification chosen over a marginally cheaper fractional plan.
        cap = [min(demand[t], cfg.inverter_max_discharge_kw) for t in range(n)]
        for t in range(n):
            if cap[t] > _EPS:
                h.addVar(0.0, 1.0)
                h.changeColIntegrality(n + t, highspy.HighsVarType.kInteger)
            else:
                h.addVar(0.0, 0.0)  # no demand → nothing to cover this hour

        cost = np.empty(2 * n, dtype=np.float64)
        for t in range(n):
            cost[t] = total_price[t] + wear * ceff - tv * ceff
            # d-column is a 0/1 switch worth cap[t] kWh delivered to the house.
            cost[n + t] = (-total_price[t] + wear + tv / deff) * cap[t]
        h.changeColsCost(2 * n, np.arange(2 * n, dtype=np.int32), cost)

        # SoC band on the running reservoir level after each hour.
        for t in range(n):
            idx: list[int] = []
            val: list[float] = []
            for k in range(t + 1):
                idx.append(k)
                val.append(ceff)
                idx.append(n + k)
                val.append(-cap[k] / deff)
            h.addRow(
                e_min - e0,
                e_max - e0,
                len(idx),
                np.array(idx, dtype=np.int32),
                np.array(val, dtype=np.float64),
            )

        # Connection-power limit on grid import.
        if cfg.connection_power_kw and cfg.connection_power_kw > 0:
            for t in range(n):
                rhs = cfg.connection_power_kw - demand[t] - ev_kwh[t]
                h.addRow(
                    -inf,
                    rhs,
                    2,
                    np.array([t, n + t], dtype=np.int32),
                    np.array([1.0, -cap[t]], dtype=np.float64),
                )

        # SoC-dependent charge curve (only when actually configured).
        for slope, intercept in _charge_curve_cuts(cfg.charge_curve, cap):
            if abs(slope) <= _EPS:
                continue
            for t in range(n):
                idx = [t]
                val = [1.0]
                for k in range(t):
                    idx.append(k)
                    val.append(-slope * ceff)
                    idx.append(n + k)
                    val.append(slope * cap[k] / deff)
                h.addRow(
                    -inf,
                    intercept + slope * e0,
                    len(idx),
                    np.array(idx, dtype=np.int32),
                    np.array(val, dtype=np.float64),
                )

        h.run()
        status = h.getModelStatus()
        if status != highspy.HighsModelStatus.kOptimal:
            raise RuntimeError(f"HiGHS did not find an optimal plan: {status}")
        sol = list(h.getSolution().col_value)
        charge = sol[:n]
        # d-columns are 0/1 switches; expand back to delivered energy (cap kWh).
        discharge = [cap[t] * sol[n + t] for t in range(n)]
        return charge, discharge

    # ------------------------------------------------------------------
    # Replay the LP solution into Decisions + cost reporting
    # ------------------------------------------------------------------
    def _build_plan(
        self,
        forecast: Forecast,
        battery: BatteryModel,
        charge: list[float],
        discharge: list[float],
        energy_price: list[float],
        distribution: list[float],
        total_price: list[float],
        demand: list[float],
        ev_kwh: list[float],
        reminders: list[str] | None,
    ) -> Plan:
        cfg = self.config
        ceff = max(battery.charge_efficiency, _EPS)
        deff = max(battery.discharge_efficiency, _EPS)
        wear = battery.wear_cost
        n = len(total_price)
        p_term = (
            cfg.terminal_price
            if cfg.terminal_price is not None
            else (sum(total_price) / n if n else 0.0)
        )
        # Economic thresholds implied by the objective (for the human-readable
        # trace; the actual decisions come from the global LP, which may deviate
        # because of SoC limits or because the energy is needed elsewhere).
        charge_threshold = ceff * (deff * p_term - wear)
        discharge_threshold = p_term + wear

        decisions: list[Decision] = []
        for t, slot in enumerate(forecast.slots):
            c = charge[t] if charge[t] > _EPS else 0.0
            d = discharge[t] if discharge[t] > _EPS else 0.0
            ev = ev_kwh[t]
            tp = total_price[t]

            soc_before = battery.soc
            cost_before = battery.energy_cost

            stored = 0.0
            delivered = 0.0
            if c > 0:
                stored = battery.charge_from_grid(c, tp)
            if d > 0:
                delivered, _ = battery.discharge_to_load(d)

            if stored > _EPS and stored >= delivered:
                mode = InverterMode.CHARGE
            elif delivered > _EPS:
                mode = InverterMode.DISCHARGE
            else:
                mode = InverterMode.PASSTHROUGH

            grid_buy = max(0.0, demand[t] - delivered + c + ev)

            decision = Decision(start=slot.start)
            decision.inverter_mode = mode
            decision.ev_charge = ev > 0
            decision.ev_charge_kwh = ev
            decision.charge_power = (
                ChargePower.LIMITED if ev > 0 else ChargePower.FULL
            )
            decision.battery_charge_kwh = stored
            # Grid-side charge power (kW) actually drawn this hour — what you set
            # on the inverter as "force charge X kW". Equals ``stored / η_ch``
            # (reduced if the SoC ceiling clipped the charge mid-hour); the hour
            # slot is 1 h so kWh == average kW.
            decision.charge_power_kw = stored / ceff if stored > _EPS else 0.0
            decision.battery_discharge_kwh = delivered
            decision.grid_buy_kwh = grid_buy
            decision.battery_soc = battery.soc
            decision.battery_energy_cost = battery.energy_cost
            decision.grid_connected = battery.soc >= cfg.grid_disconnect_soc
            decision.energy_cost = grid_buy * energy_price[t]
            decision.distribution_cost = grid_buy * distribution[t]
            decision.hour_cost = decision.energy_cost + decision.distribution_cost
            decision.fixed_cost = slot.distribution_fixed_hourly or 0.0
            decision.battery_use_cost = delivered * decision.battery_energy_cost

            decision.trace = {
                "total_price": round(tp, 4),
                "energy_price": round(energy_price[t], 4),
                "distribution": round(distribution[t], 4),
                "terminal_price": round(p_term, 4),
                "charge_threshold": round(charge_threshold, 4),
                "discharge_threshold": round(discharge_threshold, 4),
                "demand_kwh": round(demand[t], 3),
                "ev_kwh": round(ev, 3),
                "charge_kwh": round(stored, 3),
                "discharge_kwh": round(delivered, 3),
                "grid_buy_kwh": round(grid_buy, 3),
                "soc_before": round(soc_before, 1),
                "soc_after": round(battery.soc, 1),
                "battery_energy_cost_before": round(cost_before, 4),
                "reason": self._reason(
                    mode, tp, charge_threshold, discharge_threshold, stored, delivered
                ),
            }

            if t == 0 and reminders:
                decision.reminders = list(reminders)

            decisions.append(decision)

        return Plan(forecast=forecast, decisions=decisions, created_at=dt_util.now())

    @staticmethod
    def _reason(
        mode: str,
        total_price: float,
        charge_threshold: float,
        discharge_threshold: float,
        stored: float,
        delivered: float,
    ) -> str:
        if mode == InverterMode.CHARGE:
            return (
                f"ładowanie {stored:.2f} kWh: cena {total_price:.3f} ≤ próg "
                f"opłacalnego magazynowania {charge_threshold:.3f} — energia "
                f"z tej godziny pokryje droższe godziny w horyzoncie"
            )
        if mode == InverterMode.DISCHARGE:
            return (
                f"rozładowanie {delivered:.2f} kWh: cena {total_price:.3f} ≥ próg "
                f"opłacalnego rozładowania {discharge_threshold:.3f} — taniej "
                f"z baterii niż z sieci"
            )
        if total_price < charge_threshold:
            return (
                f"cena {total_price:.3f} < próg ładowania {charge_threshold:.3f}, "
                f"ale optymalizator nie ładuje (bateria pełna albo brak droższych "
                f"godzin do pokrycia tą energią) → passthrough"
            )
        if total_price > discharge_threshold:
            return (
                f"cena {total_price:.3f} > próg rozładowania "
                f"{discharge_threshold:.3f}, ale energia z baterii jest "
                f"potrzebna na jeszcze droższe godziny → passthrough"
            )
        return (
            f"cena {total_price:.3f} pomiędzy progiem ładowania "
            f"({charge_threshold:.3f}) a rozładowania ({discharge_threshold:.3f}) "
            f"→ passthrough"
        )

    # ------------------------------------------------------------------
    # EV planning (grid-fed, cheapest available hours)
    # ------------------------------------------------------------------
    def _plan_ev(
        self, forecast: Forecast, ev_request: EVRequest | None
    ) -> dict[datetime, float]:
        """Allocate EV charging into the cheapest available hours."""
        if ev_request is None or not ev_request.is_actionable:
            return {}

        candidates = [
            slot
            for slot in forecast.slots
            if slot.start in ev_request.available_hours and slot.buy_price is not None
        ]
        candidates.sort(key=lambda s: s.buy_price)

        remaining = ev_request.required_kwh
        per_hour = max(ev_request.charger_kw, 0.1)
        allocation: dict[datetime, float] = {}
        for slot in candidates:
            if remaining <= 0:
                break
            take = min(per_hour, remaining)
            allocation[slot.start] = take
            remaining -= take
        return allocation
