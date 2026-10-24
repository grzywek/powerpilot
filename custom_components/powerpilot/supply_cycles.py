"""Apartment switch recommendations: cycles, their value, what to do now.

Stage 3, slice 3 (``docs/plans/apartment-supply-stage3-2026-10-17.md``). The
optimizer plans the apartment's supply hour by hour; this module turns that
plan into what a person acts on:

* **Cycles.** A cycle is a run of planned ``meter_2`` hours. Its id is its
  planned start — or, for the one already running, the actual transition's
  ``at`` from the supply log (its return needs no threshold: the model prices
  only openings).
* **Marginal value.** Each proposed cycle is re-solved without it — its
  planned meter-2 hours held on the Victron (``SupplyPlanning.victron_hours``)
  — and saves the comparable cost without it minus the cost with it, cycle
  thresholds excluded. Holding the cycle's hours (not only its opening
  boundary) keeps a re-solve from answering with the same cycle an hour later,
  which would value a long cycle at its first hour. A cycle saving less than
  the threshold is dropped: its re-solve becomes the plan and every remaining
  cycle is valued again, until all clear it (the weakest is dropped first, so
  no cycle goes only because a weaker one was still there). The running
  cycle's saving is staying until its planned return against returning at the
  next allowed boundary.
* **Now.** The running hour is planned on the actual supply, so the
  recommendation is the planned supply of the next hour; a switch is required
  while it differs from the actual state.

Every re-solve goes through the same ``solve`` (``Optimizer.optimize`` with
the refresh's inputs); their count and wall time ride on the plan.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.util import dt as dt_util

from .const import SUPPLY_METER_2, SUPPLY_VICTRON
from .models import Plan, SupplyCycle, SupplyPlanning
from .when import moment_label

# Two optima of one model differ by up to the MIP's absolute gap
# (``mip_abs_gap`` 1e-7 per solve); a cycle whose saving equals the threshold
# within that is not sub-threshold.
_SAVING_TOLERANCE_PLN = 1e-6

# How many notified (cycle id, direction) pairs are kept: a few cycles a day,
# so weeks of history, bounded.
NOTIFIED_LIMIT = 200

REASON_NOT_ACTIVE = "wykrywanie zasilania mieszkania nie jest aktywne"
REASON_NO_PLAN = "brak planu"
REASON_SHORT_HORIZON = "horyzont planu nie obejmuje następnej godziny"


def _cycle_id(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat()


def plan_cycles(plan: Plan, last_transition_at: datetime | None) -> list[SupplyCycle]:
    """The plan's runs of ``meter_2`` hours, in order.

    A run that includes the running hour (slot 0, planned on the actual
    supply) is the running cycle; it starts at ``last_transition_at``, which
    must then be known.
    """
    decisions = plan.decisions
    n = len(decisions)
    cycles: list[SupplyCycle] = []
    i = 0
    while i < n:
        if decisions[i].supply != SUPPLY_METER_2:
            i += 1
            continue
        j = i
        while j < n and decisions[j].supply == SUPPLY_METER_2:
            j += 1
        running = i == 0
        if running:
            if last_transition_at is None:
                raise ValueError("a running cycle needs the actual transition to meter 2")
            start = last_transition_at
        else:
            start = decisions[i].start
        planned_return = decisions[j].start if j < n else None
        end = planned_return or decisions[-1].start + timedelta(hours=1)
        cycles.append(
            SupplyCycle(
                id=_cycle_id(start),
                start=start,
                planned_return=planned_return,
                hours=(end - start).total_seconds() / 3600.0,
                apartment_kwh=sum(decisions[t].trace["apartment_kwh"] for t in range(i, j)),
                running=running,
                victron_hours=frozenset(decisions[t].start for t in range(i, j)),
            )
        )
        i = j
    return cycles


def settle_cycles(solve: Callable[[SupplyPlanning], Plan], planning: SupplyPlanning) -> Plan:
    """Plan with every proposed cycle worth its threshold on its own.

    ``solve`` plans the refresh's inputs under the given supply planning.
    Without switching modelled this is that one plan, without cycles.
    """
    plan = solve(planning)
    if not plan.supply_switching:
        return plan
    threshold = planning.cycle_threshold_pln
    held = set(planning.victron_hours)
    resolves = 0
    seconds = 0.0
    dropped: list[str] = []

    def resolve(hours: set[datetime]) -> Plan:
        nonlocal resolves, seconds
        began = time.perf_counter()
        alternative = solve(replace(planning, victron_hours=frozenset(hours)))
        seconds += time.perf_counter() - began
        resolves += 1
        return alternative

    while True:
        cycles = plan_cycles(plan, planning.last_transition_at)
        without: dict[str, Plan] = {}
        for cycle in cycles:
            if cycle.running:
                continue
            alternative = resolve(held | cycle.victron_hours)
            cycle.saving_pln = alternative.comparable_cost_pln - plan.comparable_cost_pln
            without[cycle.id] = alternative
        weak = [
            c
            for c in cycles
            if not c.running and c.saving_pln + _SAVING_TOLERANCE_PLN < threshold
        ]
        if not weak:
            break
        drop = min(weak, key=lambda c: (c.saving_pln, c.start))
        held |= drop.victron_hours
        dropped.append(drop.id)
        plan = without[drop.id]

    running = next((c for c in cycles if c.running), None)
    if running is not None:
        boundary = plan.supply_boundaries[0] if plan.supply_boundaries else None
        if boundary is None:
            running.saving_note = "brak dozwolonej chwili powrotu w horyzoncie przełączeń"
        elif running.planned_return == boundary:
            running.saving_pln = 0.0  # the plan already returns at the next boundary
        else:
            stay = {h for h in running.victron_hours if h >= boundary}
            alternative = resolve(held | stay)
            running.saving_pln = alternative.comparable_cost_pln - plan.comparable_cost_pln

    plan.supply_cycles = cycles
    plan.supply_resolves = resolves
    plan.supply_resolve_seconds = seconds
    plan.supply_dropped = dropped
    return plan


def switch_availability(planning: SupplyPlanning, now: datetime) -> tuple[bool, str | None]:
    """Whether the user can switch now, and why not."""
    if planning.calendar_error is not None:
        return False, f"kalendarz nieczytelny — {planning.calendar_error}"
    if planning.in_quiet_window(dt_util.as_local(now).hour):
        return False, (
            f"okno ciszy {planning.quiet_start_hour:02d}:00–"
            f"{planning.quiet_end_hour:02d}:00"
        )
    if not planning.user_available(now):
        return False, "wyjazd w kalendarzu"
    return True, None


@dataclass
class SupplyRecommendation:
    """What the recommendation entities show (one read of plan + actual state).

    ``reason`` says why switching is not modelled (``None`` while it is);
    every field derived from the plan is then ``None`` / empty. ``notice`` is
    the (cycle id, direction) of the switch required now, with its texts.
    """

    reason: str | None
    actual: str | None
    available: bool | None = None
    unavailable_reason: str | None = None
    recommended: str | None = None
    switch_required: bool | None = None
    cycles: list[SupplyCycle] = field(default_factory=list)
    hourly: list[dict[str, Any]] = field(default_factory=list)
    next_switch: datetime | None = None
    next_target: str | None = None
    next_cycle: SupplyCycle | None = None
    cycle: SupplyCycle | None = None  # the running cycle, else the next one
    notice: tuple[str, str] | None = None
    notice_title: str | None = None
    notice_message: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """The recommendation for the panel (the hourly supply rides on the series)."""
        next_cycle = self.next_cycle
        return {
            "reason": self.reason,
            "actual": self.actual,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "recommended": self.recommended,
            "switch_required": self.switch_required,
            "next_switch": self.next_switch.isoformat() if self.next_switch else None,
            "next_target": self.next_target,
            "next_cycle_id": next_cycle.id if next_cycle else None,
            "next_return": (
                next_cycle.planned_return.isoformat()
                if next_cycle and next_cycle.planned_return
                else None
            ),
            "cycle": self.cycle.as_dict() if self.cycle else None,
            "cycles": [c.as_dict() for c in self.cycles],
            # The switch notification's text while a switch is required.
            "notice_message": self.notice_message,
        }


def _pln(value: float) -> str:
    return f"{value:.2f}".replace(".", ",")


def recommendation(
    plan: Plan | None,
    planning: SupplyPlanning | None,
    actual: str | None,
    last_transition_at: datetime | None,
    now: datetime,
) -> SupplyRecommendation:
    """The recommendation now, from the latest plan and the live supply state."""
    if planning is None:
        return SupplyRecommendation(reason=REASON_NOT_ACTIVE, actual=actual)
    available, unavailable_reason = switch_availability(planning, now)
    base = SupplyRecommendation(
        reason=None,
        actual=actual,
        available=available,
        unavailable_reason=unavailable_reason,
    )
    if plan is None:
        return replace(base, reason=REASON_NO_PLAN)
    if not plan.supply_switching:
        return replace(base, reason=plan.supply_reason)
    decisions = plan.decisions
    if len(decisions) < 2:
        return replace(base, reason=REASON_SHORT_HORIZON)

    cycles = plan.supply_cycles
    rec = replace(
        base,
        recommended=decisions[1].supply,
        cycles=cycles,
        hourly=[{"start": d.start.isoformat(), "supply": d.supply} for d in decisions],
        cycle=next((c for c in cycles if c.running), None)
        or next((c for c in cycles if not c.running), None),
    )
    for t in range(1, len(decisions)):
        if decisions[t].supply != decisions[t - 1].supply:
            rec.next_switch = decisions[t].start
            rec.next_target = decisions[t].supply
            hour = decisions[t].start if rec.next_target == SUPPLY_METER_2 else decisions[t - 1].start
            rec.next_cycle = next(c for c in cycles if hour in c.victron_hours)
            break
    if actual is None:
        return rec
    rec.switch_required = rec.recommended != actual
    if not rec.switch_required:
        return rec

    at = decisions[1].start
    if rec.recommended == SUPPLY_METER_2:
        cycle = next(c for c in cycles if at in c.victron_hours)
        rec.notice = (cycle.id, SUPPLY_METER_2)
        back = (
            f"powrót ok. {moment_label(cycle.planned_return)}"
            if cycle.planned_return is not None
            else "powrót po horyzoncie planu"
        )
        saving = (
            f", oszczędność ~{_pln(cycle.saving_pln)} zł"
            if cycle.saving_pln is not None
            else ""
        )
        rec.notice_title = "🏠 PowerPilot: przełącz mieszkanie na licznik 2"
        rec.notice_message = (
            f"Przełącz mieszkanie na licznik 2 do {moment_label(at)}, {back}{saving}."
        )
    else:
        if last_transition_at is None:
            raise ValueError("a return to the Victron needs the actual transition to meter 2")
        rec.notice = (_cycle_id(last_transition_at), SUPPLY_VICTRON)
        rec.notice_title = "🏠 PowerPilot: przełącz mieszkanie na Victron"
        rec.notice_message = (
            f"Przełącz mieszkanie z powrotem na Victron (PPE 1) do {moment_label(at)} — "
            f"koniec cyklu na liczniku 2 od {moment_label(last_transition_at)}."
        )
    return rec


class SwitchNotices:
    """The switch notification: shown per (cycle id, direction), never repeated.

    ``notified`` (oldest first, bounded) survives a restart through the
    coordinator's store; ``shown`` is the pair whose notification is up now.
    """

    def __init__(self, notified: Iterable[tuple[str, str]] = ()) -> None:
        self.notified: list[tuple[str, str]] = list(notified)[-NOTIFIED_LIMIT:]
        self.shown: tuple[str, str] | None = None

    def update(self, notice: tuple[str, str] | None) -> tuple[bool, bool]:
        """``(dismiss, create)`` for the switch now required (``None``: none)."""
        if notice == self.shown:
            return False, False
        dismiss = self.shown is not None
        self.shown = None
        if notice is None or notice in self.notified:
            return dismiss, False
        self.notified = [*self.notified, notice][-NOTIFIED_LIMIT:]
        self.shown = notice
        return dismiss, True

    def to_list(self) -> list[dict[str, str]]:
        return [{"id": cycle_id, "direction": direction} for cycle_id, direction in self.notified]

    @classmethod
    def from_list(cls, data: Any) -> "SwitchNotices":
        """Decode the stored pairs; ``ValueError`` on anything else."""
        if data is None:
            return cls()
        if not isinstance(data, list):
            raise ValueError("notified apartment switches must be a list")
        pairs: list[tuple[str, str]] = []
        for item in data:
            if not isinstance(item, dict):
                raise ValueError(f"notified apartment switch is not a mapping: {item!r}")
            cycle_id, direction = item.get("id"), item.get("direction")
            if not isinstance(cycle_id, str) or direction not in (SUPPLY_METER_2, SUPPLY_VICTRON):
                raise ValueError(f"unusable notified apartment switch: {item!r}")
            pairs.append((cycle_id, direction))
        return cls(pairs)
