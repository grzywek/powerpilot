"""Observed month-to-date settlement under the monthly price cap (Tarcza)."""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_PPE1_IMPORT_SENSORS,
    CONF_TARCZA_ENABLED,
    CONF_TARCZA_LIMIT_KWH,
    CONF_TARCZA_VALID_FROM,
    CONF_TARCZA_VALID_TO,
    PPE_1,
)
from custom_components.powerpilot.modules.billing import (
    BillingModule,
    month_bounds,
    next_month_start,
)

NOW = "2026-09-14 10:30:00+02:00"
A, B = "sensor.victron_ac_in", "sensor.ev_meter"


@pytest.fixture(autouse=True)
def _warsaw_time():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(zoneinfo.ZoneInfo("Europe/Warsaw"))
    yield
    dt_util.set_default_time_zone(original)


class _StubCoordinator:
    def __init__(self, config: dict | None = None) -> None:
        self.config = config or {}
        self.messages: list[str] = []

    def log_info(self, domain, message, extra=None) -> None:
        self.messages.append(message)

    def log_warning(self, domain, message, extra=None) -> None:
        self.messages.append(message)


class _Meters:
    """Recorder stand-in: hourly kWh per entity + the running hour's partial."""

    def __init__(self, hourly: dict[str, dict[datetime, float]], partial: dict) -> None:
        self.hourly = hourly
        self.partial = partial

    async def async_range_kwh(self, entity_id, start, end):
        return {
            h: v for h, v in self.hourly.get(entity_id, {}).items() if start <= h < end
        }

    async def async_partial_kwh(self, entity_id, start, end):
        return self.partial.get(entity_id)


class _Prices:
    def __init__(self, caps: dict[datetime, float]) -> None:
        self.caps = caps

    def cap_price_at(self, hour):
        return self.caps.get(hour)


def _config(**overrides) -> dict:
    cfg = {
        CONF_TARCZA_ENABLED: True,
        CONF_TARCZA_LIMIT_KWH: 0.61,
        CONF_TARCZA_VALID_FROM: "2025-01-01",
        CONF_TARCZA_VALID_TO: "2026-12-31",
        CONF_PPE1_IMPORT_SENSORS: [A, B],
    }
    cfg.update(overrides)
    return cfg


def _hours(start: datetime, end: datetime) -> list[datetime]:
    out = []
    h = start
    while h < end:
        out.append(h)
        h += timedelta(hours=1)
    return out


def _month_hours() -> list[datetime]:
    """Completed hours of the current (frozen) month, month start → now."""
    month_start, _ = month_bounds(dt_util.now())
    hour_now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    return _hours(month_start, hour_now)


def _fixture(*, drop_b: set | None = None, drop_cap: set | None = None, partial=None):
    """Full September data up to NOW: A = 1 kWh/h, B = 0.5 kWh/h, cap 0.5 PLN."""
    hour_now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    hours = _month_hours()
    hourly = {
        A: {h: 1.0 for h in hours},
        B: {h: 0.5 for h in hours if h not in (drop_b or set())},
    }
    caps = {h: 0.5 for h in hours + [hour_now] if h not in (drop_cap or set())}
    partial = {A: 0.2, B: 0.1} if partial is None else partial
    return _Meters(hourly, partial), _Prices(caps), hours, hour_now


def _module(meters, prices, **overrides) -> BillingModule:
    return BillingModule(None, _StubCoordinator(_config(**overrides)), meters, prices)


# ------------------------------------------------------------------ helpers


def test_month_bounds_and_next_month_start_roll_over_december() -> None:
    with freeze_time("2026-12-20 12:00:00+01:00"):
        start, end = month_bounds(dt_util.now())
    assert (start.year, start.month, start.day, start.hour) == (2026, 12, 1, 0)
    assert (end.year, end.month, end.day) == (2027, 1, 1)
    assert next_month_start(datetime(2026, 12, 31).date()).isoformat() == "2027-01-01"


# ------------------------------------------------------------------ observed


async def test_complete_month_to_date_sums_every_meter_at_the_cap_price() -> None:
    with freeze_time(NOW):
        meters, prices, hours, _ = _fixture()
        module = _module(meters, prices)
        await module.async_update()
    period = module.periods[PPE_1]
    n = len(hours)
    assert period.hours_expected == n + 1  # completed hours + the running one
    assert period.hours_covered == n + 1
    assert period.complete
    assert abs(period.energy_kwh - (n * 1.5 + 0.3)) < 1e-9
    assert abs(period.cost_pln - (n * 1.5 + 0.3) * 0.5) < 1e-9
    assert abs(period.average_kwh - 0.5) < 1e-9
    assert period.limit_kwh == 0.61


async def test_missing_reading_on_one_meter_is_an_unknown_hour_not_zero() -> None:
    with freeze_time(NOW):
        hours = _month_hours()
        meters, prices, _, _ = _fixture(drop_b={hours[5]})
        module = _module(meters, prices)
        await module.async_update()
    period = module.periods[PPE_1]
    assert not period.complete
    assert period.hours_covered == period.hours_expected - 1
    # The hour's energy from meter A is NOT counted either: the PPE total for
    # that hour is unknown.
    n = len(hours)
    assert abs(period.energy_kwh - ((n - 1) * 1.5 + 0.3)) < 1e-9
    assert any("brak odczytu sensor.ev_meter" in m for m in period.missing)


async def test_missing_cap_price_is_a_gap() -> None:
    with freeze_time(NOW):
        hours = _month_hours()
        meters, prices, _, _ = _fixture(drop_cap={hours[0], hours[1]})
        module = _module(meters, prices)
        await module.async_update()
    period = module.periods[PPE_1]
    assert period.hours_covered == period.hours_expected - 2
    assert sum("brak ceny pod Tarczę" in m for m in period.missing) == 2


async def test_unreadable_running_hour_is_a_gap() -> None:
    with freeze_time(NOW):
        meters, prices, hours, _ = _fixture(partial={A: 0.2, B: None})
        module = _module(meters, prices)
        await module.async_update()
    period = module.periods[PPE_1]
    assert period.hours_covered == len(hours)
    assert period.hours_expected == len(hours) + 1
    assert any("(bieżąca)" in m for m in period.missing)


async def test_many_gaps_are_summarised() -> None:
    with freeze_time(NOW):
        hours = _month_hours()
        meters, prices, _, _ = _fixture(drop_b=set(hours[:10]))
        module = _module(meters, prices)
        await module.async_update()
    period = module.periods[PPE_1]
    assert len(period.missing) == 5  # 4 listed + "… i 6 więcej"
    assert period.missing[-1].endswith("6 więcej")


# ------------------------------------------------------------- configuration


async def test_disabled_or_unconfigured_yields_no_period() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _fixture()
        module = _module(meters, prices, **{CONF_TARCZA_ENABLED: False})
        await module.async_update()
        assert module.periods == {}
        assert not module.active_on(dt_util.now().date())

        module = _module(meters, prices, **{CONF_PPE1_IMPORT_SENSORS: []})
        await module.async_update()
        assert module.periods == {}
        assert any("liczników" in r for r in module.reasons)


async def test_outside_validity_is_inactive_with_reason() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _fixture()
        module = _module(meters, prices, **{CONF_TARCZA_VALID_TO: "2026-08-31"})
        await module.async_update()
        assert module.periods == {}
        assert any("nieaktywna" in r for r in module.reasons)


async def test_malformed_validity_dates_disable_the_cap_loudly() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _fixture()
        module = _module(meters, prices, **{CONF_TARCZA_VALID_TO: "31.12.2026"})
        await module.async_update()
        assert module.periods == {}
        assert module.validity() is None
        assert any("niepoprawne" in r for r in module.reasons)
        assert any("niepoprawne" in m for m in module.coordinator.messages)


# ---------------------------------------------------------- horizon periods


async def test_periods_touching_spans_the_month_boundary() -> None:
    with freeze_time("2026-09-28 10:30:00+02:00"):
        meters, prices, _, _ = _fixture()
        module = _module(meters, prices)
        await module.async_update()
        first = dt_util.now().replace(minute=0, second=0, microsecond=0)
        last = first + timedelta(hours=167)
        periods = module.periods_touching(PPE_1, first, last)
    assert [p.month_start.month for p in periods] == [9, 10]
    assert periods[0] is module.periods[PPE_1]
    assert periods[1].energy_kwh == 0.0 and periods[1].complete


async def test_periods_touching_skips_months_the_cap_is_not_valid_in() -> None:
    with freeze_time("2026-12-28 10:30:00+01:00"):
        meters, prices, _, _ = _fixture()
        module = _module(meters, prices)
        await module.async_update()
        first = dt_util.now().replace(minute=0, second=0, microsecond=0)
        periods = module.periods_touching(PPE_1, first, first + timedelta(hours=167))
    assert [p.month_start.month for p in periods] == [12]
