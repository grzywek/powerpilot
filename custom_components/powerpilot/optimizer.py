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

The stored energy (kWh) is an explicit state column per hour::

    E[t] = E[t-1] + c[t]·η_ch − d[t]/η_dis          (E[-1] = E0)

bounded by the usable SoC band ``[E_min, E_max]``. The household demand of
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

Monthly price cap and the apartment's supply
---------------------------------------------
With billing periods each month's energy is settled as ``K = min(C, L·E)``
(the seller's cap); the per-hour columns then carry only the part of the price that stays outside the
settlement (distribution, excise). While supply detection is active the
apartment's forecast ``A[t]`` (``HourSlot.apartment_kwh``) is kept apart from
the Victron's own demand ``F[t]``. Without switching it is fixed to the actual
supply (unknown → Victron) and simply joins or leaves ``F`` — the model above,
unchanged. With switching (stage 3, ``SupplyPlanning``):

* ``s[t] ∈ {0,1}`` – apartment on meter 2 (PPE 2). Hours between two allowed
  switch boundaries share one column; the running hour's is fixed to the
  actual supply, and from the switch horizon ``SUPPLY_SWITCH_HOURS`` on (the
  month tail included) every hour keeps the last switch hour's column.
  A boundary allows a switch only outside the quiet window, while the user is
  available and at least the minimum stay ``H`` after the actual last
  transition; each switch keeps its source for ``H`` hours
  (``s[t] − s[t−1] ≤ s[τ]``, ``s[t−1] − s[t] ≤ 1 − s[τ]``, ``τ ∈ [t, t+H)``).
* ``o[t] ≥ s[t] − s[t−1]`` – a cycle opened (to meter 2), priced at the cycle
  threshold; returning is free.
* ``w[t] = z[t]·s[t]`` (McCormick: ``w ≤ z``, ``w ≤ s``, ``w ≥ z + s − 1``) –
  the discharge delivered is ``D0·z − (D0 − D1)·w`` with
  ``D0 = min(F + A, inverter)`` and ``D1 = min(F, inverter)``: on meter 2 the
  battery covers only the Victron's own demand.
* PPE 1 imports ``g = F + A − A·s + EV + Σc − discharge``; PPE 2 imports
  ``A·s`` (an expression). Every per-hour price is the same on both meters,
  so ``s`` acts only through the discharge and the two settlements: PPE 2's
  months ``K2 = min(C2_mtd + Σ cap·A·s, L·(E2_mtd + Σ A·s))`` are settled
  next to PPE 1's.

``min(C, L·E)`` is concave, so it is not modelled with a regime binary and a
big-M (whose weak relaxation made the month tail slow). The cap regimes are
enumerated instead: every combination of "cap does not bind" (the period
costs ``C``) and "cap binds" (it costs ``L·E``) over all periods of both
meters is solved with those linear costs, and the cheapest wins. Minimising
over the plan and over the regimes commute, and at the optimum each chosen
term is the min, so the settlement is exact — ``2^P`` solves of one model
whose objective alone changes.

The solver output is replayed through :class:`BatteryModel` so the reported
``battery_energy_cost`` / ``battery_use_cost`` and the per-hour cost breakdown
stay consistent with the rest of the integration.
"""

from __future__ import annotations

import itertools
import logging
import math
import statistics
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

import highspy
import numpy as np
from homeassistant.util import dt as dt_util

from .battery import RECOVERY_MARGIN_SOC, BatteryModel
from .const import SUPPLY_METER_2, SUPPLY_VICTRON, ChargePower, InverterMode
from .models import (
    BillingOutcome,
    BillingPeriod,
    Decision,
    ESSRequest,
    Forecast,
    MonthTail,
    Plan,
    SupplyPlanning,
)
from .modules.ev import EVRequest
from .when import moment_label

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
EV_ALLOCATOR_VERSION = "pack-level-aware-2026-08"

# A later cap-regime combination replaces the best one only when it is cheaper
# by more than this (PLN) — the MIP's absolute gap (``mip_abs_gap``), below
# which two optima are indistinguishable.
_REGIME_TIE_PLN = 1e-7

# Switch horizon ``T_sw`` (hours from the running one): the apartment's supply
# may change only on the boundaries before it; later hours, and the month's
# tail, keep the supply of hour ``T_sw − 1``. Set from the solve-time
# measurement in ``scripts/bench_supply_model.py`` (stage 3 plan, decision 7,
# re-measured after the cap regimes became an enumeration): the whole week
# took 4× longer than 72 h on the base case, and the hourly re-plan brings a
# later cycle into the window in time.
SUPPLY_SWITCH_HOURS = 72


@dataclass
class EVAllocation:
    """Per-hour EV charging schedule in both energy domains.

    ``added`` is what lands in the pack (drives the SoC forecast and compares
    against the car's energy-added sensor); ``grid`` is what flows through the
    meter (drives cost and the LP's grid load) — they differ by the flat
    charging efficiency. ``minutes`` is how long (within the hour, at full
    charger power) the charger should run to deliver the hour's energy — the
    user's automation starts/stops the charger off it.

    The mapping interface exposes the primary (pack-side) schedule — an
    ``EVAllocation`` reads like the ``{hour: added kWh}`` dict it replaced.
    """

    added: dict[datetime, float] = field(default_factory=dict)
    grid: dict[datetime, float] = field(default_factory=dict)
    minutes: dict[datetime, int] = field(default_factory=dict)

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
                and self.minutes == other.minutes
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


@dataclass
class _CapPeriod:
    """A billing period mapped onto the LP's hour indices."""

    period: BillingPeriod
    hours: list[int]


@dataclass
class _SwitchModel:
    """The apartment's supply columns ``s`` mapped onto the LP's hours.

    Hours between two allowed switch boundaries share one column (``s[t] =
    s[t−1]`` wherever no switch may happen), so ``segment[t]`` is the column
    index of hour ``t`` and ``starts[k]`` the first hour of column ``k``.
    Column 0 holds the running hour and is fixed to the actual supply.
    """

    segment: list[int]
    starts: list[int]
    actual: float  # 1.0 = on meter 2
    threshold: float  # zł per opened cycle
    min_stay: int  # hours
    # Columns held on the Victron (``SupplyPlanning.victron_hours``).
    victron: frozenset[int] = frozenset()


@dataclass
class _LPSolution:
    """Per-hour flows the solver settled on (planning horizon + month tail)."""

    charge: list[float]  # grid kWh into the battery
    discharge: list[float]  # kWh delivered to the house from the battery
    stored: list[float]  # post-loss kWh stored
    grid: list[float]  # grid import (PPE 1)
    tail_ev: list[float]  # expected EV import placed in tail hours
    periods: list[tuple[float, float, bool]]  # (energy, pre-cap cost, cap binds)
    # Apartment on meter 2 per hour (1.0) and PPE 2's settled months; empty
    # unless switching was modelled.
    supply: list[float] = field(default_factory=list)
    ppe2_periods: list[tuple[float, float, bool]] = field(default_factory=list)
    # The winning objective without the cycle thresholds (PLN).
    objective: float = 0.0


class Optimizer:
    """Produces a cost-optimal :class:`Plan` via a HiGHS linear program."""

    def __init__(self, config: OptimizerConfig) -> None:
        self.config = config
        # Cap-regime combinations of the last solve and the wall time (s) of
        # every MIP it ran — pruned combinations run none (diagnostics and
        # ``scripts/bench_supply_model.py``).
        self.last_solve_seconds: list[float] = []
        self.last_regime_combinations = 0

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------
    def optimize(
        self,
        forecast: Forecast,
        battery: BatteryModel,
        ev_request: EVRequest | None = None,
        reminders: list[str] | None = None,
        ess_request: ESSRequest | None = None,
        billing: list[BillingPeriod] | None = None,
        tail: MonthTail | None = None,
        supply: SupplyPlanning | None = None,
    ) -> Plan:
        """Plan the horizon; with ``billing`` also settle each month under its cap.

        ``billing`` lists the billing periods (months under the seller's price
        cap) the plan touches, with their observed month-to-date position;
        ``tail`` carries the estimated hours from the end of the horizon to the
        end of the last such month. Both are required together for the cap to
        be modelled — the tail alone is meaningless and is ignored. Every hour
        inside a capped month must carry ``cap_price_kwh`` and every tail hour
        a price; hours of uncapped months keep their hourly energy price. The
        caller validates completeness and reports gaps, this method only
        refuses to guess (``ValueError``).

        ``supply`` (while supply detection is active) places each slot's
        ``apartment_kwh`` on the Victron or on meter 2: fixed to the actual
        supply (unknown → Victron) unless switching is modelled, when the
        plan chooses it hour by hour under the switching rules and settles
        PPE 2's months (``supply.ppe2_periods``) next to PPE 1's. Switching
        needs both meters' periods; without them it is refused.
        """
        battery = battery.copy()  # never mutate the live state
        slots = forecast.slots
        n = len(slots)
        if n == 0:
            return Plan(forecast=forecast, decisions=[], created_at=dt_util.now())
        switching = supply is not None and supply.switching
        if switching and not (billing and supply.ppe2_periods):
            raise ValueError("switching the apartment needs PPE 1's and PPE 2's periods")
        tail_slots = list(tail.slots) if (tail is not None and billing) else []
        all_slots = slots + tail_slots
        total = len(all_slots)

        # The first slot covers the clock hour we are already inside — only its
        # REMAINDER is plannable. Every per-hour quantity of slot 0 (charge and
        # discharge caps, consumption, connection headroom, EV charger yield) is
        # scaled by this fraction, so a mid-hour re-plan works on a physically
        # correct model instead of pretending a full hour of capacity is left.
        # Future slots are whole hours (fraction 1).
        now = dt_util.now()
        first_frac = 1.0
        if slots[0].start <= now < slots[0].start + timedelta(hours=1):
            first_frac = (
                slots[0].start + timedelta(hours=1) - now
            ).total_seconds() / 3600.0
        fracs = [first_frac] + [1.0] * (n - 1) + [1.0] * len(tail_slots)

        ev_alloc = self._plan_ev(forecast, ev_request, first_frac)
        ev_charger_kw = ev_request.charger_kw if ev_request else 0.0

        # Per-hour prices and demand. Missing energy prices inside the horizon
        # fall back to the median so the SoC chain stays continuous; a tail
        # hour without a price is a gap the caller should have reported.
        prices = [s.buy_price for s in slots if s.buy_price is not None]
        median_price = statistics.median(prices) if prices else 0.0
        energy_price = [
            s.buy_price if s.buy_price is not None else median_price for s in slots
        ]
        for slot in tail_slots:
            if slot.buy_price is None:
                raise ValueError(f"tail hour {slot.start.isoformat()} has no price")
            energy_price.append(slot.buy_price)
        distribution = [s.distribution_price_kwh or 0.0 for s in all_slots]
        total_price = [energy_price[t] + distribution[t] for t in range(total)]
        charge_order_price = [_charge_order_price(price) for price in total_price]
        # Victron-side demand without the apartment, and the apartment's own
        # forecast, both over the slot's plannable window.
        demand = [
            max(0.0, s.total_consumption_kwh) * fracs[t]
            for t, s in enumerate(all_slots)
        ]
        apartment = [
            max(0.0, s.apartment_kwh) * fracs[t] for t, s in enumerate(all_slots)
        ]
        # The LP and every cost see GRID energy; the pack-side ("added") energy
        # and the fitted amps ride along into the plan for SoC/control. The
        # tail carries no planned EV hours — its expected EV import is placed
        # by the solver itself.
        ev_kwh = [ev_alloc.grid.get(slots[t].start, 0.0) for t in range(n)]
        ev_kwh += [0.0] * len(tail_slots)

        # Energy price each hour moves into its month's settlement: the cap
        # price inside a capped month, nothing outside one (that hour's energy
        # stays billed at the hourly price).
        cap_price: list[float] | None = None
        periods: list[_CapPeriod] = []
        if billing:
            for period in billing:
                hours = [
                    t
                    for t, slot in enumerate(all_slots)
                    if period.month_start <= slot.start < period.month_end
                ]
                if hours:
                    periods.append(_CapPeriod(period=period, hours=hours))
            cap_price = [0.0] * total
            for cp in periods:
                for t in cp.hours:
                    price = all_slots[t].cap_price_kwh
                    if price is None:
                        raise ValueError(
                            f"hour {all_slots[t].start.isoformat()} has no cap price"
                        )
                    cap_price[t] = price

        # PPE 2's months: the same months as PPE 1's (both meters are settled
        # under the same editions), so every hour of one carries its cap price.
        ppe2_periods: list[_CapPeriod] = []
        if supply is not None and billing:
            ppe1_months = {cp.period.month_start for cp in periods}
            for period in supply.ppe2_periods:
                hours = [
                    t
                    for t, slot in enumerate(all_slots)
                    if period.month_start <= slot.start < period.month_end
                ]
                if not hours:
                    continue
                if period.month_start not in ppe1_months:
                    raise ValueError(
                        f"PPE 2 month {period.month_start.isoformat()} has no PPE 1 month"
                    )
                ppe2_periods.append(_CapPeriod(period=period, hours=hours))

        # Without switching the apartment sits where it is for every hour and
        # simply joins (or leaves) the Victron demand — the stage-2 model.
        fixed_s = 1.0 if supply is not None and supply.actual == SUPPLY_METER_2 else 0.0
        switch = (
            self._switch_model(all_slots, supply, n) if switching else None
        )
        lp_demand = (
            demand
            if switch is not None
            else [demand[t] + apartment[t] * (1.0 - fixed_s) for t in range(total)]
        )

        solution = self._solve_lp(
            battery=battery,
            total_price=total_price,
            demand=lp_demand,
            ev_kwh=ev_kwh,
            ev_charger_kw=ev_charger_kw,
            charge_order_price=charge_order_price,
            fracs=fracs,
            slots=all_slots,
            ess_request=ess_request,
            cap_price=cap_price,
            periods=periods,
            tail_start=n,
            tail_ev_kwh=tail.ev_grid_kwh if (tail is not None and billing) else 0.0,
            tail_ev_cap_kw=tail.ev_charger_kw if (tail is not None and billing) else 0.0,
            apartment=apartment if switch is not None else None,
            switch=switch,
            ppe2_periods=ppe2_periods if switch is not None else None,
        )
        on_meter_2 = solution.supply if switch is not None else [fixed_s] * total
        ppe2_import = [apartment[t] * on_meter_2[t] for t in range(total)]
        victron_demand = [demand[t] + apartment[t] - ppe2_import[t] for t in range(total)]

        plan = self._build_plan(
            forecast=forecast,
            battery=battery,
            charge=solution.charge[:n],
            discharge=solution.discharge[:n],
            stored=solution.stored[:n],
            energy_price=energy_price[:n],
            distribution=distribution[:n],
            total_price=total_price[:n],
            demand=victron_demand[:n],
            ev_kwh=ev_kwh[:n],
            ev_alloc=ev_alloc,
            charge_order_price=charge_order_price[:n],
            ev_request=ev_request,
            reminders=reminders,
            fracs=fracs[:n],
            # Only hours of capped months carry a cap price into the trace.
            cap_price=(
                [
                    cap_price[t] if any(t in cp.hours for cp in periods) else None
                    for t in range(n)
                ]
                if cap_price is not None
                else None
            ),
            supply=(
                [
                    SUPPLY_METER_2 if on_meter_2[t] > 0.5 else SUPPLY_VICTRON
                    for t in range(n)
                ]
                if supply is not None
                else None
            ),
            apartment=apartment[:n],
        )
        plan.billing = self._billing_outcomes(
            periods, solution.periods, solution.grid, n
        )
        if ppe2_periods:
            settled2 = (
                solution.ppe2_periods
                if switch is not None
                else self._fixed_settlements(ppe2_periods, ppe2_import, cap_price)
            )
            plan.billing += self._billing_outcomes(
                ppe2_periods, settled2, ppe2_import, n
            )
        plan.supply_switching = switching
        plan.supply_reason = supply.switching_reason if supply is not None else None
        plan.comparable_cost_pln = solution.objective
        if switch is not None:
            plan.supply_boundaries = [all_slots[t].start for t in switch.starts[1:]]
        return plan

    @staticmethod
    def _switch_model(
        slots: list, supply: SupplyPlanning, horizon: int
    ) -> _SwitchModel:
        """Which hour boundaries may move the apartment, as shared ``s`` columns.

        Boundary ``t`` (between hours ``t−1`` and ``t``) is a switch hour only
        before the switch horizon, outside the quiet window (by the local
        start hour of slot ``t``), while the user is available, and once the
        actual last transition is at least the minimum stay old. A column
        holding one of ``supply.victron_hours`` is held on the Victron; the
        running hour's cannot be while the apartment is on meter 2.
        """
        t_sw = min(SUPPLY_SWITCH_HOURS, horizon)
        stay = timedelta(hours=supply.min_stay_hours)
        earliest = (
            supply.last_transition_at + stay
            if supply.last_transition_at is not None
            else None
        )
        segment: list[int] = []
        starts = [0]
        for t, slot in enumerate(slots):
            if (
                0 < t < t_sw
                and not supply.in_quiet_window(dt_util.as_local(slot.start).hour)
                and supply.user_available(slot.start)
                and (earliest is None or slot.start >= earliest)
            ):
                starts.append(t)
            segment.append(len(starts) - 1)
        actual = 1.0 if supply.actual == SUPPLY_METER_2 else 0.0
        victron = frozenset(
            segment[t] for t, slot in enumerate(slots) if slot.start in supply.victron_hours
        )
        if 0 in victron and actual > 0.5:
            raise ValueError(
                "the running hour cannot be held on the Victron: the apartment is on meter 2"
            )
        return _SwitchModel(
            segment=segment,
            starts=starts,
            actual=actual,
            threshold=supply.cycle_threshold_pln,
            min_stay=supply.min_stay_hours,
            victron=victron,
        )

    @staticmethod
    def _fixed_settlements(
        periods: list[_CapPeriod], imports: list[float], cap_price: list[float] | None
    ) -> list[tuple[float, float, bool]]:
        """``(energy, cost, cap binds)`` of months whose import is fixed."""
        settled = cap_price or [0.0] * len(imports)
        out: list[tuple[float, float, bool]] = []
        for cp in periods:
            energy = cp.period.energy_kwh + sum(imports[t] for t in cp.hours)
            cost = cp.period.cost_pln + sum(settled[t] * imports[t] for t in cp.hours)
            out.append((energy, cost, cp.period.limit_kwh * energy < cost))
        return out

    @staticmethod
    def _billing_outcomes(
        periods: list[_CapPeriod],
        settled: list[tuple[float, float, bool]],
        imports: list[float],
        horizon: int,
    ) -> list[BillingOutcome]:
        """Month forecasts per billing period from the settled flows of one PPE."""
        outcomes: list[BillingOutcome] = []
        for cp, (energy, cost, binds) in zip(periods, settled):
            planned = sum(imports[t] for t in cp.hours if t < horizon)
            tail_energy = sum(imports[t] for t in cp.hours if t >= horizon)
            outcomes.append(
                BillingOutcome(
                    period=cp.period,
                    energy_kwh=energy,
                    cost_pln=cost,
                    cap_binds=binds,
                    settled_cost_pln=min(cost, cp.period.limit_kwh * energy),
                    planned_energy_kwh=planned,
                    tail_energy_kwh=tail_energy,
                )
            )
        return outcomes

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
        fracs: list[float] | None = None,
        slots: list | None = None,
        ess_request: ESSRequest | None = None,
        cap_price: list[float] | None = None,
        periods: list[_CapPeriod] | None = None,
        tail_start: int | None = None,
        tail_ev_kwh: float = 0.0,
        tail_ev_cap_kw: float = 0.0,
        apartment: list[float] | None = None,
        switch: _SwitchModel | None = None,
        ppe2_periods: list[_CapPeriod] | None = None,
    ) -> _LPSolution:
        """Solve the MILP over ``len(total_price)`` hours.

        With ``periods`` (billing months under a price cap) the energy part of
        every grid kWh is settled per month as ``min(C_P, limit·E_P)``; without
        them the model is the plain per-hour objective. Hours from
        ``tail_start`` on are the accounting-only month tail: they carry no
        planned EV load, but ``tail_ev_kwh`` of expected EV import is placed
        among them by the solver (≤ ``tail_ev_cap_kw`` per hour).

        With ``switch`` the apartment's ``apartment`` kWh are placed per hour
        on the Victron or on meter 2 (``demand`` is then the Victron's own
        demand without it) and PPE 2's ``ppe2_periods`` are settled like
        PPE 1's; without it ``demand`` already holds the apartment's share.
        """
        cfg = self.config
        n = len(total_price)
        if switch is not None and (apartment is None or not periods or not ppe2_periods):
            raise ValueError("switching needs the apartment and both meters' periods")
        apartment = apartment if switch is not None else [0.0] * n
        ppe2_periods = ppe2_periods if switch is not None else []
        tail_start = n if tail_start is None else tail_start
        # Calendar instructions for the house battery (``#ess`` tags). Both
        # degrade gracefully: see the floor clamp and the objective bonus below.
        ess_targets = ess_request.targets if ess_request is not None else []
        ess_forced_hours = (
            ess_request.forced_hours if ess_request is not None else set()
        )
        if slots is None:  # callers that only price a horizon pass no slots
            ess_targets, ess_forced_hours = [], set()
        # Fraction of each slot that is actually plannable (slot 0 mid-hour is
        # the remainder of the running hour; see ``optimize``). Power limits
        # (kW) convert to energy bounds (kWh) through this factor; ``demand``
        # arrives already scaled.
        fracs = fracs if fracs is not None else [1.0] * n
        ceff = max(battery.charge_efficiency, _EPS)
        deff = max(battery.discharge_efficiency, _EPS)
        wear = battery.wear_cost
        capacity_kwh = battery.capacity_kwh
        e_min = capacity_kwh * battery.min_soc / 100.0
        e_max = capacity_kwh * battery.max_soc / 100.0
        # Plan from the REAL level. A pack below its minimum modelled as sitting
        # on the minimum never gets charged back — it drains to 0 % instead.
        e0 = min(max(battery.energy_kwh, 0.0), e_max)
        # Reserve recovery (below the minimum, or latched until the margin):
        # the level may start under e_min but never falls below where it is,
        # and every kWh short of the recovery target is penalised in every
        # hour, so the solver charges back as fast as the inverter, the charge
        # curve and the EV phase sharing allow — whatever the price — and does
        # not discharge on the way. A penalty rather than a hard "reach it by
        # hour k" bound, for the same reason as the ``#ess`` bonus below: a
        # bound the physics cannot meet makes the model infeasible and takes
        # the whole plan down.
        recovering = battery.recovering or battery.soc < battery.min_soc
        e_recover = min(
            capacity_kwh * (battery.min_soc + RECOVERY_MARGIN_SOC) / 100.0, e_max
        )
        e_floor = min(e_min, e0)
        charge_price = (
            charge_order_price
            if charge_order_price is not None
            else [_charge_order_price(price) for price in total_price]
        )
        capped = bool(periods)
        if capped and (cap_price is None or len(cap_price) != n):
            raise ValueError("cap price required for every hour under a price cap")
        # Energy-side price that moves into the monthly settlement — zero for
        # hours outside every capped month. Without a cap nothing moves and
        # the per-column costs are the full prices.
        settled = cap_price if capped else [0.0] * n

        p_term = (
            cfg.terminal_price
            if cfg.terminal_price is not None
            else (sum(total_price[:tail_start]) / tail_start if tail_start else 0.0)
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

        min_charge = cfg.min_charge_power_kw or 0.0
        d0 = n * seg_n  # first discharge column
        s0 = d0 + n  # first reservoir-level (state) column
        y0 = s0 + n  # first min-charge indicator column
        g0 = y0 + (n if min_charge > _EPS else 0)  # first grid-import column
        tail_n = n - tail_start
        v0 = g0 + n  # first tail-EV column (one per tail hour)
        p0 = v0 + tail_n  # first billing-period block (C, E per period)

        def state_col(t: int) -> int:
            return s0 + t

        def grid_col(t: int) -> int:
            return g0 + t

        def tail_ev_col(t: int) -> int:
            return v0 + (t - tail_start)

        def period_cols(i: int) -> tuple[int, int]:
            base = p0 + 2 * i
            return base, base + 1  # C, E

        # Per-hour grid-side charge cap (kWh over the slot's plannable window).
        # During EV hours the battery may still charge — the real limit is
        # electrical: worst case the EV charger and the inverter share one
        # phase, so the inverter gets whatever the phase fuse leaves after the
        # EV draw. Without fuse data fall back to the legacy blanket
        # subtraction.
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
            charge_caps.append(charge_cap * fracs[t])

        # Charge segment columns c[t,k].
        for t in range(n):
            for k in range(seg_n):
                h.addVar(0.0, min(seg_width[k] * fracs[t], charge_caps[t]))
        # Discharge columns are BINARY: each hour either covers the whole house
        # demand from the battery (z=1 → cap[t] kWh delivered) or not at all
        # (z=0). This makes every hour exactly one inverter mode and forbids
        # rationing a *partial* discharge across hours — a deliberate
        # simplification chosen over a marginally cheaper fractional plan.
        # With switching, ``cap`` is D0 (the apartment on the Victron) and the
        # apartment's share ``D0 − D1`` comes off it while it is on meter 2:
        # delivered = D0·z − (D0 − D1)·w with w = z·s (see the module doc).
        discharge_cap = [
            min(demand[t] + apartment[t], cfg.inverter_max_discharge_kw * fracs[t])
            for t in range(n)
        ]
        apartment_share = [
            discharge_cap[t]
            - min(demand[t], cfg.inverter_max_discharge_kw * fracs[t])
            for t in range(n)
        ]
        # Tail hours are accounting-only: their switches stay continuous, so
        # the month tail adds no branching to the search.
        for t in range(n):
            if discharge_cap[t] > _EPS:
                h.addVar(0.0, 1.0)
                if t < tail_start:
                    h.changeColIntegrality(d0 + t, highspy.HighsVarType.kInteger)
            else:
                h.addVar(0.0, 0.0)  # no demand → nothing to cover this hour

        # Reservoir level E[t] after each hour as an explicit state column,
        # bounded by the SoC band and any ``#ess_soc`` floor (added below once
        # the floors are known). Linked hour-to-hour by a sparse transition
        # row instead of the former prefix sums, so the model stays O(n) in
        # nonzeros however long the horizon grows.
        for t in range(n):
            h.addVar(e_floor, e_max)

        # Minimum charge power indicators y[t] (semi-continuous charge).
        if min_charge > _EPS:
            for t in range(n):
                h.addVar(0.0, 1.0)
                if t < tail_start:
                    h.changeColIntegrality(y0 + t, highspy.HighsVarType.kInteger)

        # Grid import g[t] = demand + EV + charge − discharge (+ tail EV), capped
        # by the physical connection power straight on the column bound.
        conn = cfg.connection_power_kw or 0.0
        for t in range(n):
            h.addVar(0.0, conn * fracs[t] if conn > 0 else inf)

        # Expected EV import in the month's tail, placed by the solver.
        tail_ev_cap = [0.0] * n
        if tail_n > 0 and tail_ev_kwh > _EPS and tail_ev_cap_kw > _EPS:
            for t in range(tail_start, n):
                tail_ev_cap[t] = tail_ev_cap_kw * fracs[t]
        placeable = sum(tail_ev_cap[tail_start:])
        if tail_ev_kwh > placeable + _EPS:
            _LOGGER.warning(
                "Ogon miesiąca: oczekiwane ładowanie EV %.1f kWh nie mieści się "
                "w %.1f kWh dostępnych na ładowarce — przycinam do tej wartości.",
                tail_ev_kwh,
                placeable,
            )
            tail_ev_kwh = placeable
        for t in range(tail_start, n):
            h.addVar(0.0, tail_ev_cap[t])

        # Billing-period blocks: C (pre-cap cost) and E (import). The month's
        # settlement ``min(C, L·E)`` is concave; instead of a regime binary
        # with big-M rows, each solve fixes every period's regime through the
        # objective (cost 1 on C, or L on E) and the cheapest combination of
        # regimes wins — see the enumeration below.
        periods = periods or []
        for _i in range(len(periods)):
            h.addVar(-inf, inf)  # C
            h.addVar(0.0, inf)  # E

        # Recovery shortfall r[t] ≥ e_recover − E[t] (one per hour, recovery only).
        r0 = p0 + 2 * len(periods)
        if recovering:
            for t in range(n):
                h.addVar(0.0, e_recover)

        # Apartment switching: one supply column per segment (s, 1 = meter 2;
        # segment 0 = the running hour, fixed to the actual supply), an open
        # indicator per later segment (o ≥ s_k − s_{k−1}, priced at the cycle
        # threshold), the product w[t] = z[t]·s[t] for hours whose discharge
        # cap the apartment changes, then PPE 2's billing blocks (C, E).
        sw0 = r0 + (n if recovering else 0)
        seg_k = len(switch.starts) if switch is not None else 0

        def s_col(k: int) -> int:
            return sw0 + k

        def o_col(k: int) -> int:  # k ≥ 1
            return sw0 + seg_k + (k - 1)

        w0 = sw0 + seg_k + max(seg_k - 1, 0)
        w_cols: dict[int, int] = {}
        if switch is not None:
            for k in range(seg_k):
                if k == 0:
                    h.addVar(switch.actual, switch.actual)
                    continue
                h.addVar(0.0, 0.0 if k in switch.victron else 1.0)
                h.changeColIntegrality(s_col(k), highspy.HighsVarType.kInteger)
            for _k in range(1, seg_k):
                h.addVar(0.0, 1.0)
            for t in range(n):
                if apartment_share[t] > _EPS and discharge_cap[t] > _EPS:
                    w_cols[t] = w0 + len(w_cols)
                    h.addVar(0.0, 1.0)
        q0 = w0 + len(w_cols)

        def ppe2_cols(i: int) -> tuple[int, int]:
            base = q0 + 2 * i
            return base, base + 1  # C, E

        for _i in range(len(ppe2_periods)):
            h.addVar(-inf, inf)  # C
            h.addVar(0.0, inf)  # E

        n_cols = q0 + 2 * len(ppe2_periods)
        cost = np.zeros(n_cols, dtype=np.float64)
        for t in range(n):
            # Earliness tie-break: among equally priced hours prefer charging
            # in the earliest, so an unfinished charge extends into the cheap
            # tail instead of expensive hours.
            tie = _EARLY_TIE_BREAK * t
            for k in range(seg_n):
                cost[seg_col(t, k)] = (
                    charge_price[t] - settled[t] + (wear - tv) * seg_eff[k] + tie
                )
            # d-column is a 0/1 switch worth discharge_cap[t] kWh delivered;
            # w takes the apartment's share back off while it is on meter 2.
            per_kwh = -(total_price[t] - settled[t]) + wear + tv / deff
            cost[d0 + t] = per_kwh * discharge_cap[t]
            if t in w_cols:
                cost[w_cols[t]] = -per_kwh * apartment_share[t]
        for t in range(tail_start, n):
            cost[tail_ev_col(t)] = total_price[t] - settled[t]
        for k in range(1, seg_k):
            cost[o_col(k)] = switch.threshold
        # The settlement columns' costs are set per regime combination below.
        if recovering:
            # Dwarfs any price, wear or terminal value: no economic gain can
            # justify leaving the reserve short for another hour.
            penalty = max((abs(p) for p in total_price), default=0.0) + abs(tv) + abs(wear) + 1e3
            for t in range(n):
                cost[r0 + t] = penalty
        # ``#ess`` hours: charge here whatever it costs. Expressed as an
        # objective bonus rather than a lower bound on the charge columns —
        # a hard floor collides with the pack's own SoC band the moment the
        # battery is already full, and an infeasible model would take the
        # WHOLE house plan down, not just this hour. As a preference it
        # degrades into "charge as much as fits". The bonus only steers the
        # solver; reported costs come from the realised flows in ``_decide``.
        if ess_forced_hours:
            bonus = max((abs(p) for p in total_price), default=0.0) + abs(wear) + 1e3
            for t in range(n):
                if slots[t].start in ess_forced_hours:
                    for k in range(seg_n):
                        cost[seg_col(t, k)] -= bonus
        h.changeColsCost(n_cols, np.arange(n_cols, dtype=np.int32), cost)

        # Floors demanded by ``#ess_socNN``: the level after the last hour that
        # ENDS by the deadline must reach the target. Clamped into the pack's
        # own band and to what the inverter could physically deliver from
        # e_min, so an over-ambitious tag becomes "as high as reachable"
        # instead of an infeasible model.
        floors: dict[int, float] = {}
        for deadline, percent, _label in ess_targets:
            hours = [
                t
                for t in range(n)
                if slots[t].start + timedelta(hours=1) <= deadline
            ]
            if not hours:
                continue
            t = max(hours)
            reachable = e0 + sum(charge_caps[k] * max(seg_eff) for k in range(t + 1))
            want = min(capacity_kwh * max(0.0, min(100.0, percent)) / 100.0, e_max)
            floors[t] = max(floors.get(t, 0.0), min(want, reachable))

        # Reservoir dynamics:
        #   E[t] − E[t−1] − Σ_s η_s·c[t,s] + (cap[t]/η_dis)·z[t] = 0,  E[−1] = e0
        # with the SoC band (and the calendar floors) as bounds on E[t].
        for t in range(n):
            lo = max(e_floor, floors.get(t, 0.0))
            h.changeColBounds(state_col(t), min(lo, e_max), e_max)
            if recovering:
                # E[t] + r[t] ≥ e_recover
                h.addRow(
                    e_recover,
                    inf,
                    2,
                    np.array([state_col(t), r0 + t], dtype=np.int32),
                    np.ones(2, dtype=np.float64),
                )
            idx = [seg_col(t, s) for s in range(seg_n)] + [d0 + t, state_col(t)]
            val = [-seg_eff[s] for s in range(seg_n)] + [
                discharge_cap[t] / deff,
                1.0,
            ]
            if t in w_cols:
                idx.append(w_cols[t])
                val.append(-apartment_share[t] / deff)
            rhs = e0
            if t > 0:
                idx.append(state_col(t - 1))
                val.append(-1.0)
                rhs = 0.0
            h.addRow(
                rhs,
                rhs,
                len(idx),
                np.array(idx, dtype=np.int32),
                np.array(val, dtype=np.float64),
            )

        # Grid balance (PPE 1): g[t] − Σ_k c[t,k] + cap[t]·z[t] − v[t] = demand + EV,
        # with switching g[t] − Σc + D0·z − (D0 − D1)·w + A·s − v = F + A + EV.
        for t in range(n):
            idx = [grid_col(t)] + [seg_col(t, k) for k in range(seg_n)] + [d0 + t]
            val = [1.0] + [-1.0] * seg_n + [discharge_cap[t]]
            if t >= tail_start:
                idx.append(tail_ev_col(t))
                val.append(-1.0)
            if t in w_cols:
                idx.append(w_cols[t])
                val.append(-apartment_share[t])
            if switch is not None and apartment[t] > 0.0:
                idx.append(s_col(switch.segment[t]))
                val.append(apartment[t])
            rhs = demand[t] + apartment[t] + ev_kwh[t]
            h.addRow(
                rhs,
                rhs,
                len(idx),
                np.array(idx, dtype=np.int32),
                np.array(val, dtype=np.float64),
            )

        # Per-hour total charge cap (the segment bounds alone allow up to the
        # full inverter power; EV hours may leave less).
        for t in range(n):
            if charge_caps[t] < sum(seg_width) * fracs[t] - _EPS:
                h.addRow(
                    -inf,
                    charge_caps[t],
                    seg_n,
                    np.array([seg_col(t, k) for k in range(seg_n)], dtype=np.int32),
                    np.ones(seg_n, dtype=np.float64),
                )

        # SoC-dependent charge curve (only when actually configured). The power
        # bound converts to the slot's energy bound through its plannable
        # fraction: charge_energy[t] ≤ frac[t]·(slope·E[t−1] + intercept).
        for slope, intercept in _charge_curve_cuts(cfg.charge_curve, capacity_kwh):
            if abs(slope) <= _EPS:
                continue
            for t in range(n):
                idx = [seg_col(t, k) for k in range(seg_n)]
                val = [1.0] * seg_n
                rhs = fracs[t] * (intercept + slope * e0)
                if t > 0:
                    idx.append(state_col(t - 1))
                    val.append(-slope * fracs[t])
                    rhs = fracs[t] * intercept
                h.addRow(
                    -inf,
                    rhs,
                    len(idx),
                    np.array(idx, dtype=np.int32),
                    np.array(val, dtype=np.float64),
                )

        # Minimum charge power: make each hour's total charge semi-continuous —
        # either 0 or ≥ min_charge. One binary indicator y[t] per hour with
        #   Σ_k c[t,k] ≤ cap[t]·y[t]   (charge only when the switch is on)
        #   Σ_k c[t,k] ≥ min_charge·y[t]  (and then at least the floor)
        # The floor is a POWER (kW), so the slot's energy floor scales with its
        # plannable fraction.
        if min_charge > _EPS:
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
                # Σc - min_charge·frac·y ≥ 0
                h.addRow(
                    0.0,
                    inf,
                    seg_n + 1,
                    np.array([*idx, y0 + t], dtype=np.int32),
                    np.array(
                        [*([1.0] * seg_n), -min_charge * fracs[t]], dtype=np.float64
                    ),
                )

        # Expected tail EV import: exactly ``tail_ev_kwh`` over the tail hours.
        if tail_n > 0 and tail_ev_kwh > _EPS:
            idx = [tail_ev_col(t) for t in range(tail_start, n)]
            h.addRow(
                tail_ev_kwh,
                tail_ev_kwh,
                len(idx),
                np.array(idx, dtype=np.int32),
                np.ones(len(idx), dtype=np.float64),
            )

        # Monthly settlement per billing period:
        #   C_P = C_mtd + Σ cap_price[t]·g[t]      E_P = E_mtd + Σ g[t]
        # and the period costs C_P (cap does not bind) or L·E_P (cap binds).
        for i, cp in enumerate(periods):
            c_col, e_col = period_cols(i)
            idx = [c_col] + [grid_col(t) for t in cp.hours]
            val = [1.0] + [-settled[t] for t in cp.hours]
            h.addRow(
                cp.period.cost_pln,
                cp.period.cost_pln,
                len(idx),
                np.array(idx, dtype=np.int32),
                np.array(val, dtype=np.float64),
            )
            idx = [e_col] + [grid_col(t) for t in cp.hours]
            val = [1.0] + [-1.0] * len(cp.hours)
            h.addRow(
                cp.period.energy_kwh,
                cp.period.energy_kwh,
                len(idx),
                np.array(idx, dtype=np.int32),
                np.array(val, dtype=np.float64),
            )

        if switch is not None:
            self._add_switch_rows(h, switch, w_cols, d0, s_col, o_col)
        # PPE 2's monthly settlement, its import being A[t]·s[t]:
        #   C2 = C2_mtd + Σ cap_price[t]·A[t]·s[t]     E2 = E2_mtd + Σ A[t]·s[t]
        # settled like PPE 1's months.
        for i, cp in enumerate(ppe2_periods):
            c_col, e_col = ppe2_cols(i)
            cost_coef: dict[int, float] = {}
            energy_coef: dict[int, float] = {}
            for t in cp.hours:
                if apartment[t] <= 0.0:
                    continue
                col = s_col(switch.segment[t])
                cost_coef[col] = cost_coef.get(col, 0.0) - settled[t] * apartment[t]
                energy_coef[col] = energy_coef.get(col, 0.0) - apartment[t]
            for col0, coef, rhs in (
                (c_col, cost_coef, cp.period.cost_pln),
                (e_col, energy_coef, cp.period.energy_kwh),
            ):
                h.addRow(
                    rhs,
                    rhs,
                    1 + len(coef),
                    np.array([col0, *coef.keys()], dtype=np.int32),
                    np.array([1.0, *coef.values()], dtype=np.float64),
                )

        # Cap regimes by enumeration. Σ_P min(C_P, L·E_P) minimised over the
        # plan equals the cheapest, over every combination of regimes, of the
        # plan minimised with each period's regime fixed (min over plans and
        # min over regimes commute); at that optimum each chosen term is the
        # min, so the settlement is exact and ``cap_binds`` is the chosen
        # regime. A fixed regime is a linear cost — C_P, or L·E_P — so no
        # big-M and no regime binary remain. The model is built once; only
        # the settlement columns' costs change between solves. Ties: more binding periods first,
        # then earlier periods binding first; a later combination must beat
        # the best by more than the MIP's gap. Binding first because a
        # "does not bind" regime on a degenerate period (C = L·E, e.g. an
        # empty next month: 0 = 0) prices its kWh below the limit at no real
        # gain, inviting battery shifts into it that save exactly nothing —
        # the binding regime plans the same cost without them.
        settlements = [(cp, period_cols(i)) for i, cp in enumerate(periods)] + [
            (cp, ppe2_cols(i)) for i, cp in enumerate(ppe2_periods)
        ]
        combos = sorted(
            itertools.product((False, True), repeat=len(settlements)),
            key=lambda combo: (-sum(combo), [not binds for binds in combo]),
        )
        tie_rank = {combo: rank for rank, combo in enumerate(combos)}

        def set_regimes(combo: tuple[bool, ...]) -> None:
            cols: list[int] = []
            vals: list[float] = []
            for (cp, (c_col, e_col)), binds in zip(settlements, combo):
                cols += [c_col, e_col]
                vals += [0.0, cp.period.limit_kwh] if binds else [1.0, 0.0]
            if cols:
                h.changeColsCost(
                    len(cols),
                    np.array(cols, dtype=np.int32),
                    np.array(vals, dtype=np.float64),
                )

        # Exact pruning: a combination's LP relaxation bounds its MIP from
        # below, so the MIPs run cheapest bound first and one whose bound
        # exceeds the best plan found is skipped. The others run with the
        # best objective as HiGHS' ``objective_bound``: only the objective
        # differs between solves, so the model is feasible for all of them
        # once the first is optimal, and "infeasible" under a bound means
        # "nothing cheaper than the best" (pruned, never a tie). No solve is
        # seeded with another's plan: a seed would decide ties inside a
        # combination by the order the combinations ran in.
        lower: dict[tuple[bool, ...], float] = {}
        if len(combos) > 1:
            h.setOptionValue("solve_relaxation", True)
            for combo in combos:
                set_regimes(combo)
                h.run()
                if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
                    raise RuntimeError(
                        f"HiGHS could not bound cap regimes {combo}: {h.getModelStatus()}"
                    )
                lower[combo] = h.getInfo().objective_function_value
            h.setOptionValue("solve_relaxation", False)
        order = sorted(combos, key=lambda combo: (lower.get(combo, 0.0), tie_rank[combo]))
        solved: dict[tuple[bool, ...], tuple[float, list[float]]] = {}
        best_obj = math.inf
        self.last_solve_seconds = []
        self.last_regime_combinations = len(combos)
        for combo in order:
            if lower.get(combo, -math.inf) > best_obj + _REGIME_TIE_PLN:
                continue
            set_regimes(combo)
            h.setOptionValue("objective_bound", best_obj + _REGIME_TIE_PLN)
            began = time.perf_counter()
            h.run()
            self.last_solve_seconds.append(time.perf_counter() - began)
            status = h.getModelStatus()
            if status == highspy.HighsModelStatus.kInfeasible and solved:
                continue  # cannot beat the best plan
            if status != highspy.HighsModelStatus.kOptimal:
                raise RuntimeError(
                    f"HiGHS did not find an optimal plan (cap regimes {combo}): {status}"
                )
            objective = h.getInfo().objective_function_value
            solved[combo] = (objective, list(h.getSolution().col_value))
            best_obj = min(best_obj, objective)
        # Among the optima within the MIP's gap of the cheapest, the tie order.
        regime_combo = min(
            (combo for combo, (obj, _) in solved.items() if obj <= best_obj + _REGIME_TIE_PLN),
            key=lambda combo: tie_rank[combo],
        )
        objective, sol = solved[regime_combo]
        # The cycle thresholds are a hurdle, not a cost: plans are compared
        # without them (o is exact under minimisation while the threshold is
        # positive; at zero it costs nothing either way).
        objective -= sum(switch.threshold * sol[o_col(k)] for k in range(1, seg_k))
        regimes = list(regime_combo[: len(periods)])
        regimes2 = list(regime_combo[len(periods) :])

        charge = [
            sum(sol[seg_col(t, k)] for k in range(seg_n)) for t in range(n)
        ]
        # Stored (post-loss) energy per hour, from the same segment solution —
        # _build_plan replays it so the effective η matches the chosen power.
        stored = [
            sum(seg_eff[k] * sol[seg_col(t, k)] for k in range(seg_n))
            for t in range(n)
        ]
        # d-columns are 0/1 switches; expand back to delivered energy (less
        # the apartment's share in hours it spends on meter 2).
        discharge = [
            discharge_cap[t] * sol[d0 + t]
            - (apartment_share[t] * sol[w_cols[t]] if t in w_cols else 0.0)
            for t in range(n)
        ]
        grid = [max(0.0, sol[grid_col(t)]) for t in range(n)]
        tail_ev = [0.0] * n
        for t in range(tail_start, n):
            tail_ev[t] = max(0.0, sol[tail_ev_col(t)])
        settled_periods: list[tuple[float, float, bool]] = []
        for i in range(len(periods)):
            c_col, e_col = period_cols(i)
            settled_periods.append((sol[e_col], sol[c_col], regimes[i]))
        supply = (
            [1.0 if sol[s_col(switch.segment[t])] > 0.5 else 0.0 for t in range(n)]
            if switch is not None
            else []
        )
        ppe2_settled = [
            (sol[ppe2_cols(i)[1]], sol[ppe2_cols(i)[0]], regimes2[i])
            for i in range(len(ppe2_periods))
        ]
        return _LPSolution(
            charge=charge,
            discharge=discharge,
            stored=stored,
            grid=grid,
            tail_ev=tail_ev,
            periods=settled_periods,
            supply=supply,
            ppe2_periods=ppe2_settled,
            objective=objective,
        )

    @staticmethod
    def _add_switch_rows(h, switch: _SwitchModel, w_cols, d0, s_col, o_col) -> None:
        """Open indicators, minimum stay and the product w = z·s."""
        inf = highspy.kHighsInf

        def row(lo: float, hi: float, cols: list[int], vals: list[float]) -> None:
            h.addRow(
                lo,
                hi,
                len(cols),
                np.array(cols, dtype=np.int32),
                np.array(vals, dtype=np.float64),
            )

        starts = switch.starts
        for k in range(1, len(starts)):
            # o_k ≥ s_k − s_{k−1}: opening a cycle (to meter 2) costs the
            # threshold; returning is free.
            row(0.0, inf, [o_col(k), s_col(k), s_col(k - 1)], [1.0, -1.0, 1.0])
            # Minimum stay: a switch at boundary t keeps the new source for
            # every hour τ ∈ [t, t + H) — rows only against the segments that
            # begin inside that window (the rest share s_k anyway):
            #   s_k − s_{k−1} ≤ s_j        s_{k−1} − s_k ≤ 1 − s_j
            for j in range(k + 1, len(starts)):
                if starts[j] >= starts[k] + switch.min_stay:
                    break
                row(-inf, 0.0, [s_col(k), s_col(k - 1), s_col(j)], [1.0, -1.0, -1.0])
                row(-inf, 1.0, [s_col(k - 1), s_col(k), s_col(j)], [1.0, -1.0, 1.0])
        # McCormick for w = z·s (exact: s is binary): w ≤ z, w ≤ s, w ≥ z + s − 1.
        for t, w in w_cols.items():
            s = s_col(switch.segment[t])
            row(-inf, 0.0, [w, d0 + t], [1.0, -1.0])
            row(-inf, 0.0, [w, s], [1.0, -1.0])
            row(-1.0, inf, [w, d0 + t, s], [1.0, -1.0, -1.0])

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
        fracs: list[float] | None = None,
        cap_price: list[float | None] | None = None,
        supply: list[str] | None = None,
        apartment: list[float] | None = None,
    ) -> Plan:
        """Replay the solved flows into decisions.

        ``demand`` is the Victron's demand under each hour's planned
        ``supply`` (``None`` without supply detection); ``apartment`` the
        apartment's own forecast, for the trace.
        """
        cfg = self.config
        ceff = max(battery.charge_efficiency, _EPS)
        deff = max(battery.discharge_efficiency, _EPS)
        wear = battery.wear_cost
        n = len(total_price)
        fracs = fracs if fracs is not None else [1.0] * n
        charge_price = charge_order_price or [
            _charge_order_price(price) for price in total_price
        ]
        # EV SoC forecast: project the car's charge forward from its live SoC as
        # planned charging is delivered and predicted trip driving drains it.
        # Only possible when the SoC sensor and a battery size are known;
        # otherwise the line stays blank.
        ev_battery_kwh = ev_request.battery_kwh if ev_request else 0.0
        ev_drain = ev_request.drain_kwh if ev_request else {}
        # Planned charging can never raise the projected EV SoC past the active
        # charge ceiling — the car's BMS stops there.  A live SoC already above
        # that ceiling must be preserved, however: the limit is not a discharge
        # command and cannot erase energy already present in the pack.
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
        # Reserve recovery target (see ``_solve_lp``); hours starting under it
        # explain themselves by the recovery, not by prices.
        recover_soc = (
            battery.min_soc + RECOVERY_MARGIN_SOC
            if battery.recovering or battery.soc < battery.min_soc
            else None
        )

        decisions: list[Decision] = []
        for t, slot in enumerate(forecast.slots):
            c = charge[t] if charge[t] > _EPS else 0.0
            d = discharge[t] if discharge[t] > _EPS else 0.0
            # ``ev`` (grid kWh) drives cost/grid load; ``ev_added`` (pack kWh,
            # = grid × η) drives the SoC forecast and is what the car's
            # energy-added sensor will report back.
            ev = ev_kwh[t]
            ev_added = (
                ev_alloc.added.get(slot.start, ev) if ev_alloc is not None else ev
            )
            ev_minutes = (
                ev_alloc.minutes.get(slot.start) if ev_alloc is not None else None
            )
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
            decision.ev_charge_minutes = ev_minutes
            if supply is not None:
                decision.supply = supply[t]
            if ev_soc_kwh is not None:
                drain = ev_drain.get(slot.start, 0.0)
                after_drain = max(0.0, ev_soc_kwh - drain)
                charge_headroom = max(0.0, ev_ceiling_kwh - after_drain)
                ev_soc_kwh = min(
                    ev_battery_kwh,
                    after_drain + min(ev_added, charge_headroom),
                )
                decision.ev_soc = round(ev_soc_kwh / ev_battery_kwh * 100.0, 1)
            decision.charge_power = (
                ChargePower.LIMITED if ev > 0 else ChargePower.FULL
            )
            decision.battery_charge_kwh = stored_kwh
            # Grid-side charge power (kW) actually drawn this hour — what you set
            # on the inverter as "force charge X kW". Equals ``stored / η``
            # spread over the slot's plannable window (the remainder of the
            # running hour for slot 0, a full hour otherwise).
            decision.charge_power_kw = (
                stored_kwh / (eff_t or ceff) / max(fracs[t], _EPS)
                if stored_kwh > _EPS
                else 0.0
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
                "ev_minutes": ev_minutes,
                "charge_kwh": round(stored_kwh, 3),
                "charge_efficiency": round(eff_t, 4) if eff_t is not None else None,
                "discharge_kwh": round(delivered, 3),
                "grid_buy_kwh": round(grid_buy, 3),
                "soc_before": round(soc_before, 1),
                "soc_after": round(battery.soc, 1),
                "battery_energy_cost_before": round(cost_before, 4),
                "reason": (
                    self._recovery_reason(
                        mode, soc_before, recover_soc, battery.min_soc, stored_kwh
                    )
                    if recover_soc is not None and soc_before < recover_soc - _EPS
                    else self._reason(
                        mode,
                        tp,
                        charge_threshold,
                        discharge_threshold,
                        stored_kwh,
                        delivered,
                    )
                ),
            }
            if fracs[t] < 1.0 - _EPS:
                # Mid-hour re-plan: this decision covers only the remainder of
                # the running hour — energies are for that window, powers (kW)
                # are already normalised by it.
                decision.trace["hour_fraction"] = round(fracs[t], 4)
            if cap_price is not None and cap_price[t] is not None:
                decision.trace["cap_price"] = round(cap_price[t], 4)
            if supply is not None:
                decision.trace["supply"] = supply[t]
                decision.trace["apartment_kwh"] = round(
                    apartment[t] if apartment is not None else 0.0, 3
                )

            if t == 0 and reminders:
                decision.reminders = list(reminders)

            decisions.append(decision)

        return Plan(forecast=forecast, decisions=decisions, created_at=dt_util.now())

    @staticmethod
    def _recovery_reason(
        mode: str, soc_before: float, recover_soc: float, min_soc: float, stored: float
    ) -> str:
        head = (
            f"odbudowa rezerwy: SoC {soc_before:.1f}% < {recover_soc:.0f}% "
            f"(minimum {min_soc:.0f}% + {RECOVERY_MARGIN_SOC:.0f}%)"
        )
        if mode == InverterMode.CHARGE:
            return f"{head} → ładowanie {stored:.2f} kWh niezależnie od ceny"
        return f"{head}, ale w tej godzinie nie da się ładować → bez rozładowania"

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
                f"z ESS niż z sieci"
            )
        if total_price < charge_threshold:
            return (
                f"cena {total_price:.3f} < próg ładowania {charge_threshold:.3f}, "
                f"ale optymalizator nie ładuje (ESS pełny albo brak droższych "
                f"godzin do pokrycia tą energią) → passthrough"
            )
        if total_price > discharge_threshold:
            return (
                f"cena {total_price:.3f} > próg rozładowania "
                f"{discharge_threshold:.3f}, ale energia z ESS jest "
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
        self,
        forecast: Forecast,
        ev_request: EVRequest | None,
        first_hour_frac: float = 1.0,
    ) -> EVAllocation:
        """Allocate EV charging across the horizon.

        The charger is **on/off at full power** — the allocator sizes hours in
        **pack energy** ("added" kWh): a full charge hour adds ``P·η`` (flat
        charging efficiency), so reaching a deficit of ``D`` kWh means
        ``K = ceil(D / (P·η))`` hours — full hours on the cheapest slots and
        the unavoidable remainder on whichever chosen hour is
        **chronologically last** (that is where charging actually finishes).
        We try every candidate top-off hour, pick the cheapest full blocks
        before it, and keep the globally cheapest combination (costs are
        grid-side: added energy ÷ η). Each planned hour also carries its
        charging **duration in minutes** (remainder ÷ full power), so the
        user's automation can stop the charger instead of relying on a BMS
        mid-hour stop.

        Three layers, never exceeding the request's SoC ceiling
        (``charge_ceiling_soc`` — the car is steered to stop there):

        1. **Forced windows** — bare ``"<keyword>"`` calendar events: charge full
           power in exactly those hours (user's explicit choice, not cost-driven).
        2. **Deadline targets** — ``"<keyword> NN%"`` events, trip floors and the
           routine drain-profile floor: cost-optimal blocks among available
           hours before the deadline (earliest deadline first).
        3. **Default top-up** — no keyword plan and no drain profile learned
           yet: cost-optimal blocks for the deficit to the target SoC.

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

        # Power/efficiency model: full charger power, flat efficiency. Grid
        # energy = added ÷ η; a full hour at full power adds P·η to the pack.
        p_full = max(ev_request.charger_power_kw, 0.1)
        eff_full = min(max(ev_request.charge_efficiency, 0.01), 1.0)

        # Full-hour yield (added kWh) — the per-hour allocation cap. The slot
        # we are already inside can only charge for its remaining minutes, so
        # its cap shrinks by ``first_hour_frac`` (see ``optimize``).
        hour_cap_added = max(p_full * eff_full, 0.1)
        first_start = forecast.slots[0].start if forecast.slots else None

        def hour_cap(start: datetime) -> float:
            # An hour the car drives off in is chargeable only up to departure.
            # The request's fraction is authoritative where it exists — for the
            # running hour it already folds in the minutes elapsed, so it must
            # not be scaled by ``first_hour_frac`` on top.
            fraction = ev_request.hour_fraction.get(start)
            if fraction is None:
                fraction = first_hour_frac if start == first_start else 1.0
            return hour_cap_added * max(fraction, 0.0)
        battery_kwh = max(ev_request.battery_kwh, 0.0)
        # Highest SoC the planner may intentionally buy to. A keyword calendar
        # target steers the car to stop there, so energy planned past the
        # ceiling is undeliverable by construction.
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

        # Chronological spine for the pack-level check: every hour that can
        # move the car's charge — a plannable slot or an hour of predicted
        # driving.
        timeline = sorted(
            {slot.start for slot in forecast.slots} | set(ev_request.drain_kwh)
        )

        def pack_levels(
            extra: dict[datetime, float] | None = None,
        ) -> list[tuple[datetime, float]]:
            """Projected pack level (kWh) at the end of every timeline hour.

            Drive drain lands before charging within an hour, matching the SoC
            line published in ``_decide`` — the car leaves, then plugs back in.
            """
            level = soc0_kwh
            out: list[tuple[datetime, float]] = []
            for hour in timeline:
                level = max(0.0, level - ev_request.drain_kwh.get(hour, 0.0))
                level += allocation.get(hour, 0.0)
                if extra:
                    level += extra.get(hour, 0.0)
                out.append((hour, level))
            return out

        def headroom(
            start: datetime, extra: dict[datetime, float] | None = None
        ) -> float:
            """kWh addable in ``start`` without ever breaching the ceiling.

            Energy put into an hour raises the projected level in every LATER
            hour as well, so the binding limit is the tightest room across the
            whole remaining horizon — not the room in ``start`` alone.

            This is the constraint the allocator used to lack. Room was one
            scalar that credited the *entire* horizon's trip drain as if it had
            already happened, so a week of driving (82 kWh) read as room in a
            pack that had 15 kWh free. A post-trip deficit could then be bought
            in the cheap hours BEFORE the trip, where the pack is still full:
            the energy was priced, loaded into the LP as house demand, and
            never delivered — while the target it was bought for went unmet
            because nothing was placed after the drain.
            """
            if battery_kwh <= 0:
                return float("inf")
            return max(
                0.0,
                min(
                    (
                        ceiling_kwh - level
                        for hour, level in pack_levels(extra)
                        if hour >= start
                    ),
                    default=ceiling_kwh,
                ),
            )

        def pack_caps(candidates: list) -> dict[datetime, float]:
            """Per-hour caps that are *jointly* deliverable, cheapest hours first.

            The ceiling constrains every prefix of the plan, so per-hour room
            does not simply add up: five hours in front of a full pack each
            have "room to the ceiling", but together they can only deliver it
            once. Room is therefore handed out in price order — the cheapest
            hour takes as much as the prefix limits allow, the next takes
            what's left — which keeps the caps jointly feasible without
            spending the room on whichever hour merely comes first.

            Any partial fill of caps built this way is feasible too (less
            energy can only lower the level), so the placement search
            downstream needs no pack awareness of its own.
            """
            caps = {
                slot.start: max(
                    0.0, hour_cap(slot.start) - allocation.get(slot.start, 0.0)
                )
                for slot in candidates
            }
            if battery_kwh <= 0:
                return caps
            granted: dict[datetime, float] = {}
            for slot in sorted(candidates, key=lambda s: (slot_price(s), s.start)):
                granted[slot.start] = min(
                    caps[slot.start], headroom(slot.start, granted)
                )
            return granted

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
            ``prefer_early``) reshape the choice within a bounded extra cost.
            Earliness outranks contiguity, day before hour: placements within
            ``early_max_extra_pct`` % of the cheapest are first narrowed to
            those charging as much energy as possible on the earliest day,
            then on the next (lexicographic per-day energy — "do it now if it
            can be done now"); contiguity then keeps the block unbroken within
            that split unless the scattered optimum is more than
            ``contiguous_max_extra_pct`` % cheaper; finally the
            earliest-finishing placement wins. This order stops a contiguous
            block on a later day from outbidding still-cheap hours today.
            """
            if room is not None:
                deficit = min(deficit, room)
            if deficit <= _EPS:
                return {}
            # What each hour can still absorb: charger headroom AND room under
            # the pack ceiling once the projected level is walked forward. An
            # hour sitting in front of a full pack is dropped however cheap it
            # looks.
            #
            # Hours already carrying a partial allocation stay eligible with
            # their remaining headroom — an earlier small target (a trip floor)
            # must not fence the cheapest hour off from a later, bigger target
            # and scatter its block over pricier hours. Physically the charger
            # simply runs at full power through the union of the chosen hours.
            room_by_hour = pack_caps(candidates)
            slots = sorted(
                (s for s in candidates if room_by_hour[s.start] > _EPS),
                key=lambda s: s.start,
            )
            if not slots:
                return {}
            caps = {s.start: room_by_hour[s.start] for s in slots}
            # Every variant must deliver the same energy or it isn't comparable
            # by cost (an under-filled variant would win on price alone).
            # ``caps`` is jointly feasible, so its sum is genuinely deliverable.
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

            # Calendar days the candidates span, earliest first — the axis the
            # earliness preference is compared on.
            day_order = sorted({dt_util.as_local(s.start).date() for s in slots})

            def _day_energy(
                variant: tuple[float, datetime, list[datetime], dict[datetime, float]],
            ) -> tuple[float, ...]:
                """Energy per calendar day, earliest day first.

                Every variant delivers the same total, so comparing these
                tuples lexicographically — bigger is better — ranks placements
                by how much of the charge happens EARLY: as much as possible
                today, then as much as possible tomorrow, and so on. Rounded so
                that float summation order can't split an otherwise equal
                split.
                """
                per_day: dict[date, float] = {}
                for start, kwh in variant[3].items():
                    day = dt_util.as_local(start).date()
                    per_day[day] = per_day.get(day, 0.0) + kwh
                return tuple(round(per_day.get(day, 0.0), 6) for day in day_order)

            def _variant(
                top_off, fulls: list
            ) -> tuple[float, datetime, list[datetime], dict[datetime, float]]:
                covered = sum(caps[s.start] for s in fulls)
                rem = deliverable - covered  # in (0, cap(top_off)]
                # Grid-side cost: added energy ÷ the flat efficiency.
                cost = (
                    sum(caps[s.start] / eff_full * slot_price(s) for s in fulls)
                    + rem / eff_full * slot_price(top_off)
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
                    s = by_price[i]
                    i += 1
                    if deliverable - covered - caps[s.start] <= _EPS:
                        # Filled whole, this hour would swallow the top-off's
                        # remainder and push it to zero or below. A negative
                        # remainder prices as a discount, wins the cost
                        # comparison, and is then dropped on conversion — the
                        # hour stays full and the car gets planned far more
                        # than the target needs. The placement that tops off
                        # on this hour is its own variant.
                        continue
                    fulls.append(s)
                    covered += caps[s.start]
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

            def _within(cost: float, base: float, pct: float) -> bool:
                # Bounded extra spend relative to ``base``; |·| keeps the
                # bound meaningful when spot prices go negative.
                return cost <= base + abs(base) * pct / 100.0 + _EPS

            # Preference cascade — earliness OUTRANKS contiguity, and the
            # calendar day outranks the hour ("charge Saturday before Sunday").
            # Both earlier orderings let a contiguous block on a LATER day
            # outbid still-cheap hours today; see the note in step 1.
            pool = variants
            if ev_request.prefer_early:
                # 1. Day: among placements within the early budget of the
                #    global optimum, keep those that put as much energy as
                #    possible on the earliest day, then as much as possible on
                #    the next, and so on. Everything after (contiguity, the
                #    hour-level pick) may only rearrange hours within that
                #    per-day split.
                #
                #    Earliness is deliberately measured as *how much energy
                #    lands on each day*, not as "which day does the placement
                #    finish on". The finish-day test collapsed into a no-op the
                #    moment the whole block no longer fitted in what was left
                #    of today: every placement then "finished tomorrow", so the
                #    day filter kept them all and contiguity was free to move
                #    ALL charging to tomorrow while today's equally cheap hours
                #    went unused. Worse, a bigger ``early_max_extra_pct`` made
                #    the plan LATER, because it let that later contiguous block
                #    into the pool in the first place. Charging early is worth a
                #    bounded premium on its own: today's cheap hours are known,
                #    tomorrow's are a forecast that can still evaporate — and an
                #    unused cheap hour never comes back.
                eligible = [
                    v
                    for v in pool
                    if _within(v[0], best_cost, ev_request.early_max_extra_pct)
                ]
                earliest = max(_day_energy(v) for v in eligible)
                pool = [v for v in eligible if _day_energy(v) == earliest]
            if ev_request.prefer_contiguous:
                # 2. Contiguity, within whatever pool earliness left: an
                #    unbroken block may cost up to its budget over the pool's
                #    own optimum. No block within budget → the gap is allowed.
                pool_best = min(v[0] for v in pool)
                contiguous_pool = [
                    v
                    for v in pool
                    if _contiguous(sorted([*v[2], v[1]]))
                    and _within(v[0], pool_best, ev_request.contiguous_max_extra_pct)
                ]
                if contiguous_pool:
                    pool = contiguous_pool
            if ev_request.prefer_early:
                # 3. Hour: among the remaining placements within the early
                #    budget, finish as soon as possible.
                pool_best = min(v[0] for v in pool)
                eligible = [
                    v
                    for v in pool
                    if _within(v[0], pool_best, ev_request.early_max_extra_pct)
                ]
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
            take = min(hour_cap(start), headroom(start))
            if take <= _EPS:
                continue  # a spent current hour (or a full pack) has no window
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
            """Predicted driving fully completed by ``moment``.

            Only whole hours count. Drain is bucketed per clock hour while a
            deadline lands mid-hour, and the straddling bucket is driving that
            happens *after* the deadline — a trip deadline IS the departure, so
            the hour the car leaves in is spent on the road, not before it.
            Counting it charged the same journey twice: a trip target's
            ``target_soc`` already covers the round trip (reserve + trip
            energy), so a car sitting above its target was told to buy another
            packful before setting off.
            """
            return sum(
                kwh
                for start, kwh in ev_request.drain_kwh.items()
                if start + timedelta(hours=1) <= moment
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

        # 3. Default top-up (no keyword plan, no drain profile yet).
        if ev_request.required_kwh > _EPS:
            candidates = [
                slot
                for start, slot in slots_by_start.items()
                if start in ev_request.available_hours
            ]
            commit(select(ev_request.required_kwh, candidates))

        # Convert the added-energy schedule into the dual-domain allocation:
        # grid energy (cost / LP load) and the charging duration within the
        # hour at full power (rounded UP so the car never gets less than
        # planned; the BMS/SoC limit still caps any overshoot).
        out = EVAllocation()
        for start, added in allocation.items():
            if added <= _EPS:
                continue
            out.added[start] = added
            out.grid[start] = added / eff_full
            out.minutes[start] = min(
                60, max(1, math.ceil(added / hour_cap_added * 60.0))
            )

        # The ceiling is physical: the charger stops there, so energy planned
        # above it is never delivered. It IS still paid for and still enters
        # the LP as house load, though, which is why this must be loud. The SoC
        # projection in ``_decide`` clamps to the ceiling and would otherwise
        # absorb the mistake silently — a flat SoC line under full charge bars.
        if battery_kwh > 0 and ev_request.charge_ceiling_soc is not None:
            for hour, level in pack_levels():
                if level > ceiling_kwh + 1e-3:
                    _LOGGER.warning(
                        "EV: plan przekracza pojemność paku o %.2f kWh o %s "
                        "(%.1f%% > sufit %.1f%%) — ta energia nie trafiłaby do "
                        "auta, a byłaby wyceniona i obciążyłaby plan domu.",
                        level - ceiling_kwh,
                        moment_label(hour),
                        level / battery_kwh * 100.0,
                        ev_request.charge_ceiling_soc,
                    )
                    break
        return out
