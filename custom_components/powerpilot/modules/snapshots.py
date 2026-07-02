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
    }
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.util import dt as dt_util

# How long vintages are kept (user-chosen: 30 days of history for bias analysis).
_SNAPSHOT_RETENTION_DAYS = 30


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
