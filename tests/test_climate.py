"""Unit tests for the temperature-dependent climate profile."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_CLIMATE_PRESENCE_SENSORS,
    CONF_CLIMATE_SENSORS,
)
from custom_components.powerpilot.modules.climate import (
    MIN_LEARN_DAYS,
    TEMP_BIN_C,
    ClimateModule,
    TemperatureProfile,
)

AC = "sensor.ac_energy"
HEAT_PUMP = "sensor.heat_pump_energy"


def test_predict_same_bin_and_hour() -> None:
    profile = TemperatureProfile()
    for _ in range(3):
        profile.observe(30.0, 14, 1.5)
    assert round(profile.predict(30.5, 14), 3) == 1.5


def test_predict_uses_neighbour_bin_at_same_hour() -> None:
    profile = TemperatureProfile()
    for _ in range(3):
        profile.observe(28.0, 14, 1.0)  # bin 14
    # 30.9 °C → bin 15; the neighbouring bin answers for the same hour.
    assert profile.predict(30.9, 14) == 1.0


def test_predict_falls_back_to_bin_average_across_hours() -> None:
    profile = TemperatureProfile()
    for hour in (10, 11, 12):
        profile.observe(30.0, hour, 2.0)
    # Hour 14 has no cell of its own → the bin's all-hours average answers.
    assert profile.predict(30.0, 14) == 2.0


def test_predict_none_without_data() -> None:
    assert TemperatureProfile().predict(20.0, 12) is None
    # A far-away temperature region stays unanswered even with data elsewhere.
    profile = TemperatureProfile()
    for _ in range(3):
        profile.observe(30.0, 14, 1.5)
    assert profile.predict(-5.0, 14) is None


def test_profile_serialisation_roundtrip() -> None:
    profile = TemperatureProfile()
    for _ in range(3):
        profile.observe(25.0, 9, 1.25)
    profile.mark_date_observed(date(2026, 7, 1))
    restored = TemperatureProfile.from_dict(profile.to_dict())
    assert restored.predict(25.0, 9) == profile.predict(25.0, 9)
    assert restored.observed_days == 1
    assert restored.samples == 3


def test_as_matrix_reports_observed_bins() -> None:
    profile = TemperatureProfile()
    profile.observe(30.0, 14, 1.5)
    profile.observe(30.0, 14, 2.5)
    profile.observe(24.0, 9, 0.5)

    rows = profile.as_matrix()
    assert [r["temp_from"] for r in rows] == [24.0, 30.0]
    assert rows[0]["temp_to"] == 24.0 + TEMP_BIN_C
    assert rows[1]["values"][14] == 2.0  # per-cell average
    assert rows[1]["values"][15] is None  # unobserved hour
    assert rows[1]["samples"] == 2


def _module(config: dict, observed: dict[str, int] | None = None) -> ClimateModule:
    module = object.__new__(ClimateModule)
    module.config = config
    module.profiles = {}
    for eid, days in (observed or {}).items():
        profile = module.profiles.setdefault(eid, TemperatureProfile())
        for i in range(days):
            profile.mark_date_observed(date(2026, 6, 1) + timedelta(days=i))
    return module


def test_handles_requires_config_and_enough_learning() -> None:
    assert not _module({}).handles(AC)
    # Configured but still learning → the weekly profile keeps the device.
    assert not _module({CONF_CLIMATE_SENSORS: [AC]}).handles(AC)
    # Readiness is per sensor: each device earns its own takeover.
    module = _module(
        {CONF_CLIMATE_SENSORS: [AC, HEAT_PUMP]},
        observed={AC: MIN_LEARN_DAYS, HEAT_PUMP: MIN_LEARN_DAYS - 1},
    )
    assert module.handles(AC)
    assert not module.handles(HEAT_PUMP)
    assert not module.handles("sensor.other")


def test_presence_sensors_read_from_config() -> None:
    assert _module({}).presence_sensors == []
    module = _module({CONF_CLIMATE_PRESENCE_SENSORS: ["person.anna"]})
    assert module.presence_sensors == ["person.anna"]


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 8, 4, hour, minute, tzinfo=timezone.utc)


def _st(state: str, at: datetime) -> SimpleNamespace:
    return SimpleNamespace(state=state, last_updated=at)


def _presence_map(states_by_entity: dict) -> dict[str, bool]:
    return ClimateModule._presence_map(states_by_entity, _utc(0), _utc(12))


def _key(hour: int) -> str:
    return dt_util.as_local(_utc(hour)).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()


def test_presence_map_marks_home_and_away_hours() -> None:
    # Home 00:00–03:30, away 03:30–12:00 → hours 0–3 present, 4–11 absent.
    presence = _presence_map(
        {"person.anna": [_st("home", _utc(0)), _st("not_home", _utc(3, 30))]}
    )
    assert presence[_key(0)] is True
    assert presence[_key(3)] is True  # partially-home hour counts as present
    assert presence[_key(4)] is False
    assert presence[_key(11)] is False


def test_presence_map_any_entity_home_wins() -> None:
    # Anna leaves at 02:00 but Bartek is home the whole window → all present.
    presence = _presence_map(
        {
            "person.anna": [_st("home", _utc(0)), _st("not_home", _utc(2))],
            "person.bartek": [_st("home", _utc(0))],
        }
    )
    assert all(presence[_key(h)] for h in range(12))


def test_presence_map_unknown_states_leave_hours_unmapped() -> None:
    # Unavailable readings contribute nothing: those hours stay unknown, and
    # the learner's ``presence.get(key, True)`` default keeps them learning.
    presence = _presence_map(
        {"person.anna": [_st("unavailable", _utc(0)), _st("home", _utc(6))]}
    )
    assert _key(0) not in presence
    assert _key(5) not in presence
    assert presence[_key(6)] is True


def test_presence_map_empty_without_states() -> None:
    assert _presence_map({"person.anna": []}) == {}


def test_profile_reset_clears_cells_and_days() -> None:
    # A presence-config change re-folds the profile from source data; reset
    # is the first half of that: everything learned is dropped for re-fold.
    profile = TemperatureProfile()
    for _ in range(3):
        profile.observe(30.0, 14, 1.5)
    profile.mark_date_observed(date(2026, 8, 1))
    profile.reset()
    assert profile.samples == 0
    assert profile.observed_days == 0
    assert not profile.is_date_observed(date(2026, 8, 1))
    assert profile.predict(30.0, 14) is None
