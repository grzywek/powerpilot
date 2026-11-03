"""The car's learned charging profile behind the month tail's EV import.

Incident 2026-11-02: the plan let the LP place the tail's EV import in the
month's cheapest hours, so October's PPE 1 settled at 0.31 zł/kWh in the model
against 0.56 on the meter — the car is half of PPE 1's energy. The tail now
carries the car's charging per weekday and hour as the recorder saw it over
the latest 28 complete days (ADR 003).
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from types import SimpleNamespace

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_EV_ENERGY_ADDED_SENSOR,
    EV_CHARGE_PROFILE_DAYS,
    EV_CHARGE_PROFILE_LOOKBACK_DAYS,
)
from custom_components.powerpilot.modules.ev import EVModule, _local_day_hours

SENSOR = "sensor.ev_energy_added"
TZ = dt_util.get_time_zone("Europe/Warsaw")


@pytest.fixture(autouse=True)
def _warsaw():
    previous = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(TZ)
    yield
    dt_util.set_default_time_zone(previous)


class _Reader:
    """``consumption.async_range_kwh`` over local-keyed hourly kWh."""

    def __init__(self, series: dict[datetime, float]) -> None:
        self.series = series
        self.calls: list[tuple[datetime, datetime]] = []

    async def async_range_kwh(self, entity_id, start, end):
        assert entity_id == SENSOR
        self.calls.append((start, end))
        return {h: v for h, v in self.series.items() if start <= h < end}


def _module(series: dict[datetime, float], sensor: str | None = SENSOR) -> tuple[EVModule, _Reader]:
    reader = _Reader(series)
    ev = EVModule.__new__(EVModule)
    ev.config = {CONF_EV_ENERGY_ADDED_SENSOR: sensor}
    ev.coordinator = SimpleNamespace(consumption=reader)
    ev._charge_profile = None
    ev._charge_profile_through = None
    ev.charge_profile_reason = "not read"
    return ev, reader


def _days(today: date, count: int, kwh=lambda hour: 0.0) -> dict[datetime, float]:
    """Hourly statistics of the ``count`` days before ``today``."""
    series: dict[datetime, float] = {}
    for back in range(1, count + 1):
        for hour in _local_day_hours(today - timedelta(days=back)):
            series[hour] = kwh(hour)
    return series


TODAY = date(2026, 11, 3)  # a Tuesday
NOW = datetime(2026, 11, 3, 12, 30, tzinfo=TZ)


async def test_profile_is_the_mean_of_each_weekday_and_hour() -> None:
    # Monday 22:00 charged 11, 0, 7, 2 kWh over four weeks; nothing else.
    mondays = {date(2026, 11, 2): 11.0, date(2026, 10, 26): 0.0, date(2026, 10, 19): 7.0,
               date(2026, 10, 12): 2.0}

    def kwh(hour: datetime) -> float:
        return mondays.get(hour.date(), 0.0) if hour.hour == 22 else 0.0

    ev, _ = _module(_days(TODAY, EV_CHARGE_PROFILE_DAYS, kwh))
    await ev._learn_charge_profile(NOW)
    assert ev.charge_profile_reason is None
    monday_22 = datetime(2026, 11, 9, 22, tzinfo=TZ)
    monday_21 = datetime(2026, 11, 9, 21, tzinfo=TZ)
    assert ev.charge_profile_kwh([monday_22, monday_21]) == [pytest.approx(5.0), 0.0]


async def test_a_negative_hour_is_kept() -> None:
    """A dip the recorder booked is that hour's reading (AGENTS.md)."""

    def kwh(hour: datetime) -> float:
        if hour.weekday() == 0 and hour.hour == 3:
            return -0.4 if hour.date() == date(2026, 11, 2) else 0.0
        return 0.0

    ev, _ = _module(_days(TODAY, EV_CHARGE_PROFILE_DAYS, kwh))
    await ev._learn_charge_profile(NOW)
    assert ev.charge_profile_kwh([datetime(2026, 11, 9, 3, tzinfo=TZ)]) == [
        pytest.approx(-0.1)
    ]


async def test_too_few_complete_days_names_the_sensor_and_the_days() -> None:
    series = _days(TODAY, 30)
    # Ten days lose an hour each: 20 complete days are left in the look-back.
    for back in range(1, 11):
        day = TODAY - timedelta(days=back)
        del series[datetime(day.year, day.month, day.day, 12, tzinfo=TZ)]
    ev, _ = _module(series)
    await ev._learn_charge_profile(NOW)
    assert ev.charge_profile_kwh([NOW]) is None
    assert SENSOR in ev.charge_profile_reason
    assert f"20 z {EV_CHARGE_PROFILE_DAYS}" in ev.charge_profile_reason


async def test_an_incomplete_day_is_skipped_for_an_older_complete_one() -> None:
    """The latest 28 COMPLETE days: yesterday still compiling is not a gap."""
    series = _days(TODAY, EV_CHARGE_PROFILE_DAYS + 1, lambda hour: 1.0)
    del series[datetime(2026, 11, 2, 23, tzinfo=TZ)]
    ev, reader = _module(series)
    await ev._learn_charge_profile(NOW)
    assert ev.charge_profile_reason is None
    assert ev._charge_profile.observed_days == EV_CHARGE_PROFILE_DAYS
    assert not ev._charge_profile.is_date_observed(date(2026, 11, 2))
    # The window starts one lead-in hour before the look-back's first day.
    start, end = reader.calls[0]
    first = TODAY - timedelta(days=EV_CHARGE_PROFILE_LOOKBACK_DAYS)
    assert start == dt_util.start_of_local_day(first) - timedelta(hours=1)
    assert end == dt_util.start_of_local_day(TODAY)
    # Yesterday is not in yet, so the next refresh reads again…
    await ev._learn_charge_profile(NOW + timedelta(minutes=10))
    assert len(reader.calls) == 2
    # …until it is; then the profile holds for the day.
    reader.series[datetime(2026, 11, 2, 23, tzinfo=TZ)] = 1.0
    await ev._learn_charge_profile(NOW + timedelta(minutes=20))
    assert ev._charge_profile.is_date_observed(date(2026, 11, 2))
    await ev._learn_charge_profile(NOW + timedelta(hours=3))
    assert len(reader.calls) == 3


async def test_the_autumn_clock_change_day_counts_as_complete() -> None:
    """25.10.2026 has 25 hours; the reader keys its repeated hour once."""
    assert len(_local_day_hours(date(2026, 10, 25))) == 24
    assert len(_local_day_hours(date(2026, 3, 29))) == 23
    ev, _ = _module(_days(date(2026, 10, 27), EV_CHARGE_PROFILE_DAYS))
    await ev._learn_charge_profile(datetime(2026, 10, 27, 12, tzinfo=TZ))
    assert ev.charge_profile_reason is None
    assert ev._charge_profile.is_date_observed(date(2026, 10, 25))


async def test_without_an_energy_sensor_there_is_no_profile() -> None:
    ev, reader = _module({}, sensor=None)
    await ev._learn_charge_profile(NOW)
    assert ev.charge_profile_kwh([NOW]) is None
    assert "czujnika energii" in ev.charge_profile_reason
    assert reader.calls == []


async def test_an_unreadable_recorder_is_the_reason_not_an_empty_profile() -> None:
    ev, _ = _module({})

    async def broken(entity_id, start, end):
        raise RuntimeError("recorder down")

    ev.coordinator.consumption.async_range_kwh = broken
    await ev._learn_charge_profile(NOW)
    assert ev.charge_profile_kwh([NOW]) is None
    assert "recorder down" in ev.charge_profile_reason
