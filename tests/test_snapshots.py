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


def test_serialisation_roundtrip() -> None:
    store = SnapshotStore()
    base = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    store.add(_rec(base))
    restored = SnapshotStore.from_dict(store.to_dict())
    assert len(restored) == 1
    assert restored.runs() == store.runs()
