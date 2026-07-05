"""Google Maps travel resolver.

Turns a calendar route into one-way driving distance (km) and duration
(minutes), via the Google Distance Matrix API. Resolved routes are cached in a
persistent ``Store`` — destinations repeat (the same office, school, gym), so
each unique origin→destination pair costs one API call per
:data:`~.const.TRAVEL_CACHE_DAYS`.

There is deliberately no fallback: without an API key, or when the API cannot
resolve a location, ``async_resolve`` returns ``None`` and the caller must
treat the trip as distance-less (no drive-energy model) instead of guessing.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .const import DOMAIN, STORAGE_VERSION_TRAVEL, TRAVEL_CACHE_DAYS

_LOGGER = logging.getLogger(__name__)

_API_URL = "https://maps.googleapis.com/maps/api/distancematrix/json"
_API_TIMEOUT_S = 15
_HOME_CACHE_KEY = "__home__"


@dataclass(frozen=True)
class TravelInfo:
    """One-way driving distance and duration home → location."""

    distance_km: float
    duration_min: float


def parse_distance_matrix(payload: dict) -> TravelInfo | None:
    """Extract the single origin→destination element from an API response.

    Returns ``None`` (never a guess) when the API did not resolve the route —
    unknown address, zero results, quota errors and so on.
    """
    if not isinstance(payload, dict) or payload.get("status") != "OK":
        return None
    rows = payload.get("rows") or []
    elements = (rows[0].get("elements") or []) if rows else []
    element = elements[0] if elements else {}
    if element.get("status") != "OK":
        return None
    try:
        meters = float(element["distance"]["value"])
        seconds = float(element["duration"]["value"])
    except (KeyError, TypeError, ValueError):
        return None
    return TravelInfo(distance_km=meters / 1000.0, duration_min=seconds / 60.0)


def _normalise(location: str) -> str:
    """Cache key for a location string (case/whitespace-insensitive)."""
    return " ".join(location.split()).lower()


class TravelResolver:
    """Resolves and caches driving distance/duration between calendar locations."""

    def __init__(self, hass: HomeAssistant, entry_id: str, api_key: str) -> None:
        self.hass = hass
        self._api_key = api_key
        self._store: Store = Store(
            hass, STORAGE_VERSION_TRAVEL, f"{DOMAIN}_{entry_id}_travel"
        )
        # {normalised route: {"km": float, "min": float, "resolved_at": iso}}
        self._cache: dict[str, dict] = {}
        self._loaded = False
        # Locations that failed to resolve this runtime — retried only after a
        # restart, so a broken key/address doesn't hammer the API every cycle.
        self._failed: set[str] = set()

    async def async_setup(self) -> None:
        stored = await self._store.async_load()
        self._cache = dict(stored or {})
        self._loaded = True

    async def async_clear_data(self) -> None:
        """Wipe the persisted travel cache (configuration untouched)."""
        self._cache = {}
        self._failed = set()
        await self._store.async_remove()

    def _cached(self, key: str) -> TravelInfo | None:
        entry = self._cache.get(key)
        if not entry:
            return None
        resolved_at = dt_util.parse_datetime(str(entry.get("resolved_at") or ""))
        if (
            resolved_at is None
            or dt_util.now() - resolved_at > timedelta(days=TRAVEL_CACHE_DAYS)
        ):
            return None
        try:
            return TravelInfo(
                distance_km=float(entry["km"]), duration_min=float(entry["min"])
            )
        except (KeyError, TypeError, ValueError):
            return None

    async def async_resolve(self, location: str) -> TravelInfo | None:
        """One-way distance/duration home → ``location`` (cached), or ``None``."""
        return await self.async_resolve_route(None, location)

    async def async_resolve_route(
        self, origin: str | None, destination: str | None
    ) -> TravelInfo | None:
        """One-way distance/duration ``origin`` → ``destination``.

        ``None`` means the Home Assistant home coordinates on either side.
        Returns ``None`` when the route cannot be resolved; callers must not
        guess.
        """
        origin_key = _normalise(origin) if origin else _HOME_CACHE_KEY
        destination_key = _normalise(destination) if destination else _HOME_CACHE_KEY
        if not destination_key:
            return None
        if origin_key == destination_key:
            return TravelInfo(distance_km=0.0, duration_min=0.0)
        key = f"{origin_key}->{destination_key}"
        if not key:
            return None
        if not self._loaded:
            await self.async_setup()
        cached = self._cached(key)
        if cached is not None:
            return cached
        if key in self._failed:
            return None

        origin_value = (
            f"{self.hass.config.latitude},{self.hass.config.longitude}"
            if origin is None
            else origin
        )
        destination_value = (
            f"{self.hass.config.latitude},{self.hass.config.longitude}"
            if destination is None
            else destination
        )
        session = async_get_clientsession(self.hass)
        try:
            async with session.get(
                _API_URL,
                params={
                    "origins": origin_value,
                    "destinations": destination_value,
                    "mode": "driving",
                    "units": "metric",
                    "key": self._api_key,
                },
                timeout=_API_TIMEOUT_S,
            ) as response:
                payload = await response.json()
        except Exception as err:  # noqa: BLE001 - network/API failure → no travel data
            self._failed.add(key)
            _LOGGER.warning(
                "Google Maps: nie udało się wyznaczyć trasy %r → %r: %s",
                origin_value,
                destination_value,
                err,
            )
            return None

        info = parse_distance_matrix(payload)
        if info is None:
            self._failed.add(key)
            _LOGGER.warning(
                "Google Maps nie rozpoznał trasy %r → %r (status=%s) — "
                "wyjazd bez modelu dystansu/czasu dojazdu.",
                origin_value,
                destination_value,
                (payload or {}).get("status"),
            )
            return None

        self._cache[key] = {
            "km": round(info.distance_km, 2),
            "min": round(info.duration_min, 1),
            "resolved_at": dt_util.now().isoformat(),
        }
        await self._store.async_save(self._cache)
        return info
