"""Missed plan actions: what the plan asked for, what happened, what it cost.

PowerPilot has no actuator, and the plan is recomputed all the time, so an
action it asked for and nobody took vanishes with the next plan. This module
keeps the record (ADR 002, ``docs/adr/002-missed-plan-actions.md``):

* **The ask** — the plan's row for the next clock hour as the last plan before
  that hour began saw it (:class:`Ask`, :class:`PlanAsks`).
* **A miss** — reality contradicting the running hour's ask on something a
  person or an automation controls: the car left unplugged, the apartment on
  the other meter, the inverter not following the planned grid mode, or the car
  charged in an hour no plan asked for. One record per hour and kind, an ESS
  episode per alarm.
* **The ledger** — every miss ever found (:class:`MissLedger`): added, settled
  once when its hour is over, never removed.

The rules that decide a miss and settle its cost are plain functions here; the
coordinator gathers their inputs (live states, the recorder, the supply log).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.util import dt as dt_util

from .models import Plan

KIND_EV_UNPLUGGED = "ev_unplugged"
KIND_SUPPLY = "supply"
KIND_ESS = "ess"
KIND_EV_OFF_PLAN = "ev_off_plan"
KINDS = (KIND_EV_UNPLUGGED, KIND_SUPPLY, KIND_ESS, KIND_EV_OFF_PLAN)

# EV energy below this is no charging (the threshold mid-hour revisions use).
EV_EPS_KWH = 0.05

# The recorder's statistics of an hour are complete this long after it ends.
SETTLE_LATENCY = timedelta(minutes=15)
# An hour whose statistics still cannot be read this long after it ends is
# settled without a cost rather than left open for ever.
SETTLE_GIVE_UP = timedelta(hours=24)

NOTE_ESS = "koszt nieznany: model nie wie, co falownik robił zamiast planu"
NOTE_OFF_PLAN = (
    "koszt nieznany: plan nie przewidywał tego ładowania, więc nie ma godziny, "
    "z którą porównać cenę"
)
NOTE_NOT_COSTED = "koszt nie został policzony przed restartem HA"
NOTE_NO_EV_SENSOR = "brak czujnika energii ładowania EV — nie wiadomo, ile auto pobrało"
NOTE_NO_EV_STATS = "brak statystyk ładowania EV z tej godziny"

# How the log names the apartment's supplies.
SUPPLY_NAMES = {"victron": "Victron (PPE 1)", "meter_2": "licznik 2 (PPE 2)"}

_HOUR = timedelta(hours=1)


def _iso(moment: datetime | None) -> str | None:
    return moment.isoformat() if moment is not None else None


def _moment(value: Any, name: str) -> datetime:
    parsed = dt_util.parse_datetime(value) if isinstance(value, str) else None
    if parsed is None or parsed.tzinfo is None:
        raise ValueError(f"{name}: not an aware ISO datetime: {value!r}")
    return parsed


def _hour_key(hour: datetime) -> str:
    return hour.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


def miss_id(kind: str, hour: datetime) -> str:
    """The record id: one per kind and clock hour (an ESS episode: its start)."""
    if kind == KIND_ESS:
        return f"{kind}:{hour.astimezone(timezone.utc).isoformat()}"
    return f"{kind}:{_hour_key(hour)}"


# ---------------------------------------------------------------------------
# The ask
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Ask:
    """What the plan asked of one clock hour, made at ``at``.

    ``supply`` is ``None`` while the plan was not free to move the apartment
    (it then simply keeps the actual supply, which asks nothing).
    """

    hour: datetime
    at: datetime
    ev_kwh: float
    ev_minutes: int | None
    supply: str | None
    mode: str
    charge_power_kw: float
    price_kwh: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "hour": self.hour.isoformat(),
            "at": self.at.isoformat(),
            "ev_kwh": self.ev_kwh,
            "ev_minutes": self.ev_minutes,
            "supply": self.supply,
            "mode": self.mode,
            "charge_power_kw": self.charge_power_kw,
            "price_kwh": self.price_kwh,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Ask":
        """Decode a stored ask; ``ValueError`` when it is unusable."""
        if not isinstance(data, Mapping):
            raise ValueError("ask must be a mapping")
        try:
            minutes = data.get("ev_minutes")
            price = data.get("price_kwh")
            return cls(
                hour=_moment(data.get("hour"), "ask.hour"),
                at=_moment(data.get("at"), "ask.at"),
                ev_kwh=float(data["ev_kwh"]),
                ev_minutes=int(minutes) if minutes is not None else None,
                supply=data.get("supply"),
                mode=str(data["mode"]),
                charge_power_kw=float(data["charge_power_kw"]),
                price_kwh=float(price) if price is not None else None,
            )
        except (KeyError, TypeError) as err:
            raise ValueError(f"ask: {err}") from err


def _utc(moment: datetime) -> datetime:
    """Hours are compared in UTC: in the hour the clocks go back, two local
    02:00s compare equal within their zone and unequal to any other zone."""
    return moment.astimezone(timezone.utc)


def ask_from_plan(plan: Plan, hour: datetime, at: datetime) -> Ask | None:
    """``plan``'s row for ``hour`` as an ask; ``None`` past its horizon."""
    target = _utc(hour)
    for index, decision in enumerate(plan.decisions):
        if _utc(decision.start) != target:
            continue
        slot = plan.forecast.slots[index] if index < len(plan.forecast.slots) else None
        return Ask(
            hour=target,
            at=at,
            ev_kwh=round(decision.ev_charge_kwh, 3),
            ev_minutes=decision.ev_charge_minutes,
            supply=decision.supply if plan.supply_switching else None,
            mode=str(decision.inverter_mode),
            charge_power_kw=round(decision.charge_power_kw, 3),
            price_kwh=(
                round(slot.total_price_kwh, 4)
                if slot is not None and slot.total_price_kwh is not None
                else None
            ),
        )
    return None


class PlanAsks:
    """The running hour's ask, frozen as it began, and the next hour's.

    After every plan run the coordinator offers the plan's next-hour row; when
    that hour begins the last offer becomes its ask. An offer for any other
    hour (Home Assistant was down across the boundary) is no ask: the hour then
    has none rather than one borrowed from an older plan.
    """

    def __init__(self, current: Ask | None = None, upcoming: Ask | None = None) -> None:
        self.current = current
        self.upcoming = upcoming

    def begin(self, hour: datetime) -> Ask | None:
        """The ask for the running clock ``hour``."""
        hour = _utc(hour)
        if self.current is not None and _utc(self.current.hour) == hour:
            return self.current
        upcoming = self.upcoming
        self.current = (
            upcoming if upcoming is not None and _utc(upcoming.hour) == hour else None
        )
        return self.current

    def offer(self, ask: Ask | None) -> bool:
        """Keep the latest plan's ask for the next hour; whether it changed."""
        changed = ask != self.upcoming
        self.upcoming = ask
        return changed

    def to_dict(self) -> dict[str, Any]:
        return {
            "current": self.current.to_dict() if self.current else None,
            "upcoming": self.upcoming.to_dict() if self.upcoming else None,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "PlanAsks":
        """Restore the persisted asks; ``ValueError`` when one is unusable."""
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ValueError("asks must be a mapping")
        current, upcoming = (
            Ask.from_dict(data[key]) if data.get(key) is not None else None
            for key in ("current", "upcoming")
        )
        return cls(current, upcoming)


# ---------------------------------------------------------------------------
# Finding a miss
# ---------------------------------------------------------------------------


def ev_left_unplugged(
    ask: Ask, *, plugged: bool | None, home: bool | None, calendar_available: bool
) -> bool:
    """The plan asked the car to charge and the cable could have gone in, but is out.

    Only when every reading says so: the plug sensor reads unplugged (not
    unknown), the car is at home (not unknown) and the calendar has it
    available — a car away on a trip nobody planned is a forecast miss, not
    one a person could have acted on.
    """
    return (
        ask.ev_kwh > EV_EPS_KWH
        and plugged is False
        and home is True
        and calendar_available
    )


def supply_not_followed(ask: Ask, actual: str | None) -> bool:
    """The apartment is on the other meter than the plan asked for."""
    return ask.supply is not None and actual is not None and actual != ask.supply


def followed_transition_at(transitions: list[Any], asked: str, hour: datetime) -> datetime:
    """When the apartment would last have switched, had the ask been followed.

    Not switched when asked: as if switched as the hour began. Switched away
    within the hour against the ask: as if it had stayed, since the switch
    before (``SupplyTransition``s, oldest first). The minimum stay counts
    from this moment in the counterfactual.
    """
    if len(transitions) >= 2 and transitions[-1].at >= hour and transitions[-2].state == asked:
        return transitions[-2].at
    return hour


def ev_off_plan(
    *,
    realized_kwh: float | None,
    planned_kwh: float | None,
    planned_before_kwh: float,
    watched: bool,
    away: bool,
    spill_kwh: float,
) -> bool:
    """Did the car charge in an hour no plan of it ever asked for?

    ``planned_kwh`` is the most any plan of the hour asked for (its vintage and
    every mid-hour revision), ``None`` when no vintage backs the hour — there
    was no plan to contradict. ``watched``: the hour was recorded by a build
    that tracks revisions; before that, silence means nobody was looking.
    Trip hours (``away``) charged elsewhere. A planned hour before lets the
    charger overrun into this one by ``spill_kwh`` — a session finishing.
    """
    if realized_kwh is None or planned_kwh is None or not watched or away:
        return False
    if planned_kwh > EV_EPS_KWH:
        return False
    floor = EV_EPS_KWH
    if planned_before_kwh > EV_EPS_KWH:
        floor = max(floor, spill_kwh)
    return realized_kwh > floor


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Counterfactual:
    """The refresh solved again with the ask honoured for the rest of the hour.

    ``delta_pln`` = the plan's spend in reality minus with the ask followed
    (``Plan.objective_pln``); ``ev_kwh`` the EV energy the followed plan
    charges in the rest of the hour. ``error`` when it could not be solved.
    """

    delta_pln: float | None
    ev_kwh: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "delta_pln": round(self.delta_pln, 4) if self.delta_pln is not None else None,
            "ev_kwh": round(self.ev_kwh, 3) if self.ev_kwh is not None else None,
            "error": self.error,
        }


def counterfactual(reality: Plan, followed: Plan, hour: datetime) -> Counterfactual:
    """Compare two solves of one refresh in PLN."""
    if reality.objective_pln is None or followed.objective_pln is None:
        return Counterfactual(None, error="plan bez rozwiązania optymalizatora")
    decision = followed.decision_at(hour)
    return Counterfactual(
        delta_pln=reality.objective_pln - followed.objective_pln,
        ev_kwh=decision.ev_charge_kwh if decision is not None else None,
    )


def _asked(ask: Ask) -> dict[str, Any]:
    return {
        "at": ask.at.isoformat(),
        "ev_kwh": ask.ev_kwh,
        "ev_minutes": ask.ev_minutes,
        "supply": ask.supply,
        "mode": ask.mode,
        "charge_power_kw": ask.charge_power_kw,
        "price_kwh": ask.price_kwh,
    }


def _record(
    kind: str,
    hour: datetime,
    found_at: datetime,
    asked: dict[str, Any],
    actual: dict[str, Any],
    *,
    record_id: str | None = None,
) -> dict[str, Any]:
    return {
        "id": record_id or miss_id(kind, hour),
        "kind": kind,
        "hour": _hour_key(hour),
        "found_at": found_at.isoformat(),
        "asked": asked,
        "actual": actual,
        "cf": None,
        "cost_pln": None,
        "note": None,
        "settled_at": None,
    }


def ev_unplugged_record(ask: Ask, found_at: datetime) -> dict[str, Any]:
    return _record(
        KIND_EV_UNPLUGGED, ask.hour, found_at, _asked(ask), {"plugged": False, "home": True}
    )


def supply_record(ask: Ask, found_at: datetime, actual: str) -> dict[str, Any]:
    return _record(KIND_SUPPLY, ask.hour, found_at, _asked(ask), {"supply": actual})


def ess_record(
    start: datetime, mode: str | None, charge_power_kw: float | None, reason: str | None
) -> dict[str, Any]:
    hour = start.replace(minute=0, second=0, microsecond=0)
    record = _record(
        KIND_ESS,
        hour,
        start,
        {"mode": mode, "charge_power_kw": charge_power_kw},
        {"reason": reason, "last_seen": start.isoformat(), "until": None},
        record_id=miss_id(KIND_ESS, start),
    )
    record["note"] = NOTE_ESS
    return record


def ev_off_plan_record(
    hour: datetime, planned_kwh: float, realized_kwh: float, settled_at: datetime
) -> dict[str, Any]:
    record = _record(
        KIND_EV_OFF_PLAN,
        hour,
        settled_at,
        {"ev_kwh": round(planned_kwh, 3)},
        {"ev_kwh": round(realized_kwh, 3)},
    )
    record["note"] = NOTE_OFF_PLAN
    record["settled_at"] = settled_at.isoformat()
    return record


# ---------------------------------------------------------------------------
# Settling a miss when its hour is over
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Settlement:
    cost_pln: float | None
    note: str | None
    actual: dict[str, Any]


def _cf(record: Mapping[str, Any]) -> tuple[float | None, float | None, str | None]:
    cf = record.get("cf")
    if not isinstance(cf, Mapping):
        return None, None, NOTE_NOT_COSTED
    if cf.get("delta_pln") is None:
        error = cf.get("error")
        return None, None, (
            f"nie policzono wariantu z wykonanym planem: {error}" if error else NOTE_NOT_COSTED
        )
    ev = cf.get("ev_kwh")
    return float(cf["delta_pln"]), (float(ev) if ev is not None else None), None


def settle_ev_unplugged(
    record: Mapping[str, Any],
    delivered_after_kwh: float | None,
    delivered_hour_kwh: float | None,
) -> Settlement:
    """Cost of a car left unplugged, scaled by the energy that stayed missed.

    ``delivered_after_kwh``: what the car took from the moment the miss was
    found to the end of the hour (the cable went in after all); the share of
    the counterfactual's energy still missing scales its cost. A
    counterfactual that charges nothing in the rest of the hour keeps its full
    cost. ``None`` readings settle without a cost, with a note saying why.
    """
    actual = {**record.get("actual", {}), "ev_kwh": delivered_hour_kwh}
    delta, cf_kwh, note = _cf(record)
    if delta is None:
        return Settlement(None, note, actual)
    if delivered_after_kwh is None:
        return Settlement(None, NOTE_NO_EV_STATS, actual)
    share = 1.0
    if cf_kwh is not None and cf_kwh > EV_EPS_KWH:
        share = min(1.0, max(0.0, (cf_kwh - delivered_after_kwh) / cf_kwh))
    return Settlement(round(delta * share, 2), None, actual)


def switch_moment(
    transitions: Iterable[Any], state: str, after: datetime, before: datetime
) -> datetime | None:
    """The first logged switch to ``state`` in ``(after, before)`` (``SupplyTransition``s)."""
    for transition in transitions:
        if transition.state == state and after < transition.at < before:
            return transition.at
    return None


def settle_supply(record: Mapping[str, Any], switched_at: datetime | None) -> Settlement:
    """Cost of the apartment on the wrong meter, scaled by the minutes it stayed there.

    ``switched_at`` is the supply log's moment of the switch to the asked meter
    within the hour (its first agreeing sample), ``None`` when it stayed put.
    """
    hour = _moment(record["hour"], "hour")
    found = _moment(record["found_at"], "found_at")
    end = hour + _HOUR
    actual = {
        **record.get("actual", {}),
        "switched_at": switched_at.isoformat() if switched_at is not None else None,
    }
    delta, _, note = _cf(record)
    if delta is None:
        return Settlement(None, note, actual)
    left = (end - found).total_seconds()
    if left <= 0:
        return Settlement(round(delta, 2), None, actual)
    wrong = ((switched_at or end) - found).total_seconds()
    share = min(1.0, max(0.0, wrong / left))
    return Settlement(round(delta * share, 2), None, actual)


# ---------------------------------------------------------------------------
# The ledger
# ---------------------------------------------------------------------------


_REQUIRED = ("id", "kind", "hour", "found_at", "asked", "actual")


class MissLedger:
    """Every missed plan action found, in the order found.

    Records are added once (an id already present is never overwritten),
    settled once, and never removed. ``off_plan_through`` is the last clock
    hour checked for EV charging no plan asked for.
    """

    def __init__(
        self,
        records: Iterable[dict[str, Any]] = (),
        off_plan_through: datetime | None = None,
    ) -> None:
        self._records: dict[str, dict[str, Any]] = {}
        for record in records:
            self._records.setdefault(record["id"], record)
        self.off_plan_through = off_plan_through

    def __len__(self) -> int:
        return len(self._records)

    def has(self, record_id: str) -> bool:
        return record_id in self._records

    def get(self, record_id: str) -> dict[str, Any] | None:
        return self._records.get(record_id)

    def add(self, record: dict[str, Any]) -> bool:
        """Add a new record; ``False`` (and nothing changes) when its id exists."""
        if record["id"] in self._records:
            return False
        self._records[record["id"]] = record
        return True

    def open_records(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [
            r
            for r in self._records.values()
            if r.get("settled_at") is None and (kind is None or r["kind"] == kind)
        ]

    def set_counterfactual(self, record_id: str, cf: Counterfactual) -> bool:
        """Attach the cost basis to an open record that has none yet."""
        record = self._records.get(record_id)
        if record is None or record.get("settled_at") is not None or record.get("cf"):
            return False
        record["cf"] = cf.to_dict()
        return True

    def touch(self, record_id: str, **actual: Any) -> bool:
        """Update what an open record saw so far (an ESS episode's last confirmation)."""
        record = self._records.get(record_id)
        if record is None or record.get("settled_at") is not None:
            return False
        record["actual"] = {**record.get("actual", {}), **actual}
        return True

    def settle(self, record_id: str, settlement: Settlement, at: datetime) -> bool:
        """Freeze an open record with its cost; a settled one is never rewritten."""
        record = self._records.get(record_id)
        if record is None or record.get("settled_at") is not None:
            return False
        record["actual"] = settlement.actual
        record["cost_pln"] = settlement.cost_pln
        record["note"] = settlement.note if settlement.note is not None else record.get("note")
        record["settled_at"] = at.isoformat()
        return True

    def records(self) -> list[dict[str, Any]]:
        """All records, oldest found first."""
        return list(self._records.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": list(self._records.values()),
            "off_plan_through": _iso(self.off_plan_through),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "MissLedger":
        """Decode the stored ledger; ``ValueError`` when any part is unusable.

        Nothing is dropped quietly: a record that cannot be read makes the whole
        file unreadable, so the caller moves it aside instead of saving a
        ledger with holes over it.
        """
        if data is None:
            return cls()
        if not isinstance(data, Mapping):
            raise ValueError("misses ledger must be a mapping")
        raw = data.get("records")
        if not isinstance(raw, list):
            raise ValueError("misses ledger: records must be a list")
        records: list[dict[str, Any]] = []
        for index, record in enumerate(raw):
            if not isinstance(record, dict) or any(key not in record for key in _REQUIRED):
                raise ValueError(f"misses ledger: record {index} is incomplete")
            if record["kind"] not in KINDS:
                raise ValueError(f"misses ledger: record {index} has kind {record['kind']!r}")
            _moment(record["hour"], f"record {index} hour")
            _moment(record["found_at"], f"record {index} found_at")
            records.append(record)
        through = data.get("off_plan_through")
        return cls(
            records,
            _moment(through, "off_plan_through") if through is not None else None,
        )
