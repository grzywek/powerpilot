"""Cap prices under an edition's terms and the far-tail archive estimate."""

from __future__ import annotations

import zoneinfo
from datetime import date, timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_PRICE_MARKUP,
    CONF_PRICE_SOURCE,
    CONF_PRICE_VAT,
    ESTIMATE_WEEKLY_WEIGHTS,
    PRICE_SOURCE_PRADCAST,
    PRICE_TYPE_CERTAIN,
    PRICE_TYPE_FORECAST,
)
from custom_components.powerpilot.models import Forecast, HourSlot, PriceCapTerms
from custom_components.powerpilot.modules.prices import (
    PriceArchive,
    PriceModule,
    cap_price_of,
)


@pytest.fixture(autouse=True)
def _warsaw_time():
    original = dt_util.get_default_time_zone()
    dt_util.set_default_time_zone(zoneinfo.ZoneInfo("Europe/Warsaw"))
    yield
    dt_util.set_default_time_zone(original)


def _hour():
    return dt_util.utcnow().replace(minute=0, second=0, microsecond=0)


def _terms(fee: bool = True) -> PriceCapTerms:
    return PriceCapTerms(
        name="test",
        valid_from=date(2000, 1, 1),
        valid_to=date(2099, 12, 31),
        limit_kwh=0.61,
        handling_fee_included=fee,
    )


def _formula(tge: float, markup: float = 0.08, excise: float = 0.005, vat: float = 0.23):
    return {"tge": tge, "markup": markup, "excise": excise, "vat": vat, "rounding": "none"}


class _StubCoordinator:
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
        _StubCoordinator(
            {
                CONF_PRICE_SOURCE: PRICE_SOURCE_PRADCAST,
                CONF_PRICE_MARKUP: 0.08,
                CONF_PRICE_VAT: 1.23,
                **(config or {}),
            }
        ),
    )


# ----------------------------------------------------------------- cap_price_of


def test_cap_price_is_energy_plus_markup_with_vat_but_no_excise() -> None:
    entry = {"energy": 9.99, "formula": _formula(tge=0.40)}
    assert round(cap_price_of(entry, _terms()), 6) == round((0.40 + 0.08) * 1.23, 6)


def test_cap_price_without_the_handling_fee_when_the_edition_leaves_it_out() -> None:
    entry = {"energy": 9.99, "formula": _formula(tge=0.40)}
    assert round(cap_price_of(entry, _terms(fee=False)), 6) == round(0.40 * 1.23, 6)


def test_cap_price_unknown_without_formula() -> None:
    assert cap_price_of({"energy": 0.5}, _terms()) is None
    assert cap_price_of({"energy": 0.5, "formula": None}, _terms()) is None
    assert cap_price_of(None, _terms()) is None


# ----------------------------------------------------------- module resolution


def test_module_cap_price_prefers_archived_certain_formula() -> None:
    module = _module()
    h = _hour()
    module.archive.record(h, 1.0, PRICE_TYPE_CERTAIN, "pradcast", "t", formula=_formula(0.30))
    # A live re-fetch of the same hour with a different wholesale price must
    # not outrank the settled (certain) archive entry.
    module._data.buy[h] = 2.0
    module._data.tge[h] = 0.90
    assert round(module.cap_price_at(h, _terms()), 6) == round((0.30 + 0.08) * 1.23, 6)


def test_module_cap_price_from_live_fetch_uses_current_pricing_config() -> None:
    module = _module({CONF_PRICE_MARKUP: 0.10, CONF_PRICE_VAT: 1.08})
    h = _hour()
    module._data.buy[h] = 2.0
    module._data.tge[h] = 0.50
    assert round(module.cap_price_at(h, _terms()), 6) == round((0.50 + 0.10) * 1.08, 6)
    assert round(module.cap_price_at(h, _terms(fee=False)), 6) == round(0.50 * 1.08, 6)


def test_module_cap_price_none_for_sensor_source_hours() -> None:
    module = _module()
    h = _hour()
    module.archive.record(h, 1.0, PRICE_TYPE_CERTAIN, "sensor", "t")
    assert module.cap_price_at(h, _terms()) is None


def test_cap_price_for_a_priced_hour_comes_from_the_same_source() -> None:
    module = _module()
    h = _hour()
    module.archive.record(h, 0.60, PRICE_TYPE_FORECAST, "pradcast", "t", formula=_formula(0.40))
    forecast = Forecast(slots=[HourSlot(start=h)])
    module.contribute(forecast)
    assert forecast.slots[0].buy_price == 0.60
    # The price module leaves the cap price to the billing module's edition.
    assert forecast.slots[0].cap_price_kwh is None
    assert round(module.cap_price_for(h, _terms()), 6) == round((0.40 + 0.08) * 1.23, 6)


def test_cap_price_for_an_estimated_hour_uses_the_same_weekly_samples() -> None:
    module = _module()
    h = _hour() + timedelta(days=5)
    for weeks, tge in ((1, 0.30), (2, 0.40), (3, 0.50)):
        module.archive.record(
            h - timedelta(days=7 * weeks),
            1.0,
            PRICE_TYPE_CERTAIN,
            "pradcast",
            "t",
            formula=_formula(tge),
        )
    forecast = Forecast(slots=[HourSlot(start=h)])
    module.contribute(forecast)
    assert "price_estimated" in forecast.slots[0].tags
    w1, w2, w3 = ESTIMATE_WEEKLY_WEIGHTS
    expected = ((0.30 + 0.08) * w1 + (0.40 + 0.08) * w2 + (0.50 + 0.08) * w3) * 1.23
    assert round(module.cap_price_for(h, _terms()), 6) == round(expected, 6)


# ------------------------------------------------------------ far-tail estimate


def test_estimate_skips_weeks_beyond_the_archive_instead_of_counting_gaps() -> None:
    """An hour 20 days past the archive's reach: weeks 1–2 do not exist yet."""
    archive = PriceArchive()
    latest = _hour()
    archive.record(latest, 9.0, PRICE_TYPE_FORECAST, "pradcast", "t")
    query = latest + timedelta(days=20)
    # Weeks 1 and 2 before ``query`` lie after ``latest`` → skipped. Weeks 3, 4
    # and 5 are the three most recent archived same-weekday samples.
    archive.record(query - timedelta(days=21), 1.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.record(query - timedelta(days=28), 2.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.record(query - timedelta(days=35), 3.0, PRICE_TYPE_CERTAIN, "pradcast", "t")

    price, breakdown = archive.estimate(query)
    w1, w2, w3 = ESTIMATE_WEEKLY_WEIGHTS
    assert round(price, 6) == round(1.0 * w1 + 2.0 * w2 + 3.0 * w3, 6)
    assert [s["weeks_ago"] for s in breakdown] == [3, 4, 5]


def test_estimate_still_treats_past_gaps_as_gaps() -> None:
    """Inside the archive's reach a missing week renormalises, as before."""
    archive = PriceArchive()
    h = _hour()
    archive.record(h - timedelta(days=7), 1.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.record(h - timedelta(days=21), 3.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    archive.record(h - timedelta(days=28), 7.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    price, breakdown = archive.estimate(h)
    w1, _w2, w3 = ESTIMATE_WEEKLY_WEIGHTS
    assert round(price, 6) == round((1.0 * w1 + 3.0 * w3) / (w1 + w3), 6)
    assert [s["weeks_ago"] for s in breakdown] == [1, 2, 3]


def test_estimate_none_when_the_tail_outruns_the_archive() -> None:
    archive = PriceArchive()
    latest = _hour()
    archive.record(latest, 9.0, PRICE_TYPE_CERTAIN, "pradcast", "t")
    price, _ = archive.estimate(latest + timedelta(days=200))
    assert price is None
