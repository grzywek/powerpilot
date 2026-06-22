"""Pluggable price sources.

A price source produces hourly buy/sell prices (PLN/kWh) plus the set of hours
whose prices are *confirmed* (binding RDN) rather than forecast.

Two sources ship today:

* :class:`SensorPriceSource` — reads HA price sensors and their hourly forecast
  attributes (Nordpool/Tibber-style).
* :class:`PradcastPriceSource` — pulls confirmed RDN + D+1..D+3 forecasts from the
  prądcast.pl API (https://api.pradcast.pl). Wholesale prices can be converted to
  the retail price actually paid via an additive markup and a VAT multiplier.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_BUY_PRICE_SENSOR,
    CONF_PRADCAST_API_KEY,
    CONF_PRICE_MARKUP,
    CONF_PRICE_VAT,
)

_LOGGER = logging.getLogger(__name__)

PRADCAST_BASE = "https://api.pradcast.pl"
PRADCAST_HORIZON_DAYS = 3  # today + D+1..D+3

_FORECAST_ATTRS = ("raw_today", "raw_tomorrow", "forecast", "prices", "today", "tomorrow")
_START_KEYS = ("start", "hour", "from", "datetime", "time")
_VALUE_KEYS = ("value", "price", "total", "cost")


@dataclass
class PriceData:
    """Result of a price-source fetch."""

    buy: dict[datetime, float] = field(default_factory=dict)
    confirmed_hours: set[datetime] = field(default_factory=set)
    levels: dict[datetime, str] = field(default_factory=dict)
    confidence: dict[datetime, str] = field(default_factory=dict)


class PriceSource:
    """Base class for price sources."""

    def __init__(self, hass: HomeAssistant, config: dict[str, Any]) -> None:
        self.hass = hass
        self.config = config

    async def async_fetch(self) -> PriceData:
        raise NotImplementedError


def _parse_entries(attr_value: Any) -> dict[datetime, float]:
    out: dict[datetime, float] = {}
    if not isinstance(attr_value, (list, tuple)):
        return out
    for entry in attr_value:
        if not isinstance(entry, dict):
            continue
        start = None
        for key in _START_KEYS:
            if key in entry:
                start = dt_util.parse_datetime(str(entry[key]))
                break
        value = None
        for key in _VALUE_KEYS:
            if key in entry and entry[key] is not None:
                try:
                    value = float(entry[key])
                except (TypeError, ValueError):
                    value = None
                break
        if start is not None and value is not None:
            hour = dt_util.as_local(start).replace(minute=0, second=0, microsecond=0)
            out[hour] = value
    return out


class SensorPriceSource(PriceSource):
    """Reads buy/sell prices from Home Assistant sensors."""

    async def async_fetch(self) -> PriceData:
        data = PriceData()
        data.buy = self._read_sensor(self.config.get(CONF_BUY_PRICE_SENSOR))
        # Treat today + tomorrow as confirmed.
        confirmed_until = (dt_util.now() + timedelta(days=1)).replace(
            hour=23, minute=0, second=0, microsecond=0
        )
        data.confirmed_hours = {h for h in data.buy if h <= confirmed_until}
        return data

    def _read_sensor(self, entity_id: str | None) -> dict[datetime, float]:
        result: dict[datetime, float] = {}
        if not entity_id:
            return result
        state = self.hass.states.get(entity_id)
        if state is None:
            return result
        try:
            hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
            result[hour] = float(state.state)
        except (TypeError, ValueError):
            pass
        for attr in _FORECAST_ATTRS:
            if attr in state.attributes:
                result.update(_parse_entries(state.attributes[attr]))
        return result


class PradcastPriceSource(PriceSource):
    """Fetches RDN + forecast prices from the prądcast.pl API."""

    async def async_fetch(self) -> PriceData:
        today = dt_util.now().date()
        days = [today + timedelta(days=offset) for offset in range(PRADCAST_HORIZON_DAYS + 1)]
        return await self.async_fetch_days(days)

    async def async_fetch_days(self, days: list[date]) -> PriceData:
        """Fetch an explicit list of days (used for the forward horizon and backfill)."""
        data = PriceData()
        api_key = self.config.get(CONF_PRADCAST_API_KEY)
        if not api_key:
            _LOGGER.warning("Pradcast price source selected but no API key configured")
            return data

        markup = float(self.config.get(CONF_PRICE_MARKUP, 0.0) or 0.0)
        vat = float(self.config.get(CONF_PRICE_VAT, 1.0) or 1.0)
        session = async_get_clientsession(self.hass)

        results = await asyncio.gather(
            *(self._fetch_day(session, api_key, day) for day in days),
            return_exceptions=True,
        )
        for day, payload in zip(days, results):
            if isinstance(payload, Exception) or not payload:
                continue
            self._merge_day(data, day, payload, markup, vat)
        return data

    async def _fetch_day(
        self, session: aiohttp.ClientSession, api_key: str, day: date
    ) -> dict | None:
        return await self._get_json(session, api_key, f"{PRADCAST_BASE}/prices/date/{day.isoformat()}")

    async def _get_json(
        self, session: aiohttp.ClientSession, api_key: str, url: str
    ) -> dict | None:
        try:
            async with asyncio.timeout(15):
                async with session.get(url, headers={"X-API-Key": api_key}) as resp:
                    if resp.status == 429:
                        _LOGGER.warning("Pradcast rate-limited (429): %s", url)
                        return None
                    if resp.status != 200:
                        _LOGGER.debug("Pradcast %s returned HTTP %s", url, resp.status)
                        return None
                    return await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as err:
            _LOGGER.debug("Pradcast fetch failed for %s: %s", url, err)
            return None

    async def async_fetch_forecasts(self, target_date: date) -> dict:
        """Return horizon-indexed forecasts for a date (for the D+1..D+3 overlay).

        Shape: ``{"D+1": [{"hour", "buy", "p10", "p90"}], "D+2": [...], ...}``
        with retail conversion applied to price/p10/p90.
        """
        api_key = self.config.get(CONF_PRADCAST_API_KEY)
        if not api_key:
            return {}
        markup = float(self.config.get(CONF_PRICE_MARKUP, 0.0) or 0.0)
        vat = float(self.config.get(CONF_PRICE_VAT, 1.0) or 1.0)
        session = async_get_clientsession(self.hass)
        payload = await self._get_json(
            session, api_key, f"{PRADCAST_BASE}/prices/forecasts/{target_date.isoformat()}"
        )
        if not payload:
            return {}

        def _retail(value):
            return (float(value) + markup) * vat if value is not None else None

        out: dict[str, list[dict]] = {}
        for horizon, block in (payload.get("forecasts") or {}).items():
            series = []
            for entry in block.get("prices", []) or []:
                if entry.get("price_kwh") is None:
                    continue
                series.append(
                    {
                        "hour": int(entry["hour"]),
                        "buy": _retail(entry["price_kwh"]),
                        "p10": _retail(entry.get("p10")),
                        "p90": _retail(entry.get("p90")),
                    }
                )
            if series:
                out[horizon] = series
        return out

    @staticmethod
    def _merge_day(
        data: PriceData, day: date, payload: dict, markup: float, vat: float
    ) -> None:
        # ``horizon`` is set (D+1/D+2/D+3) only for forecast days; null = confirmed.
        confirmed = not payload.get("horizon")
        confidence = payload.get("confidence")
        for entry in payload.get("prices", []) or []:
            hour_index = entry.get("hour")
            price_kwh = entry.get("price_kwh")
            if hour_index is None or price_kwh is None:
                continue
            start = dt_util.start_of_local_day(day) + timedelta(hours=int(hour_index))
            retail = (float(price_kwh) + markup) * vat
            data.buy[start] = retail
            if confirmed:
                data.confirmed_hours.add(start)
            level = entry.get("level")
            if level:
                data.levels[start] = level
            if confidence:
                data.confidence[start] = confidence
