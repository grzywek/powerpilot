"""Optimizer snapshot store.

Persists one *columnar* snapshot of the optimizer's inputs + plan per clock hour
(a "vintage"), so past plans can be compared against each other and against the
realized actuals on the "Symulacje" tab.

This is a plain storage helper (not a :class:`PowerPilotModule`): the coordinator
assembles each record from ``forecast.slots`` + ``plan.decisions`` and drives the
``Store``. Records are keyed by the run's UTC clock hour, deduplicated to one per
hour, and pruned after :data:`_SNAPSHOT_RETENTION_DAYS`.

Record shape (arrays are parallel, one entry per horizon hour)::

    {
      "run_at": iso, "start": iso, "n": int, "horizon_hours": int,
      "total_cost": float,
      "buy": [..], "dist": [..], "ptype": ["c"|"f"|"e"|None],
      "cons_fc": [..], "base_fc": [..], "mode": ["c"|"d"|"p"],
      "soc": [..], "grid": [..], "cost": [..],
      "rev": [{"at": iso, "why": str|None, "ev": .., "chg": .., "grid": ..,
               "mode": "c"|"d"|"p"}],   # mid-hour re-plans, oldest first
      "rev_more": int,                  # revisions dropped at the cap
      "revcap": True,                   # this hour was watched for re-plans
    }
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

# How long vintages are kept (user-chosen: 30 days of history for bias analysis).
_SNAPSHOT_RETENTION_DAYS = 30

# Mid-hour re-plans kept per vintage. The running hour may legitimately be
# re-planned several times (the car gets plugged in, a calendar event lands, HA
# restarts); the cap stops a chatty hour from growing the store without bound.
# Overflow is counted in ``rev_more``, never dropped silently.
_MAX_REVISIONS_PER_HOUR = 12


class SnapshotStore:
    """In-memory collection of optimizer vintages, keyed by UTC clock hour."""

    def __init__(self) -> None:
        # {utc_iso_hour: record}
        self._records: dict[str, dict[str, Any]] = {}

    def __len__(self) -> int:
        return len(self._records)

    @staticmethod
    def _key(run_at: datetime) -> str:
        return (
            dt_util.as_utc(run_at)
            .replace(minute=0, second=0, microsecond=0)
            .isoformat()
        )

    def add(self, record: dict[str, Any]) -> str:
        """Store a record, deduplicated to one per clock hour. Returns its key."""
        run_at = dt_util.parse_datetime(record["run_at"])
        key = self._key(run_at) if run_at else record["run_at"]
        self._records[key] = record
        return key

    def add_revision(self, run_hour: datetime, revision: dict[str, Any]) -> bool:
        """Append a mid-hour re-plan to the vintage covering ``run_hour``.

        The vintage itself (index 0 = the plan the hour started with) stays
        untouched, so forecast-accuracy scoring keeps measuring against the plan
        that was actually in force when the hour began. The revisions record what
        changed *after* that — the difference between "the plan was deliberately
        revised" and "reality simply diverged from the plan", which is invisible
        from the vintage alone.

        Returns ``False`` when no vintage backs the hour (nothing to attach to)
        or the per-hour cap is reached — the overflow is counted in ``rev_more``
        so the UI can say how many changes it isn't showing.
        """
        rec = self._records.get(self._key(run_hour))
        if rec is None:
            return False
        revisions = rec.setdefault("rev", [])
        if len(revisions) >= _MAX_REVISIONS_PER_HOUR:
            rec["rev_more"] = int(rec.get("rev_more") or 0) + 1
            return False
        revisions.append(revision)
        return True

    def revisions_at(self, hour: datetime) -> list[dict[str, Any]]:
        """Mid-hour re-plans recorded for ``hour``, oldest first (may be empty)."""
        rec = self._records.get(self._key(hour))
        return list(rec.get("rev") or []) if rec else []

    def revisions_dropped_at(self, hour: datetime) -> int:
        """How many re-plans for ``hour`` were dropped at the per-hour cap."""
        rec = self._records.get(self._key(hour))
        return int(rec.get("rev_more") or 0) if rec else 0

    def records_revisions_at(self, hour: datetime) -> bool:
        """Was ``hour`` recorded by a build that tracks mid-hour re-plans?

        Vintages written before revision tracking existed have no revisions —
        not because the hour was never re-planned, but because nobody was
        looking. "No revision recorded" may only be read as "the plan never
        changed" for hours this returns ``True`` for.
        """
        rec = self._records.get(self._key(hour))
        return bool(rec.get("revcap")) if rec else False

    def runs(self) -> list[dict[str, Any]]:
        """Lightweight list of available vintages, newest first (for the picker)."""
        out = [
            {
                "run_at": key,
                "start": rec.get("start"),
                "horizon_hours": rec.get("horizon_hours"),
                "total_cost": rec.get("total_cost"),
            }
            for key, rec in self._records.items()
        ]
        out.sort(key=lambda r: r["run_at"], reverse=True)
        return out

    def get(self, run_at: str) -> dict[str, Any] | None:
        """Exact record by its key, with a tolerant floor-to-hour fallback."""
        rec = self._records.get(run_at)
        if rec is not None:
            return rec
        parsed = dt_util.parse_datetime(run_at)
        return self._records.get(self._key(parsed)) if parsed else None

    def nearest_run_at(self, max_at: datetime) -> str | None:
        """Latest vintage whose run time is at or before ``max_at`` (lead-time pick)."""
        cutoff = self._key(max_at)
        candidates = [key for key in self._records if key <= cutoff]
        return max(candidates) if candidates else None

    def value_at(self, hour: datetime, key: str) -> Any | None:
        """Realized value of a per-hour array (e.g. ``"bcost"``) for a clock hour.

        Used to reconstruct realized past quantities the live recorder cannot
        give (the modelled battery energy cost has no sensor). Prefers the
        vintage recorded *at* that hour — its index 0 is the realized "now"
        state entering the hour — and otherwise falls back to the latest earlier
        vintage, indexing forward by the hour offset.
        """
        run_key = self.nearest_run_at(hour)
        if run_key is None:
            return None
        rec = self._records.get(run_key)
        if not rec:
            return None
        start = dt_util.parse_datetime(rec.get("start") or "")
        if start is None:
            return None
        idx = round((hour - start).total_seconds() / 3600.0)
        seq = rec.get(key) or []
        if 0 <= idx < len(seq):
            return seq[idx]
        return None

    def lead_value_at(self, hour: datetime, key: str, lead_hours: int) -> Any | None:
        """Value predicted for ``hour`` by the plan current ``lead_hours`` before it.

        Picks the latest vintage recorded at or before ``hour - lead_hours`` and
        reads the entry for ``hour`` from its parallel ``key`` array. This is how
        the chart's "prognoza" line can show, for each past hour, how the forecast
        looked N hours out — the forecast corrects itself as the hour approaches,
        so a stale-lead view surfaces divergences a fresh-lead view hides.

        ``lead_hours == 0`` is the freshest forecast (the plan made as the hour
        began). Returns ``None`` when no such vintage exists or the hour falls
        outside its horizon.
        """
        run_key = self.nearest_run_at(hour - timedelta(hours=max(lead_hours, 0)))
        if run_key is None:
            return None
        rec = self._records.get(run_key)
        if not rec:
            return None
        start = dt_util.parse_datetime(rec.get("start") or "")
        if start is None:
            return None
        idx = round((hour - start).total_seconds() / 3600.0)
        seq = rec.get(key) or []
        if 0 <= idx < len(seq):
            return seq[idx]
        return None

    def origin_at(self, hour: datetime, lead_hours: int = 0) -> str | None:
        """Run timestamp (vintage key) of the plan that fed ``hour``'s forecast.

        Mirrors the lead-aware vintage selection used to fill the forecast side
        (:meth:`run0_at` for lead 0, :meth:`nearest_run_at` for lead N), so the
        UI can label each hour's forecast with *when* the plan behind it was
        made. Returns ``None`` when no vintage backs the hour.
        """
        if lead_hours > 0:
            return self.nearest_run_at(hour - timedelta(hours=lead_hours))
        key = self._key(hour)
        return key if key in self._records else None

    def run0_at(self, run_hour: datetime, key: str) -> Any | None:
        """Index-0 value of the vintage *recorded at* ``run_hour`` (the plan made
        that hour, whose first slot is that hour).

        Unlike :meth:`value_at`, this never forward-indexes an earlier plan: if
        no vintage was recorded at ``run_hour`` it returns ``None``. Use it to
        reconstruct a single coherent plan's first-hour state, so a hour's
        forecast ``soc``/``charge``/``grid`` all come from the same trajectory
        (mixing vintages produces nonsense like "charging yet SoC falls").
        """
        rec = self._records.get(self._key(run_hour))
        if not rec:
            return None
        seq = rec.get(key) or []
        return seq[0] if seq else None

    def trips_overlapping(
        self,
        start: datetime,
        end: datetime,
        started_at_or_before: datetime | None = None,
    ) -> list[dict]:
        """Trips recorded in any vintage whose away window overlaps [start, end).

        Deduplicated by away-window overlap: a newer vintage's view of any
        overlapping span replaces older-vintage trips there (one car cannot be
        on two overlapping trips), so editing an event's hour or title does not
        leave the pre-edit copy behind. Trips *within* one vintage never
        suppress each other — nested events (a leg inside a multi-day trip)
        legitimately overlap. Past events removed from the calendar still stay
        visible: no newer overlapping trip arrives to displace them.

        ``started_at_or_before`` lets the live chart keep only trip history whose
        away window has already begun. Future trips must come from the current
        calendar read, otherwise an edited calendar event leaves its old future
        time behind as a ghost from older snapshots.
        """
        out: list[tuple[datetime, datetime, dict]] = []
        for key in sorted(self._records):  # oldest → newest
            # Everything this vintage knew inside the window — including trips
            # the ``started_at_or_before`` filter keeps off the chart — must
            # suppress older overlapping copies, or an event edited to a later,
            # not-yet-started hour keeps its old copy visible until it starts.
            known: list[tuple[datetime, datetime]] = []
            shown: list[tuple[datetime, datetime, dict]] = []
            for trip in self._records[key].get("trips") or []:
                depart = dt_util.parse_datetime(trip.get("depart") or "")
                ret = dt_util.parse_datetime(trip.get("return_end") or "")
                if depart is None or ret is None or ret <= start or depart >= end:
                    continue
                known.append((depart, ret))
                if started_at_or_before is not None and depart > started_at_or_before:
                    continue
                shown.append((depart, ret, trip))
            if not known:
                continue
            out = [
                entry
                for entry in out
                if not any(
                    entry[0] < k_ret and entry[1] > k_dep for k_dep, k_ret in known
                )
            ]
            out.extend(shown)
        out.sort(key=lambda entry: entry[0])
        return [trip for _, _, trip in out]

    def prune(self) -> None:
        cutoff = dt_util.utcnow() - timedelta(days=_SNAPSHOT_RETENTION_DAYS)
        kept: dict[str, dict[str, Any]] = {}
        for key, value in self._records.items():
            try:
                stamp = datetime.fromisoformat(key)
            except (ValueError, TypeError):
                continue
            if stamp >= cutoff:
                kept[key] = value
        self._records = kept

    def to_dict(self) -> dict[str, Any]:
        return {"records": self._records}

    @classmethod
    def from_dict(cls, data: dict | None) -> "SnapshotStore":
        store = cls()
        if data:
            store._records = dict(data.get("records") or {})
        return store
