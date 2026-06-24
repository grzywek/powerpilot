"""Unit tests for the consumption-meter hierarchy maths."""

from __future__ import annotations

from datetime import datetime

from custom_components.powerpilot.hierarchy import (
    PARENT_ROOT,
    build_children,
    exclusive_series,
    normalize_parents,
)

ROOT = "sensor.victron"
APARTMENT = "sensor.apartment"
WASHER = "sensor.washer"
GARAGE = "sensor.garage_device"
H = datetime(2026, 6, 15, 8)


def _flat_devices() -> list[str]:
    return [APARTMENT, WASHER, GARAGE]


def test_normalize_defaults_to_root() -> None:
    """Devices with no configured parent hang under the root."""
    norm = normalize_parents(ROOT, _flat_devices(), None)
    assert norm == {APARTMENT: ROOT, WASHER: ROOT, GARAGE: ROOT}


def test_normalize_invalid_parent_falls_back_to_root() -> None:
    parents = {WASHER: "sensor.nonexistent", APARTMENT: APARTMENT}  # unknown + self
    norm = normalize_parents(ROOT, _flat_devices(), parents)
    assert norm[WASHER] == ROOT
    assert norm[APARTMENT] == ROOT


def test_normalize_breaks_cycles() -> None:
    """A cycle is broken so every node still reaches the root by walking parents."""
    parents = {APARTMENT: WASHER, WASHER: APARTMENT}  # 2-cycle
    norm = normalize_parents(ROOT, _flat_devices(), parents)
    for node in _flat_devices():
        cur, hops = node, 0
        while norm[cur] != ROOT:
            cur = norm[cur]
            hops += 1
            assert hops <= len(_flat_devices()), f"{node} never reaches root"


def test_build_children_nested() -> None:
    parents = {APARTMENT: PARENT_ROOT, WASHER: APARTMENT, GARAGE: PARENT_ROOT}
    children = build_children(ROOT, _flat_devices(), parents)
    assert set(children[ROOT]) == {APARTMENT, GARAGE}
    assert children[APARTMENT] == [WASHER]
    assert children[WASHER] == []


def test_exclusive_series_nested_no_double_count() -> None:
    """The user's real topology: washer ⊂ apartment ⊂ victron, garage ⊂ victron."""
    parents = {APARTMENT: PARENT_ROOT, WASHER: APARTMENT, GARAGE: PARENT_ROOT}
    series = {
        ROOT: {H: 5.0},        # whole-house behind the inverter
        APARTMENT: {H: 3.0},   # includes the washer
        WASHER: {H: 1.0},
        GARAGE: {H: 0.5},
    }
    excl = exclusive_series(ROOT, _flat_devices(), parents, series)

    # exclusive(root)      = 5.0 − 3.0 − 0.5 = 1.5
    # exclusive(apartment) = 3.0 − 1.0       = 2.0
    # exclusive(washer)    = 1.0
    # exclusive(garage)    = 0.5
    assert excl[ROOT][H] == 1.5
    assert excl[APARTMENT][H] == 2.0
    assert excl[WASHER][H] == 1.0
    assert excl[GARAGE][H] == 0.5

    # Telescoping: the parts sum back to the main reading — counted once.
    total = excl[ROOT][H] + excl[APARTMENT][H] + excl[WASHER][H] + excl[GARAGE][H]
    assert total == series[ROOT][H]


def test_exclusive_flat_matches_legacy_subtraction() -> None:
    """With every device under the root, base = main − Σ devices (old behaviour)."""
    devices = [WASHER, GARAGE]
    series = {ROOT: {H: 4.0}, WASHER: {H: 1.0}, GARAGE: {H: 0.5}}
    excl = exclusive_series(ROOT, devices, None, series)
    assert excl[ROOT][H] == 2.5  # 4.0 − 1.0 − 0.5
    assert excl[WASHER][H] == 1.0
    assert excl[GARAGE][H] == 0.5


def test_exclusive_clamps_negative_to_zero() -> None:
    """A child reading exceeding the parent (meter noise) never goes negative."""
    series = {ROOT: {H: 1.0}, WASHER: {H: 2.0}}
    excl = exclusive_series(ROOT, [WASHER], None, series)
    assert excl[ROOT][H] == 0.0
