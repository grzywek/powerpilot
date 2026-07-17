"""Unit tests for the temperature-dependent climate profile."""

from __future__ import annotations

from datetime import date, timedelta

from custom_components.powerpilot.const import CONF_CLIMATE_SENSOR
from custom_components.powerpilot.modules.climate import (
    MIN_LEARN_DAYS,
    ClimateModule,
    TemperatureProfile,
)

AC = "sensor.ac_energy"


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


def _module(config: dict, observed_days: int = 0) -> ClimateModule:
    module = object.__new__(ClimateModule)
    module.config = config
    module.profile = TemperatureProfile()
    for i in range(observed_days):
        module.profile.mark_date_observed(date(2026, 6, 1) + timedelta(days=i))
    return module


def test_handles_requires_config_and_enough_learning() -> None:
    assert not _module({}).handles(AC)
    # Configured but still learning → the weekly profile keeps the device.
    assert not _module({CONF_CLIMATE_SENSOR: AC}).handles(AC)
    ready = _module({CONF_CLIMATE_SENSOR: AC}, observed_days=MIN_LEARN_DAYS)
    assert ready.handles(AC)
    assert not ready.handles("sensor.other")
