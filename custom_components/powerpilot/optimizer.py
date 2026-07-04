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
import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import highspy
import numpy as np
from homeassistant.util import dt as dt_util

from .battery import BatteryModel
from .const import ChargePower, InverterMode
from .models import Decision, Forecast, Plan
from .modules.ev import EVRequest

_LOGGER = logging.getLogger(__name__)

_EPS = 1e-6

# Tiny per-hour-index cost added to charging so that among *equally priced*
# hours the LP front-loads: the efficiency sweet spot fills everywhere first
# (band differences dwarf this), then the above-sweet-spot surplus lands in
# the EARLIEST hours — so any shortfall later extends into remaining cheap
# hours instead of expensive ones. Sizing matters on both ends: over a 48 h
# horizon the accumulated difference (~5e-5 PLN/kWh) stays below any real
# price difference, while adjacent-hour moves (≥1e-6 PLN per kWh) stay above
# the solver's tolerances — which also requires the tightened MIP gap set in
# ``_solve_lp`` (HiGHS' default 1e-4 relative gap would happily return any
# plan within a fraction of a grosz of optimal, scrambling the tie order).
_EARLY_TIE_BREAK = 1e-6  # PLN/kWh per hour index

# Charge-order price bucket: supplier bills and the UI reason about prices in
# grosze, so hidden sub-grosz noise must not reshuffle equal-looking charge
# hours. A full grosz difference (e.g. 0.19 vs 0.20) still dominates the
# earliness tie-break.
_CHARGE_PRICE_BUCKET = Decimal("0.01")

# Bumped whenever the EV allocation strategy changes. Surfaced in the debug dump
# so it's obvious from a JSON paste whether the running code is the current
# full-power-block allocator or a stale import.
EV_ALLOCATOR_VERSION = "trip-drain-aware-2026-07"


def _charge_order_price(price: float) -> float:
    """Price used to order charge hours, rounded to the displayed grosz."""
    return float(
        Decimal(str(price)).quantize(_CHARGE_PRICE_BUCKET, rounding=ROUND_HALF_UP)
    )


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
    # Grid-side charge power floor (kW): each hour charges 0 *or* ≥ this. 0
    # disables it. Keeps the plan from forcing trivial sub-kW dribbles.
    min_charge_power_kw: float = 0.0
    # Maximum grid import power (kW). 0 disables the connection-power limit.
    connection_power_kw: float = 0.0
    # Single-phase capacity (kW) = phase voltage × main fuse. Caps the battery
    # charger during EV hours: worst case both draw through the same phase, so
    # the inverter may take at most ``phase_capacity − EV per-phase draw``. 0 →
    # fall back to the legacy blanket subtraction (which zeroed the battery
    # whenever the EV charger was at least as strong as the inverter, splitting
    # EV and ESS charging into disjoint hours for no physical reason).
    phase_capacity_kw: float = 0.0
    # Power-dependent charge efficiency samples: list of {"kw", "eff"} points
    # (eff = 0..1). Empty/None → the battery's flat charge efficiency applies.
    charge_efficiency_curve: list[dict] | None = None
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
        charge_order_price = [_charge_order_price(price) for price in total_price]
        demand = [max(0.0, s.total_consumption_kwh) for s in slots]
        ev_kwh = [ev_hours.get(slots[t].start, 0.0) for t in range(n)]

        charge, discharge, stored = self._solve_lp(
            battery=battery,
            total_price=total_price,
            demand=demand,
            ev_kwh=ev_kwh,
            ev_charger_kw=ev_charger_kw,
            charge_order_price=charge_order_price,
        )

        return self._build_plan(
            forecast=forecast,
            battery=battery,
            charge=charge,
            discharge=discharge,
            stored=stored,
            energy_price=energy_price,
            distribution=distribution,
            total_price=total_price,
            demand=demand,
            ev_kwh=ev_kwh,
            charge_order_price=charge_order_price,
            ev_request=ev_request,
            reminders=reminders,
        )

    # ------------------------------------------------------------------
    # Linear program
    # ------------------------------------------------------------------
    def _efficiency_segments(self, ceff: float) -> list[tuple[float, float]]:
        """Marginal charge-efficiency segments ``(width_kw, η)`` for the LP.

        Built from the configured power→efficiency samples as the concave hull
        of ``stored(P) = P·η(P)``: each hull chord is one segment whose slope is
        the *marginal* efficiency over that power band. Slopes are non-increasing
        by construction, so a pure LP fills the best segment first and the fill
        order matches physics (charge power grows through the sweet spot before
        the tail where efficiency falls off). The rising low-power part of a real
        efficiency chart is convex and gets flattened by the hull — a deliberate
        approximation (slopes are clamped to ≤ 1 so the model can never store
        more than it draws), and the min-charge-power floor keeps the plan out
        of that region anyway. Without a curve: one flat-η segment.
        """
        cap = self.config.inverter_max_charge_kw
        curve = self.config.charge_efficiency_curve or []
        points = sorted(
            (float(p["kw"]), min(max(float(p["eff"]), 0.01), 1.0))
            for p in curve
            if float(p.get("kw", 0.0)) > _EPS
        )
        if not points or cap <= _EPS:
            return [(cap, ceff)]

        stored_pts: list[tuple[float, float]] = [(0.0, 0.0)]
        prev_kw, prev_eff = 0.0, points[0][1]
        for kw, eff in points:
            if kw >= cap - _EPS:
                # Interpolate η at the inverter cap and stop — powers beyond
                # the cap are unreachable.
                if kw > cap + _EPS and kw > prev_kw:
                    frac = (cap - prev_kw) / (kw - prev_kw)
                    eff = prev_eff + (eff - prev_eff) * frac
                    kw = cap
                stored_pts.append((kw, kw * eff))
                break
            stored_pts.append((kw, kw * eff))
            prev_kw, prev_eff = kw, eff
        else:
            # Curve ends below the cap → extend flat with the last efficiency.
            stored_pts.append((cap, cap * points[-1][1]))

        hull = _upper_hull(sorted(set(stored_pts)))
        segments: list[tuple[float, float]] = []
        for (x1, y1), (x2, y2) in zip(hull, hull[1:]):
            if x2 - x1 <= _EPS:
                continue
            segments.append((x2 - x1, min((y2 - y1) / (x2 - x1), 1.0)))
        return segments or [(cap, ceff)]

    def _solve_lp(
        self,
        battery: BatteryModel,
        total_price: list[float],
        demand: list[float],
        ev_kwh: list[float],
        ev_charger_kw: float,
        charge_order_price: list[float] | None = None,
    ) -> tuple[list[float], list[float], list[float]]:
        cfg = self.config
        n = len(total_price)
        ceff = max(battery.charge_efficiency, _EPS)
        deff = max(battery.discharge_efficiency, _EPS)
        wear = battery.wear_cost
        capacity_kwh = battery.capacity_kwh
        e_min = capacity_kwh * battery.min_soc / 100.0
        e_max = capacity_kwh * battery.max_soc / 100.0
        e0 = min(max(battery.energy_kwh, e_min), e_max)
        charge_price = (
            charge_order_price
            if charge_order_price is not None
            else [_charge_order_price(price) for price in total_price]
        )

        p_term = (
            cfg.terminal_price
            if cfg.terminal_price is not None
            else (sum(total_price) / n if n else 0.0)
        )
        # Value of one stored kWh left at the end: it later delivers ``deff`` kWh
        # to the house, displacing grid energy worth ``p_term`` each.
        tv = deff * p_term

        # Charging is modelled per efficiency segment: hour t has K columns
        # c[t,k] (grid kWh within power band k), stored energy = Σ η_k·c[t,k].
        # With no efficiency curve K == 1 and η_1 == ceff — the legacy model.
        segments = self._efficiency_segments(ceff)
        seg_n = len(segments)
        seg_width = [w for w, _ in segments]
        seg_eff = [e for _, e in segments]

        h = highspy.Highs()
        h.setOptionValue("output_flag", False)
        # Prove (near-)exact optimality: the earliness tie-break works in
        # micro-PLN, far below HiGHS' default 1e-4 relative MIP gap — with the
        # default the solver may return any of the many almost-equal plans and
        # the charge powers land in arbitrary hours. The model is small
        # (hundreds of columns), so the tight gap costs milliseconds.
        h.setOptionValue("mip_rel_gap", 0.0)
        h.setOptionValue("mip_abs_gap", 1e-7)
        inf = highspy.kHighsInf

        def seg_col(t: int, k: int) -> int:
            return t * seg_n + k

        d0 = n * seg_n  # first discharge column
        y0 = d0 + n  # first min-charge indicator column

        # Per-hour grid-side charge cap. During EV hours the battery may still
        # charge — the real limit is electrical: worst case the EV charger and
        # the inverter share one phase, so the inverter gets whatever the phase
        # fuse leaves after the EV draw. Without fuse data fall back to the
        # legacy blanket subtraction.
        charge_caps: list[float] = []
        for t in range(n):
            charge_cap = cfg.inverter_max_charge_kw
            if ev_kwh[t] > 0:
                if cfg.phase_capacity_kw > _EPS:
                    charge_cap = min(
                        charge_cap,
                        max(0.0, cfg.phase_capacity_kw - ev_charger_kw),
                    )
                else:
                    charge_cap = max(0.0, charge_cap - ev_charger_kw)
            charge_caps.append(charge_cap)

        # Charge segment columns c[t,k].
        for t in range(n):
            for k in range(seg_n):
                h.addVar(0.0, min(seg_width[k], charge_caps[t]))
        # Discharge columns are BINARY: each hour either covers the whole house
        # demand from the battery (z=1 → cap[t] kWh delivered) or not at all
        # (z=0). This makes every hour exactly one inverter mode and forbids
        # rationing a *partial* discharge across hours — a deliberate
        # simplification chosen over a marginally cheaper fractional plan.
        discharge_cap = [min(demand[t], cfg.inverter_max_discharge_kw) for t in range(n)]
        for t in range(n):
            if discharge_cap[t] > _EPS:
                h.addVar(0.0, 1.0)
                h.changeColIntegrality(d0 + t, highspy.HighsVarType.kInteger)
            else:
                h.addVar(0.0, 0.0)  # no demand → nothing to cover this hour

        n_cols = d0 + n
        cost = np.empty(n_cols, dtype=np.float64)
        for t in range(n):
            # Earliness tie-break: among equally priced hours prefer charging
            # in the earliest, so an unfinished charge extends into the cheap
            # tail instead of expensive hours.
            tie = _EARLY_TIE_BREAK * t
            for k in range(seg_n):
                cost[seg_col(t, k)] = (
                    charge_price[t] + (wear - tv) * seg_eff[k] + tie
                )
            # d-column is a 0/1 switch worth discharge_cap[t] kWh delivered.
            cost[d0 + t] = (-total_price[t] + wear + tv / deff) * discharge_cap[t]
        h.changeColsCost(n_cols, np.arange(n_cols, dtype=np.int32), cost)

        # SoC band on the running reservoir level after each hour.
        for t in range(n):
            idx: list[int] = []
            val: list[float] = []
            for k in range(t + 1):
                for s in range(seg_n):
                    idx.append(seg_col(k, s))
                    val.append(seg_eff[s])
                idx.append(d0 + k)
                val.append(-discharge_cap[k] / deff)
            h.addRow(
                e_min - e0,
                e_max - e0,
                len(idx),
                np.array(idx, dtype=np.int32),
                np.array(val, dtype=np.float64),
            )

        # Per-hour total charge cap (the segment bounds alone allow up to the
        # full inverter power; EV hours may leave less).
        for t in range(n):
            if charge_caps[t] < sum(seg_width) - _EPS:
                h.addRow(
                    -inf,
                    charge_caps[t],
                    seg_n,
                    np.array([seg_col(t, k) for k in range(seg_n)], dtype=np.int32),
                    np.ones(seg_n, dtype=np.float64),
                )

        # Connection-power limit on grid import.
        if cfg.connection_power_kw and cfg.connection_power_kw > 0:
            for t in range(n):
                rhs = cfg.connection_power_kw - demand[t] - ev_kwh[t]
                idx = [seg_col(t, k) for k in range(seg_n)] + [d0 + t]
                val = [1.0] * seg_n + [-discharge_cap[t]]
                h.addRow(
                    -inf,
                    rhs,
                    len(idx),
                    np.array(idx, dtype=np.int32),
                    np.array(val, dtype=np.float64),
                )

        # SoC-dependent charge curve (only when actually configured).
        for slope, intercept in _charge_curve_cuts(cfg.charge_curve, capacity_kwh):
            if abs(slope) <= _EPS:
                continue
            for t in range(n):
                idx = [seg_col(t, k) for k in range(seg_n)]
                val = [1.0] * seg_n
                for k in range(t):
                    for s in range(seg_n):
                        idx.append(seg_col(k, s))
                        val.append(-slope * seg_eff[s])
                    idx.append(d0 + k)
                    val.append(slope * discharge_cap[k] / deff)
                h.addRow(
                    -inf,
                    intercept + slope * e0,
                    len(idx),
                    np.array(idx, dtype=np.int32),
                    np.array(val, dtype=np.float64),
                )

        # Minimum charge power: make each hour's total charge semi-continuous —
        # either 0 or ≥ min_charge. One binary indicator y[t] per hour with
        #   Σ_k c[t,k] ≤ cap[t]·y[t]   (charge only when the switch is on)
        #   Σ_k c[t,k] ≥ min_charge·y[t]  (and then at least the floor)
        min_charge = cfg.min_charge_power_kw or 0.0
        if min_charge > _EPS:
            for t in range(n):
                h.addVar(0.0, 1.0)
                h.changeColIntegrality(y0 + t, highspy.HighsVarType.kInteger)
            for t in range(n):
                idx = [seg_col(t, k) for k in range(seg_n)]
                # Σc - cap·y ≤ 0
                h.addRow(
                    -inf,
                    0.0,
                    seg_n + 1,
                    np.array([*idx, y0 + t], dtype=np.int32),
                    np.array([*([1.0] * seg_n), -charge_caps[t]], dtype=np.float64),
                )
                # Σc - min_charge·y ≥ 0
                h.addRow(
                    0.0,
                    inf,
                    seg_n + 1,
                    np.array([*idx, y0 + t], dtype=np.int32),
                    np.array([*([1.0] * seg_n), -min_charge], dtype=np.float64),
                )

        h.run()
        status = h.getModelStatus()
        if status != highspy.HighsModelStatus.kOptimal:
            raise RuntimeError(f"HiGHS did not find an optimal plan: {status}")
        sol = list(h.getSolution().col_value)
        charge = [
            sum(sol[seg_col(t, k)] for k in range(seg_n)) for t in range(n)
        ]
        # Stored (post-loss) energy per hour, from the same segment solution —
        # _build_plan replays it so the effective η matches the chosen power.
        stored = [
            sum(seg_eff[k] * sol[seg_col(t, k)] for k in range(seg_n))
            for t in range(n)
        ]
        # d-columns are 0/1 switches; expand back to delivered energy.
        discharge = [discharge_cap[t] * sol[d0 + t] for t in range(n)]
        return charge, discharge, stored

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
        charge_order_price: list[float] | None = None,
        ev_request: EVRequest | None = None,
        reminders: list[str] | None = None,
        stored: list[float] | None = None,
    ) -> Plan:
        cfg = self.config
        ceff = max(battery.charge_efficiency, _EPS)
        deff = max(battery.discharge_efficiency, _EPS)
        wear = battery.wear_cost
        n = len(total_price)
        charge_price = charge_order_price or [
            _charge_order_price(price) for price in total_price
        ]
        # EV SoC forecast: project the car's charge forward from its live SoC as
        # planned charging is delivered and predicted trip driving drains it.
        # Only possible when the SoC sensor and a battery size are known;
        # otherwise the line stays blank.
        ev_battery_kwh = ev_request.battery_kwh if ev_request else 0.0
        ev_drain = ev_request.drain_kwh if ev_request else {}
        ev_soc_kwh: float | None = (
            ev_request.current_soc / 100.0 * ev_battery_kwh
            if ev_request is not None
            and ev_request.current_soc is not None
            and ev_battery_kwh > 0
            else None
        )
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

            stored_kwh = 0.0
            delivered = 0.0
            # Effective charge efficiency at the LP-chosen power (from the
            # efficiency-curve segments); flat η when no curve is configured.
            eff_t = (
                stored[t] / c
                if stored is not None and c > _EPS and stored[t] > _EPS
                else None
            )
            if c > 0:
                stored_kwh = battery.charge_from_grid(c, tp, efficiency=eff_t)
            if d > 0:
                delivered, _ = battery.discharge_to_load(d)

            if stored_kwh > _EPS and stored_kwh >= delivered:
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
            if ev_soc_kwh is not None:
                drain = ev_drain.get(slot.start, 0.0)
                ev_soc_kwh = min(ev_battery_kwh, max(0.0, ev_soc_kwh + ev - drain))
                decision.ev_soc = round(ev_soc_kwh / ev_battery_kwh * 100.0, 1)
            decision.charge_power = (
                ChargePower.LIMITED if ev > 0 else ChargePower.FULL
            )
            decision.battery_charge_kwh = stored_kwh
            # Grid-side charge power (kW) actually drawn this hour — what you set
            # on the inverter as "force charge X kW". Equals ``stored / η``
            # (reduced if the SoC ceiling clipped the charge mid-hour); the hour
            # slot is 1 h so kWh == average kW.
            decision.charge_power_kw = (
                stored_kwh / (eff_t or ceff) if stored_kwh > _EPS else 0.0
            )
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
                "total_price_raw": round(tp, 6),
                "charge_order_price": round(charge_price[t], 4),
                "energy_price": round(energy_price[t], 4),
                "distribution": round(distribution[t], 4),
                "terminal_price": round(p_term, 4),
                "charge_threshold": round(charge_threshold, 4),
                "discharge_threshold": round(discharge_threshold, 4),
                "demand_kwh": round(demand[t], 3),
                "ev_kwh": round(ev, 3),
                "charge_kwh": round(stored_kwh, 3),
                "charge_efficiency": round(eff_t, 4) if eff_t is not None else None,
                "discharge_kwh": round(delivered, 3),
                "grid_buy_kwh": round(grid_buy, 3),
                "soc_before": round(soc_before, 1),
                "soc_after": round(battery.soc, 1),
                "battery_energy_cost_before": round(cost_before, 4),
                "reason": self._reason(
                    mode, tp, charge_threshold, discharge_threshold, stored_kwh, delivered
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
        """Allocate EV charging across the horizon.

        The charger is **on/off at full power** — it cannot dose a fraction of an
        hour. So reaching a deficit of ``D`` kWh means turning the charger on for
        ``K = ceil(D / P)`` hours (``P`` = full charge power); the car draws full
        power in each and its BMS tops off — leaving an unavoidable partial of
        ``D - (K-1)·P`` on whichever hour is **chronologically last** among the
        chosen ones (that is where charging actually finishes).

        Because that partial buys *less* energy, the cost-minimising choice is to
        spend full hours on the cheapest slots and let the partial land on a
        pricier slot — but only one that can legitimately be the top-off hour
        (i.e. has enough earlier hours to hold the full blocks). We therefore try
        every candidate top-off hour, pick the cheapest full blocks before it,
        and keep the globally cheapest combination. This is what stops the plan
        from e.g. wasting a whole expensive hour just to shave a few minutes.

        Three layers, never exceeding the pack's room to 100 %:

        1. **Forced windows** — bare ``"<keyword>"`` calendar events: charge full
           power in exactly those hours (user's explicit choice, not cost-driven).
        2. **Deadline targets** — ``"<keyword> NN%"`` events: cost-optimal blocks
           among available hours before the deadline (earliest deadline first).
        3. **Default top-up** — no calendar: cost-optimal blocks for the deficit
           to the target SoC.

        Predicted trip drain (``EVRequest.drain_kwh``) is folded in on both
        sides: deadline targets buy extra to cover driving that happens before
        the deadline, and the room-to-100 % cap credits energy the trips take
        back out of the pack.
        """
        if ev_request is None or not ev_request.is_actionable:
            return {}

        slots_by_start = {
            s.start: s for s in forecast.slots if s.buy_price is not None
        }
        charger_kw = max(ev_request.charger_power_kw, 0.1)
        battery_kwh = max(ev_request.battery_kwh, 0.0)
        soc0_kwh = (
            max(0.0, (ev_request.current_soc or 0.0) / 100.0 * battery_kwh)
            if battery_kwh
            else 0.0
        )

        allocation: dict[datetime, float] = {}

        def capacity_left() -> float:
            """Room to 100 % including predicted trip drain over the horizon.

            Drain frees room in the pack (the car returns from a trip lower
            than it left), so the routine top-up after a trip may buy the trip
            energy back — without this credit the allocator would treat the
            pack as still full and skip the recharge.
            """
            if battery_kwh <= 0:
                return float("inf")
            drained = sum(ev_request.drain_kwh.values())
            return max(
                0.0, battery_kwh - soc0_kwh + drained - sum(allocation.values())
            )

        def select(
            deficit: float, candidates: list, room: float | None = None
        ) -> dict[datetime, float]:
            """Cost-optimal on/off full-power hours for ``deficit`` kWh.

            ``candidates`` = eligible, not-yet-allocated slots. Returns full power
            on the cheapest blocks plus the unavoidable remainder on the cheapest
            valid top-off hour. The car fills the chosen hours chronologically, so
            the remainder must sit on the chronologically last chosen hour.
            ``room`` overrides the pack-room cap (deadline targets compute the
            room *at their deadline*, which the global estimate can't see).

            Placement preferences (``EVRequest.prefer_contiguous`` /
            ``prefer_early``) reshape the choice within a bounded extra cost:
            contiguity keeps the block unbroken unless the scattered optimum is
            more than ``contiguous_max_extra_pct`` % cheaper; earliness picks,
            among placements within ``early_max_extra_pct`` % of the cheapest,
            the one that finishes charging soonest.
            """
            deficit = min(deficit, capacity_left() if room is None else room)
            if deficit <= _EPS:
                return {}
            slots = sorted(
                (s for s in candidates if s.start not in allocation),
                key=lambda s: s.start,
            )
            if not slots:
                return {}
            needed = max(1, math.ceil(deficit / charger_kw - 1e-9))
            needed = min(needed, len(slots))  # can't reach target → use them all
            # Remainder on the top-off hour, in (0, P]. Clamps to full power when
            # there aren't enough hours to fully reach the target.
            remainder = min(max(deficit - (needed - 1) * charger_kw, _EPS), charger_kw)

            hour_step = timedelta(hours=1)

            def _contiguous(starts: list[datetime]) -> bool:
                return all(b - a == hour_step for a, b in zip(starts, starts[1:]))

            # One variant per candidate top-off hour: cheapest full blocks
            # before it. For contiguity also try the block of `needed-1` slots
            # *immediately* before the top-off (valid when they form an
            # unbroken run of clock hours ending at it).
            variants: list[tuple[float, datetime, list[datetime]]] = []
            for li in range(needed - 1, len(slots)):
                top_off = slots[li]
                fulls = sorted(slots[:li], key=lambda s: s.buy_price)[: needed - 1]
                if len(fulls) < needed - 1:
                    continue
                cost = (
                    charger_kw * sum(s.buy_price for s in fulls)
                    + remainder * top_off.buy_price
                )
                variants.append((cost, top_off.start, [s.start for s in fulls]))
                if needed > 1:
                    block = slots[li - (needed - 1) : li]
                    block_starts = [s.start for s in block] + [top_off.start]
                    if _contiguous(block_starts):
                        bcost = (
                            charger_kw * sum(s.buy_price for s in block)
                            + remainder * top_off.buy_price
                        )
                        variants.append((bcost, top_off.start, [s.start for s in block]))
            if not variants:
                return {}

            best_cost = min(v[0] for v in variants)

            def _within(cost: float, pct: float) -> bool:
                # Bounded extra spend relative to the optimum; |·| keeps the
                # bound meaningful when spot prices go negative.
                return cost <= best_cost + abs(best_cost) * pct / 100.0 + _EPS

            pool = variants
            if ev_request.prefer_contiguous and needed > 1:
                contiguous_pool = [
                    v
                    for v in variants
                    if _contiguous(sorted([*v[2], v[1]]))
                    and _within(v[0], ev_request.contiguous_max_extra_pct)
                ]
                # No block within budget → the gap is allowed (difference
                # exceeds the configured threshold).
                if contiguous_pool:
                    pool = contiguous_pool
            if ev_request.prefer_early:
                pool_best = min(v[0] for v in pool)
                budget = (
                    pool_best
                    + abs(pool_best) * ev_request.early_max_extra_pct / 100.0
                    + _EPS
                )
                eligible = [v for v in pool if v[0] <= budget]
                chosen = min(eligible, key=lambda v: (v[1], v[0]))
            else:
                chosen = min(pool, key=lambda v: v[0])

            _, top_off_start, full_starts = chosen
            out = {start: charger_kw for start in full_starts}
            out[top_off_start] = remainder
            return out

        def commit(chosen: dict[datetime, float]) -> None:
            for start, kwh in chosen.items():
                allocation[start] = allocation.get(start, 0.0) + kwh

        # 1. Forced windows: full power in the user's chosen hours, chronological
        #    cap (the car can't take more than its room to 100 %).
        for start in sorted(ev_request.forced_hours):
            if start not in ev_request.available_hours or start not in slots_by_start:
                continue
            take = min(charger_kw, capacity_left())
            if take <= _EPS:
                break
            allocation[start] = take

        # 2. Deadline targets, earliest deadline first. Predicted trip drain
        #    before a deadline lowers what's in the pack when it arrives, so the
        #    allocator has to buy that much more to still hit the target.
        def drain_before(moment: datetime) -> float:
            return sum(
                kwh
                for start, kwh in ev_request.drain_kwh.items()
                if start < moment
            )

        for target in sorted(ev_request.targets, key=lambda t: t.deadline):
            target_kwh = (
                target.target_soc / 100.0 * battery_kwh if battery_kwh else 0.0
            )
            before = (
                soc0_kwh
                - drain_before(target.deadline)
                + sum(
                    kwh for start, kwh in allocation.items() if start < target.deadline
                )
            )
            candidates = [
                slot
                for start, slot in slots_by_start.items()
                if start < target.deadline and start in ev_request.available_hours
            ]
            room_at_deadline = max(0.0, battery_kwh - before) if battery_kwh else None
            commit(select(target_kwh - before, candidates, room=room_at_deadline))

        # 3. Default top-up (no calendar plan).
        if ev_request.required_kwh > _EPS:
            candidates = [
                slot
                for start, slot in slots_by_start.items()
                if start in ev_request.available_hours
            ]
            commit(select(ev_request.required_kwh, candidates))

        return allocation
