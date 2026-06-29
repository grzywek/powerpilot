"""Unit tests for the Pradcast price source."""

from __future__ import annotations

from datetime import date, timedelta
from types import MethodType

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import CONF_PRADCAST_API_KEY
from custom_components.powerpilot.modules.price_sources import (
    PRADCAST_HORIZON_DAYS,
    PradcastPriceSource,
    PriceData,
)


async def test_forward_forecast_merge_ignores_horizons_beyond_api_price_window() -> None:
    """Only D+1..D+3 may be treated as Pradcast price data."""
    today = date(2026, 6, 29)
    data = PriceData()
    source = PradcastPriceSource(None, {})

    async def _fake_fetch_forecasts(self, target_date):
        return {
            "D+1": [{"hour": 12, "buy": 1.1, "tge": 0.9}],
            f"D+{PRADCAST_HORIZON_DAYS}": [{"hour": 13, "buy": 1.3, "tge": 1.0}],
            f"D+{PRADCAST_HORIZON_DAYS + 1}": [
                {"hour": 14, "buy": 1.4, "tge": 1.1}
            ],
        }

    source.async_fetch_forecasts = MethodType(_fake_fetch_forecasts, source)

    await source._merge_forward_forecasts(data, today)

    d1 = dt_util.start_of_local_day(today + timedelta(days=1)) + timedelta(hours=12)
    d3 = (
        dt_util.start_of_local_day(today + timedelta(days=PRADCAST_HORIZON_DAYS))
        + timedelta(hours=13)
    )
    d4 = (
        dt_util.start_of_local_day(today + timedelta(days=PRADCAST_HORIZON_DAYS + 1))
        + timedelta(hours=14)
    )

    assert data.buy[d1] == 1.1
    assert data.buy[d3] == 1.3
    assert d4 not in data.buy


async def test_forecast_endpoint_ignores_horizons_beyond_api_price_window(
    monkeypatch,
) -> None:
    """The overlay payload must stay limited to Pradcast's supported D+1..D+3."""
    today = date(2026, 6, 29)
    source = PradcastPriceSource(None, {CONF_PRADCAST_API_KEY: "token"})

    monkeypatch.setattr(
        "custom_components.powerpilot.modules.price_sources.async_get_clientsession",
        lambda hass: object(),
    )

    async def _fake_get_json(self, session, api_key, url):
        return {
            "forecasts": {
                "D+1": {
                    "prices": [
                        {"hour": 0, "price_kwh": 1.0, "p10": 0.8, "p90": 1.2}
                    ]
                },
                f"D+{PRADCAST_HORIZON_DAYS + 1}": {
                    "prices": [
                        {"hour": 1, "price_kwh": 2.0, "p10": 1.8, "p90": 2.2}
                    ]
                },
            }
        }

    source._get_json = MethodType(_fake_get_json, source)

    horizons = await source.async_fetch_forecasts(today)

    assert list(horizons) == ["D+1"]
    assert horizons["D+1"][0]["buy"] == 1.0
