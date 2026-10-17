"""Observed month-to-date settlement under the monthly price cap (Tarcza)."""

from __future__ import annotations

import json
import logging
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from freezegun import freeze_time
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_PPE1_IMPORT_SENSORS,
    CONF_PRICE_VAT,
    CONF_TARCZA_TERMS,
    DOMAIN,
    PPE_1,
    PPE_2,
    SUPPLY_HOUR_MIXED,
    SUPPLY_HOUR_UNKNOWN,
    SUPPLY_METER_2,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.models import (
    CAP_TERMS_DATES_ORDER,
    CAP_TERMS_LIMIT,
    CAP_TERMS_OVERLAP,
    CAP_TERMS_WHOLE_MONTHS,
    Forecast,
    HourSlot,
    PriceCapTerms,
    SupplyTransition,
    price_cap_problems,
    price_cap_terms_for_day,
)
from custom_components.powerpilot.modules.billing import (
    BillingModule,
    SplitArchive,
    month_bounds,
    next_month_start,
    supply_segments,
)

NOW = "2026-09-14 10:30:00+02:00"
A, B = "sensor.victron_ac_in", "sensor.ev_meter"

# The two editions of Tarcza Pstryk as their regulations state them.
TERMS_2026 = {
    "id": "t2026",
    "name": "Tarcza Pstryk 2026",
    "valid_from": "2025-01-01",
    "valid_to": "2026-12-31",
    "limit_kwh": 0.61,
    "limit_includes_vat": True,
    "handling_fee_included": True,
}
TERMS_2027 = {
    "id": "t2027",
    "name": "Tarcza Pstryk 2027",
    "valid_from": "2027-01-01",
    "valid_to": "2027-12-31",
    "limit_kwh": 0.50,
    "limit_includes_vat": False,
    "handling_fee_included": False,
}


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
        self.entry = SimpleNamespace(entry_id="entry1")

    def log_info(self, domain, message, extra=None) -> None:
        self.messages.append(message)

    def log_warning(self, domain, message, extra=None) -> None:
        self.messages.append(message)


class _Meters:
    """Recorder stand-in: hourly kWh per entity + 5-minute sums per window.

    ``partial`` answers for the running hour; ``buckets`` for a completed
    hour read from its 5-minute statistics, keyed ``(entity_id, hour)``;
    ``windows`` for any other sub-hour window, keyed ``(entity_id, start,
    end)`` (a window not listed there has no compiled bucket).
    """

    def __init__(
        self,
        hourly: dict[str, dict[datetime, float]],
        partial: dict,
        buckets: dict | None = None,
        windows: dict | None = None,
    ) -> None:
        self.hourly = hourly
        self.partial = partial
        self.buckets = buckets or {}
        self.windows = windows or {}

    async def async_range_kwh(self, entity_id, start, end):
        return {
            h: v for h, v in self.hourly.get(entity_id, {}).items() if start <= h < end
        }

    async def async_partial_kwh(self, entity_id, start, end):
        if (entity_id, start, end) in self.windows:
            return self.windows[(entity_id, start, end)]
        if end - start == timedelta(hours=1):
            return self.buckets.get((entity_id, start))
        if start == start.replace(minute=0, second=0, microsecond=0):
            return self.partial.get(entity_id)
        return None


class _Supply:
    """Supply-module stand-in: per-hour states, the transition log, readiness.

    ``states`` overrides the supply of single (local) hours; every other hour
    is ``default``. ``state`` is the last transition's, as on the module.
    """

    def __init__(
        self,
        *,
        active: bool = True,
        ready: bool = True,
        default: str = SUPPLY_VICTRON,
        states: dict | None = None,
        transitions: list | None = None,
        apartment: str = "sensor.apartment_energy",
        error: Exception | None = None,
    ) -> None:
        self.active = active
        self.history_ready = ready
        self.apartment_meter = apartment if active else ""
        self.default = default
        self.states = states or {}
        self.transitions = transitions or [_transition(SUPPLY_VICTRON, "2026-08-20 12:00")]
        self.error = error
        self.calls: list[tuple[datetime, datetime]] = []

    @property
    def state(self):
        return self.transitions[-1].state if self.transitions else None

    async def async_past_hour_states(self, start, end):
        self.calls.append((start, end))
        if self.error is not None:
            raise self.error
        out = {}
        hour = dt_util.as_utc(start)
        while hour < dt_util.as_utc(end):
            out[hour] = self.default
            hour += timedelta(hours=1)
        for local, state in self.states.items():
            out[dt_util.as_utc(local)] = state
        return out


def _transition(state: str, local: str) -> SupplyTransition:
    """A confirmed switch at a local wall-clock moment (``YYYY-MM-DD HH:MM[:SS]``)."""
    at = datetime.fromisoformat(local).replace(tzinfo=dt_util.get_default_time_zone())
    return SupplyTransition(
        state=state,
        at=dt_util.as_utc(at),
        not_before=dt_util.as_utc(at) - timedelta(minutes=1),
        apartment_w=800.0,
        victron_net_w=0.0 if state == SUPPLY_METER_2 else 800.0,
        source="live",
    )


def _local(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=dt_util.get_default_time_zone())


class _Prices:
    """Cap prices per hour; ``cap_price_for`` prices from fixed components."""

    def __init__(self, caps: dict[datetime, float]) -> None:
        self.caps = caps
        self.terms_seen: list[str] = []

    def cap_price_at(self, hour, terms):
        self.terms_seen.append(terms.name)
        return self.caps.get(hour)

    def cap_price_for(self, hour, terms):
        return terms.cap_price(0.40, 0.08, 0.23)


def _config(**overrides) -> dict:
    cfg = {
        CONF_TARCZA_TERMS: [TERMS_2026],
        CONF_PRICE_VAT: 1.23,
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


def _fixture(
    *,
    drop_a: set | None = None,
    drop_b: set | None = None,
    drop_cap: set | None = None,
    partial=None,
):
    """Full September data up to NOW: A = 1 kWh/h, B = 0.5 kWh/h, cap 0.5 PLN."""
    hour_now = dt_util.now().replace(minute=0, second=0, microsecond=0)
    hours = _month_hours()
    hourly = {
        A: {h: 1.0 for h in hours if h not in (drop_a or set())},
        B: {h: 0.5 for h in hours if h not in (drop_b or set())},
    }
    caps = {h: 0.5 for h in hours + [hour_now] if h not in (drop_cap or set())}
    partial = {A: 0.2, B: 0.1} if partial is None else partial
    return _Meters(hourly, partial), _Prices(caps), hours, hour_now


def _module(meters, prices, supply=None, **overrides) -> BillingModule:
    supply = _Supply(active=False) if supply is None else supply
    return BillingModule(None, _StubCoordinator(_config(**overrides)), meters, prices, supply)


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
    assert period.terms_name == "Tarcza Pstryk 2026"
    assert set(prices.terms_seen) == {"Tarcza Pstryk 2026"}


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


async def test_the_hour_that_just_ended_is_read_from_its_5_minute_buckets() -> None:
    """Incident 2026-10-17: the plan re-runs at the top of the hour, before the
    recorder has compiled the hour that ended and the first bucket of the new
    one. Both were reported as missing, so every hour-boundary plan ran
    without the cap. Statistics still being compiled are pending, not gaps.
    """
    with freeze_time("2026-09-14 10:03:00+02:00"):
        hours = _month_hours()
        last = hours[-1]  # 09:00, ended three minutes ago
        meters, prices, _, _ = _fixture(drop_a={last}, drop_b={last}, partial={A: None, B: None})
        meters.buckets = {(A, last): 0.9, (B, last): 0.4}
        module = _module(meters, prices)
        await module.async_update()
    period = module.periods[PPE_1]
    assert period.complete
    assert period.hours_covered == period.hours_expected == len(hours) + 1
    n = len(hours) - 1
    assert abs(period.energy_kwh - (n * 1.5 + 1.3)) < 1e-9
    assert any("09:00" in p and "5-minutowych" in p for p in period.pending)
    assert any("(bieżąca)" in p for p in period.pending)


async def test_an_ended_hour_without_any_bucket_is_a_gap_even_when_recent() -> None:
    with freeze_time("2026-09-14 10:03:00+02:00"):
        hours = _month_hours()
        meters, prices, _, _ = _fixture(drop_b={hours[-1]}, partial={A: None, B: None})
        module = _module(meters, prices)
        await module.async_update()
    period = module.periods[PPE_1]
    assert not period.complete
    assert any("09:00: brak odczytu sensor.ev_meter" in m for m in period.missing)


async def test_statistics_overdue_past_the_recorder_latency_are_gaps() -> None:
    with freeze_time("2026-09-14 10:16:00+02:00"):
        hours = _month_hours()
        meters, prices, _, _ = _fixture(drop_b={hours[-1]}, partial={A: None, B: None})
        meters.buckets = {(B, hours[-1]): 0.4}
        module = _module(meters, prices)
        await module.async_update()
    period = module.periods[PPE_1]
    assert not period.complete
    assert period.pending == ()
    assert any("09:00: brak odczytu sensor.ev_meter" in m for m in period.missing)
    assert any("(bieżąca): brak odczytu" in m for m in period.missing)


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
        module = _module(meters, prices, **{CONF_TARCZA_TERMS: []})
        await module.async_update()
        assert not module.enabled
        assert module.periods == {}
        assert module.reasons == []
        assert not module.active_on(dt_util.now().date())

        module = _module(meters, prices, **{CONF_PPE1_IMPORT_SENSORS: []})
        await module.async_update()
        assert module.periods == {}
        assert any("liczników" in r for r in module.reasons)


async def test_outside_validity_is_inactive_with_reason() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _fixture()
        ended = {**TERMS_2026, "valid_to": "2026-08-31"}
        module = _module(meters, prices, **{CONF_TARCZA_TERMS: [ended]})
        await module.async_update()
        assert module.periods == {}
        assert any("nieaktywna" in r for r in module.reasons)


async def test_malformed_terms_disable_the_cap_loudly() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _fixture()
        broken = {**TERMS_2026, "valid_to": "31.12.2026"}
        module = _module(meters, prices, **{CONF_TARCZA_TERMS: [broken]})
        await module.async_update()
        assert module.periods == {}
        assert module.terms() == []
        assert "niepoprawna konfiguracja" in module.config_error
        assert any("niepoprawna" in r for r in module.reasons)
        assert any("niepoprawna" in m for m in module.coordinator.messages)


async def test_overlapping_or_partial_month_terms_are_refused_not_guessed() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _fixture()
        overlapping = {**TERMS_2027, "valid_from": "2026-12-01"}
        module = _module(meters, prices, **{CONF_TARCZA_TERMS: [TERMS_2026, overlapping]})
        await module.async_update()
        assert module.periods == {}
        assert "nakładają" in module.config_error
        assert not module.active_on(dt_util.now().date())

        partial = {**TERMS_2026, "valid_from": "2025-01-15"}
        module = _module(meters, prices, **{CONF_TARCZA_TERMS: [partial]})
        await module.async_update()
        assert "pełnych miesięcy" in module.config_error
        # Logged once per distinct problem, not on every refresh.
        await module.async_update()
        assert sum("pełnych miesięcy" in m for m in module.coordinator.messages) == 1


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


async def test_each_month_is_settled_under_its_own_edition() -> None:
    """2026 → 2027: the limit and its basis change with the edition."""
    with freeze_time("2026-12-28 10:30:00+01:00"):
        meters, prices, _, _ = _fixture()
        module = _module(meters, prices, **{CONF_TARCZA_TERMS: [TERMS_2026, TERMS_2027]})
        await module.async_update()
        first = dt_util.now().replace(minute=0, second=0, microsecond=0)
        december, january = module.periods_touching(
            PPE_1, first, first + timedelta(hours=167)
        )
    assert december is module.periods[PPE_1]
    assert december.terms_name == "Tarcza Pstryk 2026"
    assert december.limit_kwh == 0.61  # stated gross
    assert january.terms_name == "Tarcza Pstryk 2027"
    assert abs(january.limit_kwh - 0.50 * 1.23) < 1e-9  # stated net
    assert january.energy_kwh == 0.0 and january.complete


async def test_a_capped_month_after_an_uncapped_one_is_still_planned() -> None:
    """Only a 2027 edition configured: December has no cap, January does."""
    with freeze_time("2026-12-28 10:30:00+01:00"):
        meters, prices, _, _ = _fixture()
        module = _module(meters, prices, **{CONF_TARCZA_TERMS: [TERMS_2027]})
        await module.async_update()
        assert module.periods == {}
        first = dt_util.now().replace(minute=0, second=0, microsecond=0)
        periods = module.periods_touching(PPE_1, first, first + timedelta(hours=167))
    assert [p.month_start.month for p in periods] == [1]


# --------------------------------------------------------------------- PPE 2
#
# PPE 2 feeds only the apartment: its import in an hour is the apartment
# meter's energy times the part of the hour the apartment was on meter 2.

APT = "sensor.apartment_energy"


def _ppe2_fixture(*, drop_apt: set | None = None, apt_partial=0.12, windows=None, **kwargs):
    """``_fixture`` plus the apartment meter: 0.3 kWh in every completed hour."""
    meters, prices, hours, hour_now = _fixture(**kwargs)
    meters.hourly[APT] = {h: 0.3 for h in hours if h not in (drop_apt or set())}
    meters.partial[APT] = apt_partial
    meters.windows = windows or {}
    return meters, prices, hours, hour_now


async def test_ppe2_counts_the_apartment_energy_only_in_meter_2_hours() -> None:
    with freeze_time(NOW):
        hours = _month_hours()
        meters, prices, _, _ = _ppe2_fixture()
        prices.caps[hours[3]] = 0.9
        supply = _Supply(states={hours[3]: SUPPLY_METER_2, hours[4]: SUPPLY_METER_2})
        module = _module(meters, prices, supply)
        await module.async_update()
        month_start, _ = month_bounds(dt_util.now())
    period = module.periods[PPE_2]
    assert period.ppe == PPE_2
    # Victron hours are 0 kWh on PPE 2 (and the running hour is on the Victron).
    assert abs(period.energy_kwh - 0.6) < 1e-9
    assert abs(period.cost_pln - (0.3 * 0.9 + 0.3 * 0.5)) < 1e-9
    assert period.complete
    assert period.hours_expected == period.hours_covered == len(hours) + 1
    assert supply.calls == [(month_start, hours[-1] + timedelta(hours=1))]
    # PPE 1 is read exactly as before.
    n = len(hours)
    assert abs(module.periods[PPE_1].energy_kwh - (n * 1.5 + 0.3)) < 1e-9


async def test_ppe2_splits_a_mixed_hour_at_each_transition_from_5_minute_energy() -> None:
    """To meter 2 at 07:12:30, back at 08:31: the meter-2 part of 07:00 is
    07:10–08:00 and of 08:00 is 08:00–08:30 (each switch cut at the 5-minute
    bucket it was first seen in)."""
    with freeze_time(NOW):
        hours = _month_hours()
        seven, eight = _local("2026-09-14 07:00"), _local("2026-09-14 08:00")
        windows = {
            (APT, _local("2026-09-14 07:10"), eight): 0.2,
            (APT, eight, _local("2026-09-14 08:30")): 0.15,
        }
        meters, prices, _, _ = _ppe2_fixture(windows=windows)
        prices.caps[seven] = 0.8
        supply = _Supply(
            states={seven: SUPPLY_HOUR_MIXED, eight: SUPPLY_HOUR_MIXED},
            transitions=[
                _transition(SUPPLY_VICTRON, "2026-08-20 12:00"),
                _transition(SUPPLY_METER_2, "2026-09-14 07:12:30"),
                _transition(SUPPLY_VICTRON, "2026-09-14 08:31:00"),
            ],
        )
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert period.complete
    assert period.hours_covered == len(hours) + 1
    assert abs(period.energy_kwh - 0.35) < 1e-9
    assert abs(period.cost_pln - (0.2 * 0.8 + 0.15 * 0.5)) < 1e-9


async def test_ppe2_mixed_hour_with_both_switches_inside_reads_the_part_between() -> None:
    with freeze_time(NOW):
        five = _local("2026-09-14 05:00")
        windows = {(APT, _local("2026-09-14 05:20"), _local("2026-09-14 05:45")): 0.1}
        meters, prices, _, _ = _ppe2_fixture(windows=windows)
        supply = _Supply(
            states={five: SUPPLY_HOUR_MIXED},
            transitions=[
                _transition(SUPPLY_VICTRON, "2026-08-20 12:00"),
                _transition(SUPPLY_METER_2, "2026-09-14 05:22:40"),
                _transition(SUPPLY_VICTRON, "2026-09-14 05:47:10"),
            ],
        )
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert period.complete
    assert abs(period.energy_kwh - 0.1) < 1e-9


async def test_ppe2_unknown_supply_is_a_gap_with_its_reason_never_zero() -> None:
    with freeze_time(NOW):
        hours = _month_hours()
        # 02:00 unknown; 03:00 mixed before the log knows any state (no
        # transition before it), so its parts cannot be told either.
        supply = _Supply(
            states={hours[2]: SUPPLY_HOUR_UNKNOWN, hours[3]: SUPPLY_HOUR_MIXED},
            transitions=[_transition(SUPPLY_METER_2, "2026-09-14 09:40")],
        )
        meters, prices, _, _ = _ppe2_fixture()
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert not period.complete
    assert period.hours_covered == period.hours_expected - 2
    assert any("01.09 02:00: stan zasilania mieszkania nieznany" in m for m in period.missing)
    assert any("01.09 03:00: stan zasilania mieszkania nieznany" in m for m in period.missing)


async def test_ppe2_missing_apartment_reading_is_a_gap() -> None:
    with freeze_time(NOW):
        hours = _month_hours()
        six = _local("2026-09-14 06:00")
        supply = _Supply(
            states={hours[4]: SUPPLY_METER_2, six: SUPPLY_HOUR_MIXED},
            transitions=[
                _transition(SUPPLY_VICTRON, "2026-08-20 12:00"),
                _transition(SUPPLY_METER_2, "2026-09-14 06:40"),
                _transition(SUPPLY_VICTRON, "2026-09-14 06:55"),
            ],
        )
        # No hourly statistic for 04:00, no 5-minute bucket for 06:40–06:55.
        meters, prices, _, _ = _ppe2_fixture(drop_apt={hours[4]})
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert not period.complete
    assert period.hours_covered == period.hours_expected - 2
    assert any(f"01.09 04:00: brak odczytu {APT}" in m for m in period.missing)
    assert any(f"14.09 06:00: brak odczytu {APT}" in m for m in period.missing)
    assert period.energy_kwh == 0.0


async def test_ppe2_hour_that_just_ended_is_read_from_buckets_and_running_is_pending() -> None:
    with freeze_time("2026-09-14 10:03:00+02:00"):
        hours = _month_hours()
        last = hours[-1]  # 09:00, ended three minutes ago
        meters, prices, _, _ = _ppe2_fixture(drop_apt={last}, apt_partial=None)
        meters.buckets = {(APT, last): 0.25}
        supply = _Supply(
            default=SUPPLY_METER_2,
            transitions=[_transition(SUPPLY_METER_2, "2026-08-20 12:00")],
        )
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert period.complete
    assert period.hours_covered == period.hours_expected == len(hours) + 1
    assert abs(period.energy_kwh - ((len(hours) - 1) * 0.3 + 0.25)) < 1e-9
    assert any("09:00" in p and "5-minutowych" in p for p in period.pending)
    assert any("(bieżąca)" in p for p in period.pending)


async def test_ppe2_statistics_overdue_past_the_recorder_latency_are_gaps() -> None:
    with freeze_time("2026-09-14 10:16:00+02:00"):
        hours = _month_hours()
        meters, prices, _, _ = _ppe2_fixture(drop_apt={hours[-1]}, apt_partial=None)
        meters.buckets = {(APT, hours[-1]): 0.25}
        supply = _Supply(
            default=SUPPLY_METER_2,
            transitions=[_transition(SUPPLY_METER_2, "2026-08-20 12:00")],
        )
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert period.pending == ()
    assert any(f"09:00: brak odczytu {APT}" in m for m in period.missing)
    assert any(f"(bieżąca): brak odczytu {APT}" in m for m in period.missing)


async def test_ppe2_running_hour_on_meter_2_counts_its_elapsed_part() -> None:
    with freeze_time(NOW):
        meters, prices, hours, _ = _ppe2_fixture(apt_partial=0.12)
        supply = _Supply(transitions=[_transition(SUPPLY_METER_2, "2026-08-20 12:00")])
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert period.complete
    # Every completed hour is on the Victron in the stub's states; the
    # running hour follows the confirmed state, meter 2.
    assert abs(period.energy_kwh - 0.12) < 1e-9


async def test_ppe2_running_hour_is_split_at_a_transition_inside_it() -> None:
    with freeze_time(NOW):
        now = dt_util.now()
        windows = {(APT, _local("2026-09-14 10:10"), now): 0.07}
        meters, prices, _, _ = _ppe2_fixture(windows=windows)
        supply = _Supply(
            transitions=[
                _transition(SUPPLY_VICTRON, "2026-08-20 12:00"),
                _transition(SUPPLY_METER_2, "2026-09-14 10:12:30"),
            ]
        )
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert period.complete
    assert abs(period.energy_kwh - 0.07) < 1e-9

    with freeze_time(NOW):
        windows = {(APT, _local("2026-09-14 10:00"), _local("2026-09-14 10:10")): 0.05}
        meters, prices, _, _ = _ppe2_fixture(windows=windows)
        supply = _Supply(
            transitions=[
                _transition(SUPPLY_METER_2, "2026-08-20 12:00"),
                _transition(SUPPLY_VICTRON, "2026-09-14 10:12:30"),
            ]
        )
        module = _module(meters, prices, supply)
        await module.async_update()
    assert abs(module.periods[PPE_2].energy_kwh - 0.05) < 1e-9


async def test_ppe2_part_after_a_switch_minutes_ago_is_pending_not_a_gap() -> None:
    """The recorder latency applies to every 5-minute window: one that started
    less than 15 minutes ago without a compiled bucket is on its way."""
    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture()
        supply = _Supply(
            transitions=[
                _transition(SUPPLY_VICTRON, "2026-08-20 12:00"),
                _transition(SUPPLY_METER_2, "2026-09-14 10:24:00"),
            ]
        )
        module = _module(meters, prices, supply)
        await module.async_update()
    period = module.periods[PPE_2]
    assert period.complete
    assert any("(bieżąca)" in p and "10:20" in p for p in period.pending)


async def test_ppe2_is_settled_under_the_same_edition_and_limit_as_ppe1() -> None:
    with freeze_time("2026-12-28 10:30:00+01:00"):
        meters, prices, _, _ = _ppe2_fixture()
        module = _module(
            meters, prices, _Supply(), **{CONF_TARCZA_TERMS: [TERMS_2026, TERMS_2027]}
        )
        await module.async_update()
        first = dt_util.now().replace(minute=0, second=0, microsecond=0)
        december, january = module.periods_touching(PPE_2, first, first + timedelta(hours=167))
    ppe1, ppe2 = module.periods[PPE_1], module.periods[PPE_2]
    assert december is ppe2
    assert (ppe2.terms_name, ppe2.limit_kwh) == (ppe1.terms_name, ppe1.limit_kwh)
    assert (ppe2.month_start, ppe2.month_end) == (ppe1.month_start, ppe1.month_end)
    assert ppe2.terms_name == "Tarcza Pstryk 2026" and ppe2.limit_kwh == 0.61
    assert january.ppe == PPE_2 and january.terms_name == "Tarcza Pstryk 2027"
    assert abs(january.limit_kwh - 0.50 * 1.23) < 1e-9
    assert set(prices.terms_seen) == {"Tarcza Pstryk 2026"}


async def test_no_ppe2_period_while_supply_detection_is_off() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture()
        supply = _Supply(active=False)
        module = _module(meters, prices, supply)
        await module.async_update()
    assert set(module.periods) == {PPE_1}
    assert any("wykrywanie zasilania mieszkania wyłączone" in r for r in module.reasons_for(PPE_2))
    # PPE 2's reasons stay off PPE 1, which the plan reads.
    assert module.reasons == module.reasons_for(PPE_1) == []
    assert supply.calls == []


async def test_no_ppe2_period_until_the_supply_history_is_replayed() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture()
        supply = _Supply(ready=False)
        module = _module(meters, prices, supply)
        await module.async_update()
    assert set(module.periods) == {PPE_1}
    assert any("odtwarza" in r for r in module.reasons_for(PPE_2))
    assert supply.calls == []


async def test_ppe2_read_failure_keeps_ppe1_and_surfaces_the_error() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture()
        module = _module(meters, prices, _Supply(error=RuntimeError("recorder down")))
        with pytest.raises(RuntimeError):
            await module.async_update()
    assert set(module.periods) == {PPE_1}
    assert any("błąd odczytu" in r and "recorder down" in r for r in module.reasons_for(PPE_2))
    assert module.reasons_for(PPE_1) == []


async def test_ppe2_is_observed_without_ppe1_meters_and_shares_module_reasons() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture()
        module = _module(meters, prices, _Supply(), **{CONF_PPE1_IMPORT_SENSORS: []})
        await module.async_update()
        assert set(module.periods) == {PPE_2}
        assert any("liczników" in r for r in module.reasons_for(PPE_1))
        assert module.reasons_for(PPE_2) == []

        ended = {**TERMS_2026, "valid_to": "2026-08-31"}
        module = _module(meters, prices, _Supply(), **{CONF_TARCZA_TERMS: [ended]})
        await module.async_update()
        assert module.periods == {}
        assert any("nieaktywna" in r for r in module.reasons_for(PPE_2))


def test_supply_segments_cut_at_buckets_and_merge_switches_sharing_one() -> None:
    start, end = _local("2026-09-14 05:00"), _local("2026-09-14 06:00")
    before = _transition(SUPPLY_VICTRON, "2026-08-20 12:00")
    there = _transition(SUPPLY_METER_2, "2026-09-14 05:21:00")
    back = _transition(SUPPLY_VICTRON, "2026-09-14 05:23:00")
    again = _transition(SUPPLY_METER_2, "2026-09-14 05:40:05")
    assert supply_segments([before, there, back, again], start, end) == [
        (start, _local("2026-09-14 05:40"), SUPPLY_VICTRON),
        (_local("2026-09-14 05:40"), end, SUPPLY_METER_2),
    ]
    # Before the log knows any state, the first part has none.
    assert supply_segments([there], start, end) == [
        (start, _local("2026-09-14 05:20"), None),
        (_local("2026-09-14 05:20"), end, SUPPLY_METER_2),
    ]
    # A switch exactly on the hour leaves nothing before it.
    on_the_hour = _transition(SUPPLY_METER_2, "2026-09-14 05:00:00")
    assert supply_segments([before, on_the_hour], start, end) == [(start, end, SUPPLY_METER_2)]


# ------------------------------------------------------- PPE 2 split archive
#
# The 5-minute statistics a split is read from are purged after ~10 days; the
# archive keeps each completed split hour's result, keyed by the windows read.

ARCHIVE_KEY = f"{DOMAIN}_entry1_billing_ppe2"


class _FakeStore:
    """``Store`` stand-in recording what was saved."""

    def __init__(self) -> None:
        self.saved: list[dict] = []

    async def async_save(self, data) -> None:
        self.saved.append(json.loads(json.dumps(data)))


def _switch_supply(to_meter_2: str, back: str) -> _Supply:
    """Meter 2 from ``to_meter_2`` to ``back`` (local times on 14.09)."""
    hours = {
        _local(f"2026-09-14 {to_meter_2[:2]}:00"): SUPPLY_HOUR_MIXED,
        _local(f"2026-09-14 {back[:2]}:00"): SUPPLY_HOUR_MIXED,
    }
    return _Supply(
        states=hours,
        transitions=[
            _transition(SUPPLY_VICTRON, "2026-08-20 12:00"),
            _transition(SUPPLY_METER_2, f"2026-09-14 {to_meter_2}"),
            _transition(SUPPLY_VICTRON, f"2026-09-14 {back}"),
        ],
    )


def _split_windows() -> dict:
    """The 5-minute energy of the switch in ``_switch_supply("07:12:30", "08:31:00")``."""
    return {
        (APT, _local("2026-09-14 07:10"), _local("2026-09-14 08:00")): 0.2,
        (APT, _local("2026-09-14 08:00"), _local("2026-09-14 08:30")): 0.15,
    }


async def test_a_split_hour_is_archived_and_reused_after_its_5_minute_statistics_are_gone() -> None:
    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture(windows=_split_windows())
        module = _module(meters, prices, _switch_supply("07:12:30", "08:31:00"))
        store = _FakeStore()
        module._archive_store = store
        await module.async_update()
        assert abs(module.periods[PPE_2].energy_kwh - 0.35) < 1e-9
        assert len(store.saved) == 1 and len(store.saved[0]["hours"]) == 2

        meters.windows = {}  # purged by the recorder
        await module.async_update()
    period = module.periods[PPE_2]
    assert period.complete
    assert abs(period.energy_kwh - 0.35) < 1e-9
    assert len(store.saved) == 1  # nothing changed, nothing saved

    restored = SplitArchive.from_dict(store.saved[0])
    window = (APT, _local("2026-09-14 07:10"), _local("2026-09-14 08:00"))
    assert restored.lookup(_local("2026-09-14 07:00"), [window]) == 0.2


async def test_changed_cuts_recompute_instead_of_reusing_the_archive() -> None:
    """A supply log rebuilt under another rule moves the switch: the old split
    must not answer for the new cuts."""
    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture(windows=_split_windows())
        module = _module(meters, prices, _switch_supply("07:12:30", "08:31:00"))
        await module.async_update()

        meters.windows = {}
        module._supply = _switch_supply("07:22:00", "08:31:00")
        await module.async_update()
        period = module.periods[PPE_2]
        assert any("14.09 07:00: brak odczytu" in m for m in period.missing)

        meters.windows = {(APT, _local("2026-09-14 07:20"), _local("2026-09-14 08:00")): 0.12}
        await module.async_update()
    assert abs(module.periods[PPE_2].energy_kwh - (0.12 + 0.15)) < 1e-9
    old = (APT, _local("2026-09-14 07:10"), _local("2026-09-14 08:00"))
    new = (APT, _local("2026-09-14 07:20"), _local("2026-09-14 08:00"))
    seven = _local("2026-09-14 07:00")
    assert module.archive.lookup(seven, [old]) is None
    assert module.archive.lookup(seven, [new]) == 0.12


async def test_pending_or_gap_splits_are_never_archived() -> None:
    # 09:00 ended three minutes ago: read, but still inside the latency window.
    with freeze_time("2026-09-14 10:03:00+02:00"):
        windows = {(APT, _local("2026-09-14 09:00"), _local("2026-09-14 09:40")): 0.1}
        meters, prices, _, _ = _ppe2_fixture(windows=windows, apt_partial=None)
        module = _module(meters, prices, _switch_supply("08:42:00", "09:40:00"))
        await module.async_update()
        nine = _local("2026-09-14 09:00")
        period = module.periods[PPE_2]
        assert abs(period.energy_kwh - 0.1) < 1e-9  # 09:00 counted…
        assert module.archive.lookup(nine, list(windows)) is None  # …not archived
        # 08:00: its window 08:40–09:00 has no bucket — a gap, not archived.
        assert period.missing == (f"14.09 08:00: brak odczytu {APT}",)
        assert len(module.archive) == 0

    # A window of the hour that just ended still on its way: pending, not archived.
    with freeze_time("2026-09-14 10:03:00+02:00"):
        meters, prices, _, _ = _ppe2_fixture(apt_partial=None)
        module = _module(meters, prices, _switch_supply("09:56:00", "10:01:00"))
        await module.async_update()
        assert any("09:00: minuty od 09:55 w drodze" in p for p in module.periods[PPE_2].pending)
        assert len(module.archive) == 0


async def test_saving_prunes_hours_before_the_previous_month() -> None:
    window = lambda h: [(APT, h + timedelta(minutes=10), h + timedelta(minutes=20))]  # noqa: E731
    july, august = _local("2026-07-31 23:00"), _local("2026-08-01 00:00")
    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture()
        module = _module(meters, prices, _Supply())
        module.archive.record(july, window(july), 0.1)
        module.archive.record(august, window(august), 0.2)
        store = _FakeStore()
        module._archive_store = store
        await module.async_update()
    (saved,) = store.saved
    assert [item["hour"] for item in saved["hours"]] == [dt_util.as_utc(august).isoformat()]


async def test_an_undecodable_archive_is_moved_aside_and_never_overwritten(
    hass, hass_storage, tmp_path: Path, monkeypatch, caplog
) -> None:
    """Data rule: never start empty after a failed read."""
    monkeypatch.setattr(hass.config, "config_dir", str(tmp_path))
    bad = {"hours": [{"hour": "not a time", "windows": [], "kwh": 1}]}
    hass_storage[ARCHIVE_KEY] = {"version": 1, "minor_version": 1, "key": ARCHIVE_KEY, "data": bad}
    storage_dir = tmp_path / ".storage"
    storage_dir.mkdir()
    (storage_dir / ARCHIVE_KEY).write_text(json.dumps(hass_storage[ARCHIVE_KEY]), encoding="utf-8")

    meters, prices, _, _ = _ppe2_fixture(windows=_split_windows())
    module = BillingModule(
        hass, _StubCoordinator(_config()), meters, prices, _switch_supply("07:12:30", "08:31:00")
    )
    with caplog.at_level(logging.WARNING):
        await module.async_setup()

    assert not (storage_dir / ARCHIVE_KEY).exists()
    (aside,) = storage_dir.glob(f"{ARCHIVE_KEY}.corrupt-*")
    assert json.loads(aside.read_text(encoding="utf-8"))["data"] == bad
    assert any("corrupt" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert module.archive is None and module.archive_error
    assert any("archiwum" in m for m in module.coordinator.messages)

    with freeze_time(NOW):
        meters, prices, _, _ = _ppe2_fixture(windows=_split_windows())
        module._meters, module._prices = meters, prices
        await module.async_update()
    # PPE 2 still works from the 5-minute statistics and says what it lacks…
    assert abs(module.periods[PPE_2].energy_kwh - 0.35) < 1e-9
    assert any("archiwum" in r for r in module.reasons_for(PPE_2))
    assert module.reasons_for(PPE_1) == []
    # …and nothing was written over the bad file.
    assert hass_storage[ARCHIVE_KEY]["data"] == bad


def test_archive_decoding_refuses_anything_unusable() -> None:
    hour = dt_util.as_utc(_local("2026-09-14 07:00"))
    good = {"hour": hour.isoformat(), "windows": [[APT, hour.isoformat(), (hour + timedelta(minutes=5)).isoformat()]], "kwh": 0.1}
    assert len(SplitArchive.from_dict({"hours": [good]})) == 1
    for broken in (
        {**good, "kwh": "x"},
        {**good, "windows": []},
        {**good, "hour": "2026-09-14T07:00:00"},  # no zone
        {**good, "windows": [[APT, hour.isoformat(), (hour + timedelta(hours=2)).isoformat()]]},
    ):
        with pytest.raises(ValueError):
            SplitArchive.from_dict({"hours": [broken]})
    with pytest.raises(ValueError):
        SplitArchive.from_dict({"hours": [good, good]})
    with pytest.raises(ValueError):
        SplitArchive.from_dict([])


# ------------------------------------------------------------- slot prices


def test_contribute_prices_every_slot_under_its_months_edition() -> None:
    meters, prices, _, _ = _fixture()
    module = _module(meters, prices, **{CONF_TARCZA_TERMS: [TERMS_2027]})
    december = dt_util.as_utc(datetime(2026, 12, 31, 23, tzinfo=dt_util.get_default_time_zone()))
    forecast = Forecast(slots=[HourSlot(start=december), HourSlot(start=december + timedelta(hours=1))])
    module.contribute(forecast)
    uncapped, capped = forecast.slots
    # December: no edition → its energy stays at the hourly price.
    assert uncapped.cap_price_kwh is None
    # January 2027: energy only (no handling fee), grossed up.
    assert abs(capped.cap_price_kwh - 0.40 * 1.23) < 1e-9


def test_contribute_counts_the_handling_fee_where_the_edition_does() -> None:
    meters, prices, _, _ = _fixture()
    module = _module(meters, prices)
    hour = dt_util.as_utc(datetime(2026, 9, 14, 12, tzinfo=dt_util.get_default_time_zone()))
    forecast = Forecast(slots=[HourSlot(start=hour)])
    module.contribute(forecast)
    assert abs(forecast.slots[0].cap_price_kwh - (0.40 + 0.08) * 1.23) < 1e-9


def test_contribute_leaves_slots_alone_while_the_terms_are_unusable() -> None:
    meters, prices, _, _ = _fixture()
    broken = {**TERMS_2026, "limit_kwh": 0}
    module = _module(meters, prices, **{CONF_TARCZA_TERMS: [broken]})
    hour = dt_util.as_utc(datetime(2026, 9, 14, 12, tzinfo=dt_util.get_default_time_zone()))
    forecast = Forecast(slots=[HourSlot(start=hour)])
    module.contribute(forecast)
    assert forecast.slots[0].cap_price_kwh is None


# --------------------------------------------------------------- the terms


def test_terms_price_and_limit_follow_their_edition() -> None:
    t2026 = PriceCapTerms.from_dict(TERMS_2026)
    t2027 = PriceCapTerms.from_dict(TERMS_2027)
    assert abs(t2026.cap_price(0.40, 0.08, 0.23) - 0.48 * 1.23) < 1e-12
    assert abs(t2027.cap_price(0.40, 0.08, 0.23) - 0.40 * 1.23) < 1e-12
    assert t2026.gross_limit(0.23) == 0.61
    assert abs(t2027.gross_limit(0.23) - 0.615) < 1e-12
    assert PriceCapTerms.from_dict(t2027.to_dict()) == t2027


def test_terms_must_span_whole_months_in_order_with_a_positive_limit() -> None:
    def problems(**changes):
        return PriceCapTerms.from_dict({**TERMS_2026, **changes}).problems()

    assert problems() == []
    assert CAP_TERMS_WHOLE_MONTHS in problems(valid_from="2025-01-02")
    assert CAP_TERMS_WHOLE_MONTHS in problems(valid_to="2026-12-30")
    assert problems(valid_to="2024-02-29") == [CAP_TERMS_DATES_ORDER]
    assert problems(limit_kwh=0) == [CAP_TERMS_LIMIT]


def test_overlapping_terms_are_reported_and_days_pick_their_edition() -> None:
    t2026 = PriceCapTerms.from_dict(TERMS_2026)
    t2027 = PriceCapTerms.from_dict(TERMS_2027)
    assert price_cap_problems([t2026, t2027]) == []
    early = PriceCapTerms.from_dict({**TERMS_2027, "valid_from": "2026-12-01"})
    assert price_cap_problems([t2026, early]) == [CAP_TERMS_OVERLAP]
    assert price_cap_terms_for_day([t2026, t2027], datetime(2026, 12, 31).date()) is t2026
    assert price_cap_terms_for_day([t2026, t2027], datetime(2027, 1, 1).date()) is t2027
    assert price_cap_terms_for_day([t2026, t2027], datetime(2028, 1, 1).date()) is None


def test_terms_without_dates_or_limit_do_not_decode() -> None:
    for missing in ("valid_from", "valid_to", "limit_kwh"):
        data = {k: v for k, v in TERMS_2026.items() if k != missing}
        with pytest.raises(ValueError):
            PriceCapTerms.from_dict(data)
