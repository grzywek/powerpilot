"""How long the charger actually ran, measured — not inferred from energy.

The plan commits to a duration (``ev_charge_minutes``), so "3.2 kWh went in"
cannot answer whether it ran the planned 45 minutes at full power or 60 minutes
tapering. The measurement counts the recorder's 5-minute buckets that carried
real flow, which is the finest resolution the statistics offer.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.modules.consumption import (
    STAT_BUCKET_MIN,
    ConsumptionModule,
)

HOUR = dt_util.now().replace(minute=0, second=0, microsecond=0)


class _State:
    def __init__(self, unit: str) -> None:
        self.attributes = {"unit_of_measurement": unit}


class _States:
    def __init__(self, unit: str | None) -> None:
        self._unit = unit

    def get(self, entity_id: str):
        return _State(self._unit) if self._unit else None


class _Hass:
    def __init__(self, unit: str | None) -> None:
        self.states = _States(unit)


def _module(unit: str | None, rows: list[dict]) -> ConsumptionModule:
    module = object.__new__(ConsumptionModule)
    module.hass = _Hass(unit)
    module._rows = rows
    return module


def _bucket(index: int, **values) -> dict:
    """One 5-minute statistics row, ``index`` buckets into the hour."""
    return {"start": HOUR + timedelta(minutes=STAT_BUCKET_MIN * index), **values}


async def _run(
    monkeypatch: pytest.MonkeyPatch,
    unit: str | None,
    rows: list[dict],
    entity_id: str = "sensor.ev_added",
    start: datetime | None = None,
    end: datetime | None = None,
) -> dict:
    module = _module(unit, rows)

    class _Instance:
        @staticmethod
        async def async_add_executor_job(func, *args):
            return {entity_id: rows}

    monkeypatch.setattr(
        "custom_components.powerpilot.modules.consumption.get_instance",
        lambda hass: _Instance(),
    )
    return await module.async_active_minutes(
        entity_id, start or HOUR, end or HOUR + timedelta(hours=1)
    )


async def test_energy_counter_counts_buckets_that_grew(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A total_increasing kWh counter: flow is the delta between buckets."""
    rows = [
        _bucket(0, sum=10.0),  # lead-in: nothing to difference against
        _bucket(1, sum=10.6),  # +0.6 kWh → charging
        _bucket(2, sum=11.2),  # +0.6 kWh → charging
        _bucket(3, sum=11.2),  # flat → idle
    ]

    assert await _run(monkeypatch, "kWh", rows) == {HOUR: 10}


async def test_a_measured_idle_hour_reads_zero_not_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """"The charger stayed off" and "we cannot say" are different answers."""
    rows = [_bucket(i, sum=10.0) for i in range(4)]

    assert await _run(monkeypatch, "kWh", rows) == {HOUR: 0}


async def test_an_hour_without_statistics_is_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert await _run(monkeypatch, "kWh", []) == {}


async def test_counter_reset_is_not_counted_as_flow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new session zeroes the counter; that drop is not charging."""
    rows = [_bucket(0, sum=9.0), _bucket(1, sum=0.0), _bucket(2, sum=0.4)]

    assert await _run(monkeypatch, "kWh", rows) == {HOUR: 5}


async def test_sensor_jitter_stays_under_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.001 kWh per bucket is a stationary counter, not a charger."""
    rows = [_bucket(i, sum=10.0 + 0.001 * i) for i in range(6)]

    assert await _run(monkeypatch, "kWh", rows) == {HOUR: 0}


async def test_power_sensor_counts_buckets_above_the_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rows = [
        _bucket(0, mean=3000.0),  # 3 kW → charging
        _bucket(1, mean=3000.0),
        _bucket(2, mean=5.0),  # 5 W standby → idle
    ]

    assert await _run(monkeypatch, "W", rows) == {HOUR: 10}


async def test_reset_bucket_still_counts_as_measured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The reset itself is not flow, but the hour was observed all the same."""
    rows = [_bucket(0, sum=9.0), _bucket(1, sum=0.0)]

    assert await _run(monkeypatch, "kWh", rows) == {HOUR: 0}


async def test_minutes_never_exceed_a_full_hour(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More buckets than fit an hour (overlapping stats) must still cap at 60."""
    rows = [_bucket(0, sum=10.0)] + [
        {"start": HOUR + timedelta(minutes=i), "sum": 10.0 + 0.5 * i}
        for i in range(1, 30)
    ]

    assert await _run(monkeypatch, "kWh", rows) == {HOUR: 60}


async def test_a_sensor_without_an_energy_unit_yields_nothing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No unit → no statistics to read; say nothing rather than guess."""
    assert await _run(monkeypatch, None, []) == {}


async def test_empty_window_is_not_queried(monkeypatch: pytest.MonkeyPatch) -> None:
    assert await _run(monkeypatch, "kWh", [], start=HOUR, end=HOUR) == {}
