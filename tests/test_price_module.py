"""Unit tests for the price module's read path and refresh cadence."""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_PRICE_REFRESH_HOURS,
    CONF_PRICE_SOURCE,
    PRICE_SOURCE_PRADCAST,
    PRICE_SOURCE_SENSOR,
    PRICE_TYPE_CERTAIN,
    PRICE_TYPE_FORECAST,
)
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.price_sources import PriceData
from custom_components.powerpilot.modules.prices import PriceModule


@pytest.fixture(autouse=True)
def _warsaw_time():
    """The RDN publication hour is a local-time fact — pin the zone."""
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(zoneinfo.ZoneInfo("Europe/Warsaw"))
    yield
    dt_util.set_default_time_zone(original)


class _StubCoordinator:
    """Just enough coordinator for a module: config + the log sinks."""

    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.messages: list[str] = []

    def log_info(self, domain, message, extra=None) -> None:
        self.messages.append(message)

    def log_warning(self, domain, message, extra=None) -> None:
        self.messages.append(message)


def _module(config: dict | None = None) -> PriceModule:
    return PriceModule(
        None,
        _StubCoordinator({CONF_PRICE_SOURCE: PRICE_SOURCE_PRADCAST, **(config or {})}),
    )


def _confirm_day(data: PriceData, day_start: datetime, price: float) -> None:
    for index in range(24):
        hour = day_start + timedelta(hours=index)
        data.buy[hour] = price
        data.confirmed_hours.add(hour)


def test_archived_certain_outranks_a_live_forecast() -> None:
    """A binding price stays binding when the source re-serves it as a forecast."""
    module = _module()
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    module.archive.record(hour, 1.30, PRICE_TYPE_CERTAIN, PRICE_SOURCE_PRADCAST, "t1")
    module._data.buy[hour] = 9.99  # same hour, but this fetch calls it a forecast

    assert module.price_at(hour) == 1.30
    assert module.price_type_at(hour) == PRICE_TYPE_CERTAIN
    assert module.is_confirmed(hour) is True

    forecast = Forecast(slots=[HourSlot(start=hour)])
    module.contribute(forecast)
    assert forecast.slots[0].buy_price == 1.30
    assert forecast.slots[0].price_confirmed is True


def test_live_forecast_still_wins_over_an_archived_forecast() -> None:
    """Only *certain* is final — a fresh forecast refreshes a stale one."""
    module = _module()
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    module.archive.record(hour, 1.00, PRICE_TYPE_FORECAST, PRICE_SOURCE_PRADCAST, "t1")
    module._data.buy[hour] = 1.20

    assert module.price_at(hour) == 1.20
    assert module.price_type_at(hour) == PRICE_TYPE_FORECAST
    assert module.is_confirmed(hour) is False


def test_hours_outside_the_fetch_window_come_from_the_archive() -> None:
    module = _module()
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0) - timedelta(days=5)
    module.archive.record(hour, 0.80, PRICE_TYPE_FORECAST, PRICE_SOURCE_PRADCAST, "t1")

    forecast = Forecast(slots=[HourSlot(start=hour)])
    module.contribute(forecast)

    assert forecast.slots[0].buy_price == 0.80
    assert "price_archived" in forecast.slots[0].tags


def test_awaiting_fixing_only_after_publication_and_while_tomorrow_is_forecast() -> None:
    module = _module()
    with freeze_time("2026-08-22 08:00:00+02:00"):
        now = dt_util.now()
        # Before ~10:45 tomorrow is legitimately still a forecast.
        assert module._awaiting_fixing(now) is False

    with freeze_time("2026-08-22 12:00:00+02:00"):
        now = dt_util.now()
        assert module._awaiting_fixing(now) is True

        _confirm_day(module._data, dt_util.start_of_local_day(now + timedelta(days=1)), 0.5)
        assert module._awaiting_fixing(now) is False


def test_a_price_sensor_is_not_judged_against_the_rdn_clock() -> None:
    """Only Pradcast serves TGE Fixing I directly; sensors have their own cadence."""
    module = _module({CONF_PRICE_SOURCE: PRICE_SOURCE_SENSOR})
    with freeze_time("2026-08-22 12:00:00+02:00"):
        assert module._awaiting_fixing(dt_util.now()) is False


async def test_update_refetches_after_the_fixing_despite_the_throttle() -> None:
    """The published RDN prices must not wait out ``price_refresh_hours``."""
    module = _module({CONF_PRICE_REFRESH_HOURS: 3})
    fetched: list[datetime] = []

    class _Source:
        async def async_fetch(self) -> PriceData:
            fetched.append(dt_util.now())
            return PriceData(buy={dt_util.now(): 0.5})

    module._build_source = lambda: _Source()

    with freeze_time("2026-08-22 12:00:00+02:00"):
        module._data = PriceData(buy={dt_util.now(): 0.5})
        module._last_source_fetch = dt_util.now() - timedelta(minutes=40)
        await module.async_update()
        assert len(fetched) == 1  # 40 min < 3 h, but tomorrow is still a forecast

        # Just re-fetched: the tighter cadence still applies a floor.
        module._last_source_fetch = dt_util.now() - timedelta(minutes=5)
        await module.async_update()
        assert len(fetched) == 1


async def test_update_keeps_the_configured_throttle_once_tomorrow_is_confirmed() -> None:
    module = _module({CONF_PRICE_REFRESH_HOURS: 3})
    fetched: list[datetime] = []

    class _Source:
        async def async_fetch(self) -> PriceData:
            fetched.append(dt_util.now())
            return PriceData()

    module._build_source = lambda: _Source()

    with freeze_time("2026-08-22 12:00:00+02:00"):
        now = dt_util.now()
        module._data = PriceData(buy={now: 0.5})
        _confirm_day(module._data, dt_util.start_of_local_day(now + timedelta(days=1)), 0.5)
        module._last_source_fetch = now - timedelta(minutes=40)
        await module.async_update()

    assert fetched == []
