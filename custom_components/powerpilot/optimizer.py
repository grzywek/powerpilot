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
# allocator or a stale import.
EV_ALLOCATOR_VERSION = "amp-fit-efficiency-2026-07"


def _ev_efficiency_at(curve: list[dict], power_kw: float) -> float:
    """AC charging efficiency at a total charging power (linear interpolation).

    ``curve`` holds {"kw","eff"} samples from a measured chart; outside the
    sampled range the nearest end value applies. No curve → lossless (1.0),
    the legacy behaviour.
    """
    points = sorted(
        (float(p["kw"]), min(max(float(p["eff"]), 0.01), 1.0))
        for p in (curve or [])
        if float(p.get("kw", 0.0)) > _EPS
    )
    if not points:
        return 1.0
    if power_kw <= points[0][0]:
        return points[0][1]
    if power_kw >= points[-1][0]:
        return points[-1][1]
    for (x1, y1), (x2, y2) in zip(points, points[1:]):
        if x1 <= power_kw <= x2:
            if x2 - x1 <= _EPS:
                return y2
            return y1 + (y2 - y1) * (power_kw - x1) / (x2 - x1)
    return points[-1][1]


@dataclass
class EVAllocation:
    """Per-hour EV charging schedule in both energy domains.

    ``added`` is what lands in the pack (drives the SoC forecast and compares
    against the car's energy-added sensor); ``grid`` is what flows through the
    meter (drives cost and the LP's grid load) — they differ by the charging
    efficiency at the hour's power. ``amps`` carries the planned charging
    current when current control is active (None per hour otherwise).

    The mapping interface exposes the primary (pack-side) schedule — an
    ``EVAllocation`` reads like the ``{hour: added kWh}`` dict it replaced.
    """

    added: dict[datetime, float] = field(default_factory=dict)
    grid: dict[datetime, float] = field(default_factory=dict)
    amps: dict[datetime, int] = field(default_factory=dict)

    def __getitem__(self, key: datetime) -> float:
        return self.added[key]

    def get(self, key: datetime, default=None):
        return self.added.get(key, default)

    def items(self):
        return self.added.items()

    def keys(self):
        return self.added.keys()

    def values(self):
        return self.added.values()

    def __iter__(self):
        return iter(self.added)

    def __len__(self) -> int:
        return len(self.added)

    def __bool__(self) -> bool:
        return bool(self.added)

    def __contains__(self, key: datetime) -> bool:
        return key in self.added

    def __eq__(self, other) -> bool:
        if isinstance(other, dict):
            return self.added == other
        if isinstance(other, EVAllocation):
            return (
                self.added == other.added
                and self.grid == other.grid
                and self.amps == other.amps
            )
        return NotImplemented


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

        ev_alloc = self._plan_ev(forecast, ev_request)
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
        # The LP and every cost see GRID energy; the pack-side ("added") energy
        # and the fitted amps ride along into the plan for SoC/control.
        ev_kwh = [ev_alloc.grid.get(slots[t].start, 0.0) for t in range(n)]

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
            ev_alloc=ev_alloc,
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
        ev_alloc: EVAllocation | None = None,
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
        # The projected EV SoC can never exceed the active charge ceiling — the
        # car's BMS stops there, so a line climbing past it would be fiction.
        ev_ceiling_kwh = (
            max(0.0, min(100.0, ev_request.charge_ceiling_soc)) / 100.0 * ev_battery_kwh
            if ev_request is not None and ev_request.charge_ceiling_soc is not None
            else ev_battery_kwh
        )
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
            # ``ev`` (grid kWh) drives cost/grid load; ``ev_added`` (pack kWh,
            # = grid × η at the hour's power) drives the SoC forecast and is
            # what the car's energy-added sensor will report back.
            ev = ev_kwh[t]
            ev_added = (
                ev_alloc.added.get(slot.start, ev) if ev_alloc is not None else ev
            )
            ev_amps = ev_alloc.amps.get(slot.start) if ev_alloc is not None else None
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
            # Pack-side ("added") energy: what the car's energy-added sensor
            # will report, what the chart bars/snapshots compare against and
            # what the SoC forecast integrates. The grid-side draw (losses
            # included) rides along for cost/grid accounting.
            decision.ev_charge_kwh = ev_added
            decision.ev_grid_kwh = ev
            decision.ev_charge_amps = ev_amps
            if ev_soc_kwh is not None:
                drain = ev_drain.get(slot.start, 0.0)
                ev_soc_kwh = min(
                    ev_ceiling_kwh, max(0.0, ev_soc_kwh + ev_added - drain)
                )
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
                "ev_kwh": round(ev_added, 3),
                "ev_grid_kwh": round(ev, 3),
                "ev_amps": ev_amps,
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
    ) -> EVAllocation:
        """Allocate EV charging across the horizon.

        The allocator sizes hours in **pack energy** ("added" kWh): a full
        charge hour adds ``P·η(P)`` at full power, so reaching a deficit of
        ``D`` kWh means ``K = ceil(D / (P·η))`` hours — full hours on the
        cheapest slots and the unavoidable remainder on whichever chosen hour
        is **chronologically last** (that is where charging actually
        finishes). We try every candidate top-off hour, pick the cheapest full
        blocks before it, and keep the globally cheapest combination (costs
        are grid-side: added energy ÷ the efficiency at the hour's power).

        The top-off hour depends on the current-control mode:

        * **full power** (legacy) — the car draws full power and its BMS stops
          mid-hour once the remainder is in;
        * **current control** — the hour runs at the smallest configured
          current whose full-hour yield covers the remainder, so a small
          top-up charges gently across the hour instead of a full-power
          burst; the chosen amps ship in the returned allocation.

        Three layers, never exceeding the request's SoC ceiling
        (``charge_ceiling_soc`` — the car is steered to stop there):

        1. **Forced windows** — bare ``"<keyword>"`` calendar events: charge full
           power in exactly those hours (user's explicit choice, not cost-driven).
        2. **Deadline targets** — ``"<keyword> NN%"`` events: cost-optimal blocks
           among available hours before the deadline (earliest deadline first).
        3. **Default top-up** — no calendar: cost-optimal blocks for the deficit
           to the target SoC.

        Predicted trip drain (``EVRequest.drain_kwh``) is folded in on both
        sides: deadline targets buy extra to cover driving the car can still
        recharge after (never the leg driven off after the last chargeable
        hour), and the room-to-ceiling cap credits energy the trips take back
        out of the pack.
        """
        if ev_request is None or not ev_request.is_actionable:
            return EVAllocation()

        slots_by_start = {
            s.start: s for s in forecast.slots if s.buy_price is not None
        }

        def slot_price(slot) -> float:
            """Per-kWh price the EV actually pays in a slot.

            The charger is grid-fed, so it pays the FULL price — energy plus
            distribution — the same composition the battery LP optimises on.
            Ordering by ``buy_price`` alone picked hours that looked cheap on
            the energy component but were expensive once the TOU distribution
            band was added. Bucketed to the displayed grosz (like the battery
            charge order) so sub-grosz noise can't reshuffle equal-looking
            hours between planning cycles; sorts are stable, so grosz ties
            stay chronological.
            """
            return _charge_order_price(
                slot.buy_price + (slot.distribution_price_kwh or 0.0)
            )

        # Power/efficiency model. The allocator sizes hours in pack ("added")
        # energy; grid energy = added ÷ η(P) at the hour's charging power.
        eff_curve = ev_request.efficiency_curve
        p_full = max(ev_request.charger_power_kw, 0.1)
        eff_full = _ev_efficiency_at(eff_curve, p_full)
        current_control = (
            ev_request.current_control
            and 1 <= ev_request.min_current_a <= ev_request.max_current_a
        )

        def fit_for_added(added: float) -> tuple[float, int | None]:
            """(grid kWh, amps) to put ``added`` kWh into the pack in one hour.

            With current control: the smallest whole-amp level whose full-hour
            yield covers ``added`` (gentle top-off); the car's BMS still stops
            once the energy is in, so a need below the minimum level's yield
            simply ends mid-hour. Without: full power, mid-hour BMS stop.
            """
            if current_control:
                for amps in range(
                    ev_request.min_current_a, ev_request.max_current_a + 1
                ):
                    p = ev_request.power_at_amps(amps)
                    eff = _ev_efficiency_at(eff_curve, p)
                    if p * eff + _EPS >= added:
                        return added / eff, amps
                return added / eff_full, ev_request.max_current_a
            return added / eff_full, None

        # Full-hour yield (added kWh) — the per-hour allocation cap.
        hour_cap_added = max(p_full * eff_full, 0.1)
        battery_kwh = max(ev_request.battery_kwh, 0.0)
        # Highest SoC the planner may intentionally buy to. The car/charger is
        # steered off ``soc_limit_now`` and stops there, so energy planned past
        # the ceiling is undeliverable by construction.
        ceiling_kwh = (
            max(0.0, min(100.0, ev_request.charge_ceiling_soc)) / 100.0 * battery_kwh
            if ev_request.charge_ceiling_soc is not None and battery_kwh > 0
            else (battery_kwh if battery_kwh > 0 else float("inf"))
        )
        soc_known = ev_request.current_soc is not None
        soc0_kwh = (
            max(0.0, (ev_request.current_soc or 0.0) / 100.0 * battery_kwh)
            if battery_kwh
            else 0.0
        )

        allocation: dict[datetime, float] = {}

        def capacity_left() -> float:
            """Room to the SoC ceiling including predicted trip drain.

            Drain frees room in the pack (the car returns from a trip lower
            than it left), so the routine top-up after a trip may buy the trip
            energy back — without this credit the allocator would treat the
            pack as still full and skip the recharge.
            """
            if battery_kwh <= 0:
                return float("inf")
            drained = sum(ev_request.drain_kwh.values())
            return max(
                0.0, ceiling_kwh - soc0_kwh + drained - sum(allocation.values())
            )

        def select(
            deficit: float, candidates: list, room: float | None = None
        ) -> dict[datetime, float]:
            """Cost-optimal on/off full-power hours for ``deficit`` kWh.

            ``candidates`` = eligible slots; hours partially taken by an
            earlier commitment keep their remaining headroom (topping them up
            is the same physical full-power hour). Returns headroom-filling on
            the cheapest hours plus the unavoidable remainder on the cheapest
            valid top-off hour. The car fills the chosen hours chronologically,
            so the remainder must sit on the chronologically last chosen hour.
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
            # Hours already carrying a partial allocation stay eligible with
            # their remaining headroom — an earlier small target (a trip floor)
            # must not fence the cheapest hour off from a later, bigger target
            # and scatter its block over pricier hours. Physically the charger
            # simply runs at full power through the union of the chosen hours.
            slots = sorted(
                (
                    s
                    for s in candidates
                    if hour_cap_added - allocation.get(s.start, 0.0) > _EPS
                ),
                key=lambda s: s.start,
            )
            if not slots:
                return {}
            caps = {
                s.start: hour_cap_added - allocation.get(s.start, 0.0) for s in slots
            }
            # Every variant must deliver the same energy or it isn't comparable
            # by cost (an under-filled variant would win on price alone).
            deliverable = min(deficit, sum(caps.values()))

            hour_step = timedelta(hours=1)

            def _contiguous(starts: list[datetime]) -> bool:
                # Consecutive clock hours; hours already charging from an
                # earlier commitment bridge gaps — the charger doesn't switch
                # off there, so the physical session is still unbroken.
                for a, b in zip(starts, starts[1:]):
                    h = a + hour_step
                    while h < b:
                        if allocation.get(h, 0.0) <= _EPS:
                            return False
                        h += hour_step
                return True

            def _variant(
                top_off, fulls: list
            ) -> tuple[float, datetime, list[datetime], dict[datetime, float]]:
                covered = sum(caps[s.start] for s in fulls)
                rem = deliverable - covered  # in (0, cap(top_off)]
                # Grid-side cost: full hours end at full power (÷ η_full); the
                # top-off runs at the current fitted to the hour's final
                # (existing + remainder) added energy, at that power's η.
                total_top_added = allocation.get(top_off.start, 0.0) + rem
                _grid, amps = fit_for_added(total_top_added)
                p_top_kw = (
                    ev_request.power_at_amps(amps) if amps is not None else p_full
                )
                eff_top = _ev_efficiency_at(eff_curve, p_top_kw)
                cost = (
                    sum(caps[s.start] / eff_full * slot_price(s) for s in fulls)
                    + rem / eff_top * slot_price(top_off)
                )
                fill = {s.start: caps[s.start] for s in fulls}
                fill[top_off.start] = rem
                return (cost, top_off.start, [s.start for s in fulls], fill)

            # One variant per candidate top-off hour: hours before it filled to
            # their headroom, cheapest first — forced ones until the top-off can
            # absorb the rest, then only hours cheaper than the top-off (each
            # displaces top-off energy), keeping a positive remainder on it.
            variants: list[
                tuple[float, datetime, list[datetime], dict[datetime, float]]
            ] = []
            for li, top_off in enumerate(slots):
                cap_top = caps[top_off.start]
                by_price = sorted(slots[:li], key=slot_price)
                fulls: list = []
                covered = 0.0
                i = 0
                while deliverable - covered > cap_top + _EPS and i < len(by_price):
                    fulls.append(by_price[i])
                    covered += caps[by_price[i].start]
                    i += 1
                if deliverable - covered > cap_top + _EPS:
                    continue  # this top-off can't hold the remainder
                p_top = slot_price(top_off)
                while i < len(by_price):
                    s = by_price[i]
                    i += 1
                    if slot_price(s) >= p_top:
                        break  # price-sorted → nothing cheaper follows
                    if deliverable - covered - caps[s.start] <= _EPS:
                        continue  # would leave no top-off remainder
                    fulls.append(s)
                    covered += caps[s.start]
                variants.append(_variant(top_off, fulls))
                # Contiguity candidate: the run of slots *immediately* before
                # the top-off, just long enough that the remainder fits on it
                # (valid when their clock hours chain up to the top-off).
                if fulls:
                    bfulls: list = []
                    bcov = 0.0
                    j = li - 1
                    while j >= 0 and deliverable - bcov > cap_top + _EPS:
                        bfulls.append(slots[j])
                        bcov += caps[slots[j].start]
                        j -= 1
                    block = list(reversed(bfulls))
                    if deliverable - bcov <= cap_top + _EPS and _contiguous(
                        [s.start for s in block] + [top_off.start]
                    ):
                        variants.append(_variant(top_off, block))
            if not variants:
                return {}

            best_cost = min(v[0] for v in variants)

            def _within(cost: float, pct: float) -> bool:
                # Bounded extra spend relative to the optimum; |·| keeps the
                # bound meaningful when spot prices go negative.
                return cost <= best_cost + abs(best_cost) * pct / 100.0 + _EPS

            pool = variants
            if ev_request.prefer_contiguous:
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

            return dict(chosen[3])

        def commit(chosen: dict[datetime, float]) -> None:
            for start, kwh in chosen.items():
                allocation[start] = allocation.get(start, 0.0) + kwh

        # 1. Forced windows: full power in the user's chosen hours, chronological
        #    cap (the car can't take more than its room to the SoC ceiling).
        for start in sorted(ev_request.forced_hours):
            if start not in ev_request.available_hours or start not in slots_by_start:
                continue
            take = min(hour_cap_added, capacity_left())
            if take <= _EPS:
                break
            allocation[start] = take

        # 2. Deadline targets, earliest deadline first. Predicted trip drain
        #    lowers what's in the pack, so the allocator buys that much more to
        #    still hit the target — but only drain the car can charge *after*:
        #    energy driven off past the last chargeable hour (the outbound leg
        #    right before a trip deadline) cannot be bought back in time, and
        #    crediting it overfilled the pack past its ceiling at departure.
        #    Sizing a % deficit needs the live SoC; when the sensor is asleep
        #    the targets are skipped rather than seeded from a fabricated 0 %
        #    (which used to swing the plan by half a pack between cycles).
        def drain_before(moment: datetime) -> float:
            return sum(
                kwh
                for start, kwh in ev_request.drain_kwh.items()
                if start < moment
            )

        targets = sorted(ev_request.targets, key=lambda t: t.deadline)
        if targets and not soc_known and battery_kwh > 0:
            _LOGGER.warning(
                "EV: SoC niedostępny — cele procentowe pominięte w tym cyklu "
                "(nie da się policzyć deficytu bez stanu naładowania)."
            )
            targets = []
        for target in targets:
            target_kwh = (
                min(target.target_soc / 100.0 * battery_kwh, ceiling_kwh)
                if battery_kwh
                else 0.0
            )
            candidates = [
                slot
                for start, slot in slots_by_start.items()
                if start < target.deadline and start in ev_request.available_hours
            ]
            last_charge_end = (
                max(s.start for s in candidates) + timedelta(hours=1)
                if candidates
                else target.deadline
            )
            compensable = min(last_charge_end, target.deadline)
            before = (
                soc0_kwh
                - drain_before(compensable)
                + sum(
                    kwh for start, kwh in allocation.items() if start < target.deadline
                )
            )
            room_at_deadline = max(0.0, ceiling_kwh - before) if battery_kwh else None
            commit(select(target_kwh - before, candidates, room=room_at_deadline))

        # 3. Default top-up (no calendar plan).
        if ev_request.required_kwh > _EPS:
            candidates = [
                slot
                for start, slot in slots_by_start.items()
                if start in ev_request.available_hours
            ]
            commit(select(ev_request.required_kwh, candidates))

        # Convert the added-energy schedule into the dual-domain allocation:
        # grid energy (cost / LP load) and the fitted charging current.
        out = EVAllocation()
        for start, added in allocation.items():
            if added <= _EPS:
                continue
            grid, amps = fit_for_added(added)
            out.added[start] = added
            out.grid[start] = grid
            if amps is not None:
                out.amps[start] = amps
        return out
