"""Unit tests for the Pradcast price source."""

from __future__ import annotations

from datetime import date, timedelta
from types import MethodType

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import CONF_PRADCAST_API_KEY
from custom_components.powerpilot.modules.price_sources import (
    PRADCAST_HORIZON_DAYS,
    PradcastPriceSource,
)


def _payload(day: date, horizon: str | None, price: float) -> dict:
    return {
        "date": day.isoformat(),
        "source": "forecast_model" if horizon else "tge_fixing1",
        "horizon": horizon,
        "prices": [{"hour": hour, "price_kwh": price} for hour in range(24)],
    }


async def test_fetch_asks_one_day_endpoint_per_horizon_day(monkeypatch) -> None:
    """Today + D+1..D+3, one ``/prices/date/{date}`` call each."""
    today = dt_util.now().date()
    source = PradcastPriceSource(None, {CONF_PRADCAST_API_KEY: "token"})
    monkeypatch.setattr(
        "custom_components.powerpilot.modules.price_sources.async_get_clientsession",
        lambda hass: object(),
    )
    asked: list[date] = []

    async def _fake_fetch_day(self, session, api_key, day):
        asked.append(day)
        # Confirmed for today + tomorrow, model forecast further out.
        offset = (day - today).days
        return _payload(day, None if offset <= 1 else f"D+{offset}", 0.5)

    source._fetch_day = MethodType(_fake_fetch_day, source)

    data = await source.async_fetch()

    assert asked == [today + timedelta(days=n) for n in range(PRADCAST_HORIZON_DAYS + 1)]
    assert len(data.buy) == 24 * (PRADCAST_HORIZON_DAYS + 1)
    # Confirmed exactly where the payload carried no forecast horizon.
    assert len(data.confirmed_hours) == 48
    noon_tomorrow = dt_util.start_of_local_day(today + timedelta(days=1)) + timedelta(hours=12)
    noon_d2 = dt_util.start_of_local_day(today + timedelta(days=2)) + timedelta(hours=12)
    assert noon_tomorrow in data.confirmed_hours
    assert noon_d2 in data.buy
    assert noon_d2 not in data.confirmed_hours


async def test_day_that_fails_contributes_no_prices(monkeypatch) -> None:
    """A day the API cannot serve stays empty — never filled from another date."""
    today = dt_util.now().date()
    source = PradcastPriceSource(None, {CONF_PRADCAST_API_KEY: "token"})
    monkeypatch.setattr(
        "custom_components.powerpilot.modules.price_sources.async_get_clientsession",
        lambda hass: object(),
    )

    async def _fake_fetch_day(self, session, api_key, day):
        if day == today + timedelta(days=1):
            return None
        return _payload(day, None, 0.5)

    source._fetch_day = MethodType(_fake_fetch_day, source)

    data = await source.async_fetch()

    tomorrow = dt_util.start_of_local_day(today + timedelta(days=1))
    assert all(tomorrow + timedelta(hours=h) not in data.buy for h in range(24))
    assert len(data.buy) == 24 * PRADCAST_HORIZON_DAYS


async def test_retail_conversion_applies_markup_excise_and_vat(monkeypatch) -> None:
    """The stored buy price is the gross retail price, TGE stays net."""
    today = dt_util.now().date()
    source = PradcastPriceSource(
        None,
        {
            CONF_PRADCAST_API_KEY: "token",
            "price_markup": 0.1,
            "excise_kwh": 0.005,
            "price_vat": 1.23,
        },
    )
    monkeypatch.setattr(
        "custom_components.powerpilot.modules.price_sources.async_get_clientsession",
        lambda hass: object(),
    )

    async def _fake_fetch_day(self, session, api_key, day):
        return _payload(day, None, 0.4) if day == today else None

    source._fetch_day = MethodType(_fake_fetch_day, source)

    data = await source.async_fetch()

    hour = dt_util.start_of_local_day(today)
    assert round(data.buy[hour], 6) == round((0.4 + 0.1 + 0.005) * 1.23, 6)
    assert data.tge[hour] == 0.4
