"""Observed month-to-date settlement under the monthly price cap (Tarcza)."""

from __future__ import annotations

import zoneinfo
from datetime import datetime, timedelta

import pytest
from freezegun import freeze_time
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_PPE1_IMPORT_SENSORS,
    CONF_PRICE_VAT,
    CONF_TARCZA_TERMS,
    PPE_1,
)
from custom_components.powerpilot.models import (
    CAP_TERMS_DATES_ORDER,
    CAP_TERMS_LIMIT,
    CAP_TERMS_OVERLAP,
    CAP_TERMS_WHOLE_MONTHS,
    Forecast,
    HourSlot,
    PriceCapTerms,
    price_cap_problems,
    price_cap_terms_for_day,
)
from custom_components.powerpilot.modules.billing import (
    BillingModule,
    month_bounds,
    next_month_start,
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

    def log_info(self, domain, message, extra=None) -> None:
        self.messages.append(message)

    def log_warning(self, domain, message, extra=None) -> None:
        self.messages.append(message)


class _Meters:
    """Recorder stand-in: hourly kWh per entity + 5-minute sums per window.

    ``partial`` answers for the running hour; ``buckets`` for a completed
    hour read from its 5-minute statistics, keyed ``(entity_id, hour)``.
    """

    def __init__(
        self,
        hourly: dict[str, dict[datetime, float]],
        partial: dict,
        buckets: dict | None = None,
    ) -> None:
        self.hourly = hourly
        self.partial = partial
        self.buckets = buckets or {}

    async def async_range_kwh(self, entity_id, start, end):
        return {
            h: v for h, v in self.hourly.get(entity_id, {}).items() if start <= h < end
        }

    async def async_partial_kwh(self, entity_id, start, end):
        if end - start == timedelta(hours=1):
            return self.buckets.get((entity_id, start))
        return self.partial.get(entity_id)


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
