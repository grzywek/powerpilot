"""Hourly and running-hour kWh read from the recorder's statistics."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.modules import consumption
from custom_components.powerpilot.modules.consumption import ConsumptionModule

METER = "sensor.ess_multiplus_ii_energy_from_ac_in_1_to_inverter"
H = dt_util.as_utc(datetime(2026, 10, 12, 9, tzinfo=dt_util.get_default_time_zone()))


class _Recorder:
    """Recorder stand-in returning only the statistic types asked for."""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.calls: list[tuple] = []

    async def async_add_executor_job(self, func, *args):
        return func(*args)

    def statistics(self, hass, start, end, ids, period, units, types):
        self.calls.append((period, set(types)))
        (entity_id,) = ids
        return {
            entity_id: [
                {"start": r["start"], **{t: r[t] for t in types if t in r}}
                for r in self.rows
            ]
        }


@pytest.fixture
def recorder(monkeypatch):
    holder = SimpleNamespace(recorder=None)

    def install(rows: list[dict]) -> _Recorder:
        holder.recorder = _Recorder(rows)
        monkeypatch.setattr(consumption, "get_instance", lambda hass: holder.recorder)
        monkeypatch.setattr(
            consumption, "statistics_during_period", holder.recorder.statistics
        )
        return holder.recorder

    return install


def _module() -> ConsumptionModule:
    module = object.__new__(ConsumptionModule)
    state = SimpleNamespace(attributes={"unit_of_measurement": "kWh"})
    module.hass = SimpleNamespace(states=SimpleNamespace(get=lambda entity_id: state))
    return module


def _ts(moment: datetime) -> float:
    return moment.timestamp()


async def test_a_small_meter_dip_is_that_hours_reading_not_a_missing_hour(
    recorder,
) -> None:
    """Incident 2026-10-17: the Victron AC-in-1 → inverter counter went
    415.500 → 415.498 kWh at 12.10 11:00. The recorder booked −0.002 kWh for
    that hour (a dip under 10 % is not a reset), but the reader dropped the
    hour as a "meter reset", so the price-cap settlement saw an unmeasured
    hour and refused the whole of October.
    """
    sums = [-851.700, -851.700, -851.702, -851.684]  # 09:00 … 12:00 rows
    recorder([{"start": _ts(H + timedelta(hours=i)), "sum": s} for i, s in enumerate(sums)])
    series = await _module().async_range_kwh(METER, H, H + timedelta(hours=4))
    hours = sorted(series)
    # The first row only seeds the difference; 10:00, 11:00 and 12:00 remain.
    assert [h - H for h in hours] == [timedelta(hours=i) for i in (1, 2, 3)]
    assert abs(series[hours[1]] - (-0.002)) < 1e-9
    # The hours add up to the meter's own change over the window.
    assert abs(sum(series.values()) - (sums[-1] - sums[0])) < 1e-9


async def test_the_running_hour_counts_its_first_five_minutes(recorder) -> None:
    """Live check 2026-10-17 on the same meter, 16.10 13:00: the hourly
    statistic says 3.878 kWh and so do the twelve 5-minute changes, while
    differencing the first and last cumulative sums gave 3.568 — the first
    bucket (0.31 kWh) was lost, because a sum row is the total at the END of
    its bucket.
    """
    changes = [0.31, 0.3, 0.32, 0.33, 0.3, 0.31, 0.34, 0.33, 0.32, 0.36, 0.33, 0.328]
    total = 100.0
    rows = []
    for i, change in enumerate(changes):
        total += change
        rows.append({"start": _ts(H + timedelta(minutes=5 * i)), "sum": total, "change": change})
    rec = recorder(rows)
    kwh = await _module().async_partial_kwh(METER, H, H + timedelta(hours=1))
    assert abs(kwh - sum(changes)) < 1e-9
    assert rec.calls == [("5minute", {"change"})]


async def test_the_running_hour_keeps_a_dip_instead_of_clamping_it(recorder) -> None:
    recorder(
        [
            {"start": _ts(H), "sum": 1.0, "change": -0.002},
            {"start": _ts(H + timedelta(minutes=5)), "sum": 1.0, "change": 0.0},
        ]
    )
    kwh = await _module().async_partial_kwh(METER, H, H + timedelta(minutes=10))
    assert abs(kwh - (-0.002)) < 1e-12


async def test_the_running_hour_without_statistics_is_unknown_not_zero(recorder) -> None:
    recorder([])
    assert await _module().async_partial_kwh(METER, H, H + timedelta(minutes=10)) is None
