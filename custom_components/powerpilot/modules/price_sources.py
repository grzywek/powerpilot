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
    CONF_EXCISE_KWH,
    CONF_PRADCAST_API_KEY,
    CONF_PRICE_MARKUP,
    CONF_PRICE_VAT,
)

_LOGGER = logging.getLogger(__name__)

PRADCAST_BASE = "https://api.pradcast.pl"
PRADCAST_HORIZON_DAYS = 3  # today + D+1..D+3
# Cap concurrent day fetches so the one-time year-long backfill doesn't storm the
# API (and trip rate limits). Normal forward fetches are only a few days.
PRADCAST_MAX_CONCURRENCY = 6

_FORECAST_ATTRS = ("raw_today", "raw_tomorrow", "forecast", "prices", "today", "tomorrow")
_START_KEYS = ("start", "hour", "from", "datetime", "time")
_VALUE_KEYS = ("value", "price", "total", "cost")


@dataclass
class PriceData:
    """Result of a price-source fetch.

    ``buy`` is the gross energy-side price (PLN/kWh) the optimizer uses.
    ``tge`` holds the raw net wholesale price per hour for the sources that
    expose it (Pradcast) so the display can break the bill down into TGE +
    marża + akcyza + VAT; sensor sources leave it empty.
    """

    buy: dict[datetime, float] = field(default_factory=dict)
    tge: dict[datetime, float] = field(default_factory=dict)
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
        data = await self.async_fetch_days(days)
        # Backfill forward horizon from /prices/forecasts/{today} for any day
        # whose per-day endpoint returned nothing (Pradcast typically only
        # publishes RDN for today+tomorrow as confirmed; D+2/D+3 only live
        # under the multi-horizon forecast endpoint).
        await self._merge_forward_forecasts(data, today)
        return data

    async def _merge_forward_forecasts(self, data: PriceData, today: date) -> None:
        """Fill D+1..D+3 from the multi-horizon forecast endpoint as a backstop."""
        try:
            horizons = await self.async_fetch_forecasts(today)
        except Exception as err:  # noqa: BLE001
            _LOGGER.debug("Pradcast forecast backfill failed: %s", err)
            return
        if not horizons:
            return
        for horizon_label, entries in horizons.items():
            # "D+1" -> today + 1 day, etc.
            try:
                offset = int(horizon_label.replace("D+", ""))
            except (ValueError, AttributeError):
                continue
            day = today + timedelta(days=offset)
            for entry in entries:
                hour_idx = entry.get("hour")
                buy = entry.get("buy")
                if hour_idx is None or buy is None:
                    continue
                start = dt_util.start_of_local_day(day) + timedelta(hours=int(hour_idx))
                # Don't overwrite confirmed/already-fetched hours.
                if start in data.buy:
                    continue
                data.buy[start] = float(buy)
                tge = entry.get("tge")
                if tge is not None:
                    data.tge[start] = float(tge)

    async def async_fetch_days(self, days: list[date]) -> PriceData:
        """Fetch an explicit list of days (used for the forward horizon and backfill)."""
        data = PriceData()
        api_key = self.config.get(CONF_PRADCAST_API_KEY)
        if not api_key:
            _LOGGER.warning("Pradcast price source selected but no API key configured")
            return data

        markup = float(self.config.get(CONF_PRICE_MARKUP, 0.0) or 0.0)
        vat = float(self.config.get(CONF_PRICE_VAT, 1.0) or 1.0)
        excise = float(self.config.get(CONF_EXCISE_KWH, 0.0) or 0.0)
        session = async_get_clientsession(self.hass)

        sem = asyncio.Semaphore(PRADCAST_MAX_CONCURRENCY)

        async def _throttled(day: date) -> dict | None:
            async with sem:
                return await self._fetch_day(session, api_key, day)

        results = await asyncio.gather(
            *(_throttled(day) for day in days),
            return_exceptions=True,
        )
        for day, payload in zip(days, results):
            if isinstance(payload, Exception) or not payload:
                continue
            self._merge_day(data, day, payload, markup, vat, excise)
        return data

    async def _fetch_day(
        self, session: aiohttp.ClientSession, api_key: str, day: date
    ) -> dict | None:
        return await self._get_json(session, api_key, f"{PRADCAST_BASE}/prices/date/{day.isoformat()}")

    async def _get_json(
        self, session: aiohttp.ClientSession, api_key: str, url: str, retries: int = 3
    ) -> dict | None:
        for attempt in range(retries):
            try:
                async with asyncio.timeout(15):
                    async with session.get(url, headers={"X-API-Key": api_key}) as resp:
                        if resp.status == 429:
                            if attempt + 1 < retries:
                                await asyncio.sleep(1.5 * (attempt + 1))
                                continue
                            _LOGGER.warning(
                                "Pradcast rate-limited (429) po %d próbach: %s", retries, url
                            )
                            return None
                        if resp.status != 200:
                            _LOGGER.debug("Pradcast %s returned HTTP %s", url, resp.status)
                            return None
                        return await resp.json()
            except (aiohttp.ClientError, asyncio.TimeoutError) as err:
                _LOGGER.debug("Pradcast fetch failed for %s: %s", url, err)
                return None
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
        excise = float(self.config.get(CONF_EXCISE_KWH, 0.0) or 0.0)
        session = async_get_clientsession(self.hass)
        payload = await self._get_json(
            session, api_key, f"{PRADCAST_BASE}/prices/forecasts/{target_date.isoformat()}"
        )
        if not payload:
            return {}

        def _retail(value):
            return (float(value) + markup + excise) * vat if value is not None else None

        out: dict[str, list[dict]] = {}
        for horizon, block in (payload.get("forecasts") or {}).items():
            series = []
            for entry in block.get("prices", []) or []:
                if entry.get("price_kwh") is None:
                    continue
                series.append(
                    {
                        "hour": int(entry["hour"]),
                        "tge": float(entry["price_kwh"]),
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
        data: PriceData,
        day: date,
        payload: dict,
        markup: float,
        vat: float,
        excise: float = 0.0,
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
            retail = (float(price_kwh) + markup + excise) * vat
            data.buy[start] = retail
            data.tge[start] = float(price_kwh)
            if confirmed:
                data.confirmed_hours.add(start)
            level = entry.get("level")
            if level:
                data.levels[start] = level
            if confidence:
                data.confidence[start] = confidence
