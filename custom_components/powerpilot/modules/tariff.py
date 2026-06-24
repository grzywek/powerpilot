"""Distribution tariff module.

Resolves the distribution price (PLN/kWh) per hour using user-configured
:class:`Tariff` definitions. The pipeline rule is:

    *All configuration changes take effect from the **next** hour.*

This means:
  - ``slot[0]`` (the hour we are currently in) → use a **snapshot** of the
    distribution price; lazily created on first access from the current config.
  - ``slot[1..]`` (future hours) → **live**-evaluated from the current config.

Day-of-week classification:
  - For "today" (D+0): read ``period.day_sensor`` state directly from HA.
  - For future days (D+1..D+N): pre-fetch the ``workday.check_date`` service
    response in :meth:`async_update` for every (day_sensor, date) pair in the
    horizon, cache the booleans, and use the cache from :meth:`contribute`.
    Day sensors that don't support ``workday.check_date`` (e.g. custom helpers)
    fall back to *today's* state with a warning.

Pricing per hour:
  ``total_distribution = tariff.base_component_kwh + matching_period.price_kwh``

The user is responsible for adding a catch-all ``Pozaszczyt`` period
(``day_sensor=None``, hours 0-24) so that some period always matches. If none
matches, the price for that hour is ``None`` and a warning is logged.

Snapshots are persisted to a dedicated ``Store`` keyed by UTC-ISO hour
timestamp, pruned to the last 90 days on every load.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_TARIFFS,
    DOMAIN,
    STORAGE_VERSION_TARIFF_SNAPSHOTS,
)
from ..models import Forecast, Tariff, tariff_for_day
from .base import PowerPilotModule

# How many days of past snapshots to keep.
_SNAPSHOT_RETENTION_DAYS = 90

# How many future days to pre-fetch workday.check_date for (full 7-day horizon).
_FUTURE_DAYS_TO_PREFETCH = 7

_WORKDAY_DOMAIN = "workday"
_CHECK_DATE_SERVICE = "check_date"


class TariffModule(PowerPilotModule):
    """Provides hourly distribution prices to the forecast."""

    domain = "tariff"

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._store: Store | None = None
        # Snapshot dict: {utc_iso_hour: {"price": float, "tariff_id": str,
        #                                "period_id": str | None, "backfilled": bool}}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._tariffs: list[Tariff] = []
        # Pre-fetched day-sensor results: {(entity_id, date): bool}.
        # date == today is read live from hass.states.
        self._future_day_cache: dict[tuple[str, date], bool] = {}
        # Day sensors known to *not* support workday.check_date — we warn once
        # and fall back to today's state for them.
        self._unsupported_sensors: set[str] = set()

    # ------------------------------------------------------------------ setup

    async def async_setup(self) -> None:
        self._store = Store(
            self.hass,
            STORAGE_VERSION_TARIFF_SNAPSHOTS,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_tariff_snapshots",
        )
        stored = await self._store.async_load() or {}
        self._snapshots = self._prune(stored.get("snapshots") or {})
        self._reload_config()

    def _reload_config(self) -> None:
        raw_tariffs = self.config.get(CONF_TARIFFS) or []
        self._tariffs = [Tariff.from_dict(t) for t in raw_tariffs]

    # ------------------------------------------------------------------ update

    async def async_update(self) -> None:
        self._reload_config()

        today = dt_util.now().date()
        active = tariff_for_day(self._tariffs, today)
        if active is None:
            if self._tariffs:
                self.log_warning(
                    f"Brak aktywnej taryfy dla {today.isoformat()} "
                    f"(skonfigurowanych: {len(self._tariffs)}).",
                    extra={"configured_tariffs": len(self._tariffs)},
                )
            else:
                self.log_warning(
                    "Nie skonfigurowano żadnej taryfy dystrybucyjnej. "
                    "Optymalizator nie zna pełnej ceny — skonfiguruj taryfę "
                    "w Konfiguracji → PowerPilot → Konfiguruj → Taryfy.",
                    extra={"configured_tariffs": 0},
                )
            self._future_day_cache.clear()
            return

        await self._prefetch_future_days(today)

        self.log_info(
            f"Aktywna taryfa: {active.name} "
            f"(składnik bazowy {active.base_component_kwh:.4f} PLN/kWh, "
            f"{len(active.periods)} okresów). "
            f"Snapshot: {len(self._snapshots)} godzin. "
            f"Cache klasyfikacji dni: {len(self._future_day_cache)}.",
            extra={
                "tariff_name": active.name,
                "periods": len(active.periods),
                "base_component_kwh": active.base_component_kwh,
                "snapshot_hours": len(self._snapshots),
                "future_day_cache": len(self._future_day_cache),
            },
        )

    async def _prefetch_future_days(self, today: date) -> None:
        """Call ``workday.check_date`` for every (sensor, future_date) pair."""
        needed: set[tuple[str, date]] = set()
        for offset in range(1, _FUTURE_DAYS_TO_PREFETCH + 1):
            day = today + timedelta(days=offset)
            tariff = tariff_for_day(self._tariffs, day)
            if tariff is None:
                continue
            for period in tariff.periods:
                if period.day_sensor:
                    needed.add((period.day_sensor, day))

        # Drop stale cache entries.
        valid_dates = {today + timedelta(days=d) for d in range(1, _FUTURE_DAYS_TO_PREFETCH + 1)}
        self._future_day_cache = {
            k: v for k, v in self._future_day_cache.items() if k[1] in valid_dates
        }

        for entity_id, day in needed:
            if (entity_id, day) in self._future_day_cache:
                continue
            if entity_id in self._unsupported_sensors:
                continue
            try:
                response = await self.hass.services.async_call(
                    _WORKDAY_DOMAIN,
                    _CHECK_DATE_SERVICE,
                    {"check_date": day.isoformat()},
                    target={"entity_id": entity_id},
                    blocking=True,
                    return_response=True,
                )
            except HomeAssistantError as exc:
                self._unsupported_sensors.add(entity_id)
                self.log_warning(
                    f"Sensor {entity_id} nie wspiera workday.check_date "
                    f"({type(exc).__name__}: {exc}). Dla przyszłych dni użyję "
                    f"stanu z dziś — przekonfiguruj period jeśli to nie pasuje.",
                    extra={"day_sensor": entity_id, "error": str(exc)},
                )
                continue
            except Exception as exc:  # noqa: BLE001 — defensive: voluptuous etc.
                self._unsupported_sensors.add(entity_id)
                self.log_warning(
                    f"Błąd workday.check_date dla {entity_id}: "
                    f"{type(exc).__name__}: {exc}.",
                    extra={"day_sensor": entity_id, "error": str(exc)},
                )
                continue
            result = self._parse_check_date_response(response, entity_id)
            if result is None:
                continue
            self._future_day_cache[(entity_id, day)] = result

    @staticmethod
    def _parse_check_date_response(response: Any, entity_id: str) -> bool | None:
        """``workday.check_date`` returns ``{<entity_id>: {"workday": bool}}``."""
        if not isinstance(response, dict):
            return None
        per_entity = response.get(entity_id)
        if isinstance(per_entity, dict) and "workday" in per_entity:
            return bool(per_entity["workday"])
        if "workday" in response:
            return bool(response["workday"])
        return None

    # -------------------------------------------------------------- contribute

    def contribute(self, forecast: Forecast) -> None:
        if not forecast.slots or not self._tariffs:
            return

        now = dt_util.now()
        today = now.date()
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        snapshot_dirty = False

        for index, slot in enumerate(forecast.slots):
            slot_dt = slot.start
            if slot_dt.tzinfo is None:
                slot_dt = dt_util.as_local(slot_dt)
            day_offset = (slot_dt.date() - today).days
            is_current_or_past = slot_dt <= current_hour_start

            if is_current_or_past:
                key = self._snapshot_key(slot_dt)
                snap = self._snapshots.get(key)
                if snap is None:
                    price, tariff_id, period_id = self._resolve_price(slot_dt, day_offset)
                    if price is not None:
                        self._snapshots[key] = {
                            "price": price,
                            "tariff_id": tariff_id,
                            "period_id": period_id,
                            "backfilled": index > 0,
                        }
                        snapshot_dirty = True
                    slot.distribution_price_kwh = price
                else:
                    slot.distribution_price_kwh = snap["price"]
            else:
                price, _, _ = self._resolve_price(slot_dt, day_offset)
                slot.distribution_price_kwh = price

        if snapshot_dirty:
            self._schedule_save()

    # ------------------------------------------------------------- resolution

    def _resolve_price(
        self, slot_dt: datetime, day_offset: int
    ) -> tuple[float | None, str | None, str | None]:
        """Return ``(price, tariff_id, period_id)`` for the given hour.

        Price is gross: ``(base_component_kwh + period.price_kwh) * (1 + vat_rate)``.
        """
        tariff = tariff_for_day(self._tariffs, slot_dt.date())
        if tariff is None:
            return None, None, None

        vat_mul = 1.0 + tariff.vat_rate

        for period in tariff.periods:
            if not period.matches_hour(slot_dt.hour):
                continue
            if period.day_sensor is None:
                price = (tariff.base_component_kwh + period.price_kwh) * vat_mul
                return price, tariff.id, period.id
            if not self._is_day_sensor_on(period.day_sensor, slot_dt.date(), day_offset):
                continue
            price = (tariff.base_component_kwh + period.price_kwh) * vat_mul
            return price, tariff.id, period.id

        self.log_warning(
            f"Brak pasującego okresu dla {slot_dt.isoformat()} "
            f"(taryfa: {tariff.name}). Dodaj jawny okres typu pozaszczyt "
            f"(0–24h, bez sensora dnia).",
            extra={"tariff": tariff.name, "hour": slot_dt.isoformat()},
        )
        return None, tariff.id, None

    def _is_day_sensor_on(self, sensor_id: str, day: date, day_offset: int) -> bool:
        """Resolve whether ``sensor_id`` is ON for ``day``."""
        if day_offset == 0:
            state = self.hass.states.get(sensor_id)
            return state is not None and state.state == "on"
        if day_offset > 0:
            cached = self._future_day_cache.get((sensor_id, day))
            if cached is not None:
                return cached
            # Cache miss → sensor likely doesn't support check_date.
            # Best effort: today's state.
            state = self.hass.states.get(sensor_id)
            return state is not None and state.state == "on"
        # Past day: best effort, use today's state.
        state = self.hass.states.get(sensor_id)
        return state is not None and state.state == "on"

    # --------------------------------------------------------------- snapshot

    @staticmethod
    def _snapshot_key(slot_dt: datetime) -> str:
        return dt_util.as_utc(slot_dt).replace(minute=0, second=0, microsecond=0).isoformat()

    @staticmethod
    def _prune(snapshots: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        cutoff = dt_util.utcnow() - timedelta(days=_SNAPSHOT_RETENTION_DAYS)
        kept: dict[str, dict[str, Any]] = {}
        for key, value in snapshots.items():
            try:
                stamp = datetime.fromisoformat(key)
            except (ValueError, TypeError):
                continue
            if stamp >= cutoff:
                kept[key] = value
        return kept

    def _schedule_save(self) -> None:
        if self._store is None:
            return
        self._store.async_delay_save(self._serialise_snapshots, 30.0)

    def _serialise_snapshots(self) -> dict[str, Any]:
        return {"snapshots": self._snapshots}

    async def async_clear_data(self) -> None:
        """Drop recorded tariff snapshots and day-sensor caches.

        Tariff definitions live in the config entry, so they are reloaded from
        config rather than wiped here.
        """
        if self._store is not None:
            await self._store.async_remove()
        self._snapshots = {}
        self._future_day_cache = {}
        self._unsupported_sensors = set()
        self._reload_config()

    # ------------------------------------------------------------------ public

    def snapshot_for(self, hour_start: datetime) -> float | None:
        """Lookup helper used by ``coordinator.get_series`` for past hours."""
        return (self._snapshots.get(self._snapshot_key(hour_start)) or {}).get("price")

    def distribution_for(self, hour_start: datetime) -> float | None:
        """Gross distribution price (PLN/kWh) for *any* hour.

        Returns the persisted snapshot when present (past/current hours), else
        resolves it live from the current config (future hours) — used by the
        price-archive view, which spans days the snapshot store hasn't reached.
        """
        snap = self.snapshot_for(hour_start)
        if snap is not None:
            return snap
        slot_dt = hour_start
        if slot_dt.tzinfo is None:
            slot_dt = dt_util.as_local(slot_dt)
        day_offset = (slot_dt.date() - dt_util.now().date()).days
        price, _, _ = self._resolve_price(slot_dt, day_offset)
        return price

    @property
    def tariffs(self) -> list[Tariff]:
        return list(self._tariffs)
