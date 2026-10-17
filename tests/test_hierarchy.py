"""Unit tests for the consumption-meter hierarchy maths."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from custom_components.powerpilot.const import (
    CONF_CLIMATE_SENSORS,
    CONF_DEVICE_SENSORS,
    SUPPLY_HOUR_MIXED,
    SUPPLY_HOUR_UNKNOWN,
    SUPPLY_METER_2,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.hierarchy import (
    PARENT_ROOT,
    build_children,
    exclusive_series,
    metered_sensors,
    normalize_parents,
    subtree,
)

ROOT = "sensor.victron"
APARTMENT = "sensor.apartment"
WASHER = "sensor.washer"
GARAGE = "sensor.garage_device"
AC = "sensor.salon_klimka_energy"
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
    excl = exclusive_series(ROOT, _flat_devices(), parents, series).series

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
    excl = exclusive_series(ROOT, devices, None, series).series
    assert excl[ROOT][H] == 2.5  # 4.0 − 1.0 − 0.5
    assert excl[WASHER][H] == 1.0
    assert excl[GARAGE][H] == 0.5


def test_metered_sensors_folds_in_climate_meters() -> None:
    """A weather-dependent meter counts as a sub-meter, listed or not."""
    assert metered_sensors({}) == []
    assert metered_sensors({CONF_CLIMATE_SENSORS: [AC]}) == [AC]

    # Configured devices keep their order; a climate-only meter is appended.
    cfg = {CONF_DEVICE_SENSORS: [APARTMENT, WASHER], CONF_CLIMATE_SENSORS: [AC]}
    assert metered_sensors(cfg) == [APARTMENT, WASHER, AC]

    # Listing the same meter in both places must not duplicate it — a duplicate
    # would subtract its energy twice in exclusive_series.
    both = {CONF_DEVICE_SENSORS: [APARTMENT, AC], CONF_CLIMATE_SENSORS: [AC]}
    assert metered_sensors(both) == [APARTMENT, AC]


def test_climate_meter_is_excluded_from_the_base_load() -> None:
    """The AC's energy leaves the root's exclusive series, so the climate
    module's temperature forecast replaces it instead of stacking on it."""
    series = {
        ROOT: {H: 3.0},
        AC: {H: 1.2},
    }
    devices = metered_sensors({CONF_CLIMATE_SENSORS: [AC]})
    out = exclusive_series(ROOT, devices, None, series).series
    assert out[ROOT][H] == 1.8
    assert out[AC][H] == 1.2


# ---------------------------------------------------------------------------
# Supply-aware tree (stage 2, slice 3)
# ---------------------------------------------------------------------------

BOILER = "sensor.boiler"
NESTED = [APARTMENT, WASHER, GARAGE, BOILER]
NESTED_PARENTS = {WASHER: APARTMENT, BOILER: WASHER}
H2 = H + timedelta(hours=1)
H3 = H + timedelta(hours=2)


def _nested_series() -> dict:
    """Root 5.0 = apartment 3.0 (⊃ washer 1.0 ⊃ boiler 0.25) + garage 0.5 + 1.5 own."""
    return {
        ROOT: {H: 5.0, H2: 5.0, H3: 5.0},
        APARTMENT: {H: 3.0, H2: 3.0, H3: 3.0},
        WASHER: {H: 1.0, H2: 1.0, H3: 1.0},
        BOILER: {H: 0.25, H2: 0.25, H3: 0.25},
        GARAGE: {H: 0.5, H2: 0.5, H3: 0.5},
    }


def test_meter_2_hour_detaches_the_apartment_subtree_from_the_root() -> None:
    """On PPE 2 the Victron does not carry the apartment: root − its other children."""
    series = _nested_series()
    series[ROOT][H2] = 2.0  # the Victron without the apartment: 1.5 own + 0.5 garage
    supply = {H: SUPPLY_VICTRON, H2: SUPPLY_METER_2, H3: SUPPLY_VICTRON}

    out = exclusive_series(
        ROOT, NESTED, NESTED_PARENTS, series, apartment=APARTMENT, supply=supply
    )

    assert out.series[ROOT] == {H: 1.5, H2: 1.5, H3: 1.5}
    # The apartment subtree is computed as usual in every hour, whatever its source.
    assert out.series[APARTMENT] == {H: 2.0, H2: 2.0, H3: 2.0}
    assert out.series[WASHER] == {H: 0.75, H2: 0.75, H3: 0.75}
    assert out.series[BOILER] == {H: 0.25, H2: 0.25, H3: 0.25}
    assert out.series[GARAGE] == {H: 0.5, H2: 0.5, H3: 0.5}
    assert out.supply_dropped == 0


def test_mixed_and_unknown_hours_drop_only_the_root() -> None:
    supply = {H: SUPPLY_HOUR_MIXED, H2: SUPPLY_HOUR_UNKNOWN, H3: SUPPLY_VICTRON}
    out = exclusive_series(
        ROOT, NESTED, NESTED_PARENTS, _nested_series(), apartment=APARTMENT, supply=supply
    )
    assert out.series[ROOT] == {H3: 1.5}
    assert out.supply_dropped == 2
    for node in NESTED:
        assert set(out.series[node]) == {H, H2, H3}


def test_an_hour_missing_from_the_supply_map_is_unknown() -> None:
    out = exclusive_series(
        ROOT, NESTED, NESTED_PARENTS, _nested_series(), apartment=APARTMENT,
        supply={H: SUPPLY_VICTRON},
    )
    assert out.series[ROOT] == {H: 1.5}
    assert out.supply_dropped == 2


def test_a_missing_child_reading_drops_the_parents_hour_and_is_counted() -> None:
    """Regression: a child with no reading counted as 0 and inflated its parent."""
    series = _nested_series()
    del series[WASHER][H2]  # the washer meter has no statistics for that hour
    del series[GARAGE][H3]

    out = exclusive_series(ROOT, NESTED, NESTED_PARENTS, series)

    assert out.series[APARTMENT] == {H: 2.0, H3: 2.0}  # its child is missing at H2
    assert out.series[WASHER] == {H: 0.75, H3: 0.75}  # no reading of its own at H2
    assert out.series[ROOT] == {H: 1.5, H2: 1.5}  # garage missing at H3
    assert out.missing_child == {APARTMENT: 1, ROOT: 1}
    assert out.negative == {}


def test_a_negative_exclusive_value_is_kept_and_counted() -> None:
    """Regression: a child above its parent was clamped to 0 and learned as a real 0.

    Live 2026-10: the unmetered base is ~20 W, so 126 of 504 hours were negative
    meter noise; dropping them biased the base upward. They are kept as is.
    """
    series = {ROOT: {H: 1.0, H2: 4.0}, WASHER: {H: 2.0, H2: 1.0}}
    out = exclusive_series(ROOT, [WASHER], None, series)
    assert out.series[ROOT] == {H: -1.0, H2: 3.0}
    assert out.series[WASHER] == {H: 2.0, H2: 1.0}
    assert out.negative == {ROOT: 1}
    assert out.missing_child == {}


def test_without_supply_information_every_hour_is_on_the_victron() -> None:
    out = exclusive_series(ROOT, NESTED, NESTED_PARENTS, _nested_series())
    assert out.series[ROOT] == {H: 1.5, H2: 1.5, H3: 1.5}
    assert out.series[APARTMENT] == {H: 2.0, H2: 2.0, H3: 2.0}
    assert out.supply_dropped == 0


def test_a_meter_2_hour_with_the_apartment_reading_missing_still_gives_the_root() -> None:
    """Detached, the apartment is not the root's child: its gap does not matter there."""
    series = _nested_series()
    series[ROOT][H] = 2.0
    del series[APARTMENT][H]
    out = exclusive_series(
        ROOT, NESTED, NESTED_PARENTS, series, apartment=APARTMENT,
        supply={H: SUPPLY_METER_2, H2: SUPPLY_VICTRON, H3: SUPPLY_VICTRON},
    )
    assert out.series[ROOT][H] == 1.5
    assert H not in out.series[APARTMENT]


def test_the_apartment_must_be_a_metered_device_under_the_root() -> None:
    with pytest.raises(ValueError):
        exclusive_series(
            ROOT, NESTED, NESTED_PARENTS, _nested_series(), apartment="sensor.other",
            supply={},
        )
    with pytest.raises(ValueError):
        exclusive_series(
            ROOT, NESTED, NESTED_PARENTS, _nested_series(), apartment=WASHER, supply={}
        )


def test_subtree_is_the_node_and_all_its_descendants() -> None:
    assert subtree(ROOT, NESTED, NESTED_PARENTS, APARTMENT) == {APARTMENT, WASHER, BOILER}
    assert subtree(ROOT, NESTED, NESTED_PARENTS, GARAGE) == {GARAGE}
    assert subtree(ROOT, NESTED, NESTED_PARENTS, "sensor.other") == set()
