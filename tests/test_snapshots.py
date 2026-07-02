"""Unit tests for the optimizer snapshot store (the "Symulacje" tab data model)."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.modules.snapshots import SnapshotStore


def _rec(run_at, start=None, total_cost=1.0):
    return {
        "run_at": run_at.isoformat(),
        "start": (start or run_at).isoformat(),
        "n": 3,
        "horizon_hours": 3,
        "total_cost": total_cost,
        "buy": [0.5, 0.6, 0.7],
        "dist": [0.3, 0.3, 0.3],
        "ptype": ["c", "f", "e"],
        "cons_fc": [1.0, 1.1, 1.2],
        "base_fc": [0.8, 0.9, 1.0],
        "mode": ["c", "p", "d"],
        "soc": [50.0, 55.0, 60.0],
        "grid": [0.0, 0.1, 0.2],
        "cost": [0.0, 0.05, 0.1],
    }


def test_add_dedups_by_clock_hour() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    store.add(_rec(base + timedelta(minutes=2), total_cost=1.0))
    store.add(_rec(base + timedelta(minutes=47), total_cost=2.0))  # same hour
    assert len(store) == 1
    # The later write within the hour wins.
    runs = store.runs()
    assert runs[0]["total_cost"] == 2.0


def test_runs_newest_first() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    store.add(_rec(base - timedelta(hours=2)))
    store.add(_rec(base))
    store.add(_rec(base - timedelta(hours=1)))
    runs = store.runs()
    assert [r["run_at"] for r in runs] == sorted(
        (r["run_at"] for r in runs), reverse=True
    )
    assert len(runs) == 3


def test_get_exact_and_floor_fallback() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    key = store.add(_rec(base + timedelta(minutes=20)))
    assert store.get(key) is not None
    # A non-floored timestamp in the same hour still resolves.
    assert store.get((base + timedelta(minutes=55)).isoformat()) is not None
    assert store.get(base.isoformat()) is not None


def test_nearest_run_at() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    store.add(_rec(base - timedelta(hours=4)))
    store.add(_rec(base - timedelta(hours=1)))
    # Latest run at or before the cutoff.
    near = store.nearest_run_at(base - timedelta(minutes=30))
    assert near == store._key(base - timedelta(hours=1))
    # Nothing early enough.
    assert store.nearest_run_at(base - timedelta(hours=10)) is None


def test_prune_drops_old_vintages() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    store.add(_rec(base - timedelta(days=45)))
    store.add(_rec(base))
    store.prune()
    assert len(store) == 1
    assert store.runs()[0]["run_at"] == store._key(base)


def test_lead_value_at_picks_older_vintage() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    # Two vintages: one 3 h ago, one just now. Each vintage's `soc` array is
    # [idx0, idx1, idx2] for its own start, +1h, +2h.
    old = _rec(base - timedelta(hours=3))
    old["soc"] = [10.0, 11.0, 12.0]  # covers base-3h, base-2h, base-1h
    fresh = _rec(base)
    fresh["soc"] = [90.0, 91.0, 92.0]  # covers base, base+1h, base+2h
    store.add(old)
    store.add(fresh)

    # For hour `base-1h`, lead 0 → freshest plan at/ before base-1h is `old`,
    # its idx 2 (base-3h + 2h == base-1h) → 12.0.
    assert store.lead_value_at(base - timedelta(hours=1), "soc", 0) == 12.0
    # Lead 3 for hour `base` → plan current at base-3h == `old`, idx 3 is out of
    # range (only 3 entries) → None.
    assert store.lead_value_at(base, "soc", 3) is None
    # Lead 0 for hour `base` → `fresh` idx 0 → 90.0.
    assert store.lead_value_at(base, "soc", 0) == 90.0
    # Lead 2 for hour `base+1h` → plan current at base-1h is still `old` (fresh
    # was recorded at base, which is after base-1h) → idx (base+1h - (base-3h)) =
    # 4, out of range → None. Confirms it never forward-runs past the horizon.
    assert store.lead_value_at(base + timedelta(hours=1), "soc", 2) is None


def test_origin_at_reports_vintage_run_time() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    old = _rec(base - timedelta(hours=3))
    fresh = _rec(base)
    store.add(old)
    store.add(fresh)

    # lead 0 needs a vintage recorded exactly at the hour.
    assert store.origin_at(base, 0) == store._key(base)
    # No vintage at base-1h → lead 0 origin is unknown.
    assert store.origin_at(base - timedelta(hours=1), 0) is None
    # lead 3 for `base` picks the plan current at base-3h == `old`.
    assert store.origin_at(base, 3) == store._key(base - timedelta(hours=3))


def test_serialisation_roundtrip() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    store.add(_rec(base))
    restored = SnapshotStore.from_dict(store.to_dict())
    assert len(restored) == 1
    assert restored.runs() == store.runs()
