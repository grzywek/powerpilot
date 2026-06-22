"""Distribution tariff module.

Resolves the distribution price (PLN/kWh) per hour using user-configured
:class:`Tariff` definitions. The pipeline rule is:

    *All configuration changes take effect from the **next** hour.*

This means:
  - ``slot[0]`` (the hour we are currently in) → use a **snapshot** of the
    distribution price; lazily created on first access from the current config.
  - ``slot[1..]`` (future hours) → **live**-evaluated from the current config
    using the matching day classifier sensor.

For day-of-week classification we honour ``period.day_sensor``. For future days
D+1..D+7 we substitute ``binary_sensor.workday_today`` with the corresponding
``CONF_WORKDAY_PLUS_N_SENSORS`` entry; other custom day sensors are evaluated
as-is (with the caveat that their *current* state is used, not the future
state).

Snapshots are persisted to a dedicated ``Store`` keyed by ISO hour timestamp,
pruned to the last 90 days on every load.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_TARIFFS,
    CONF_WORKDAY_PLUS_N_SENSORS,
    DOMAIN,
    MAX_WORKDAY_PLUS_N,
    STORAGE_VERSION_TARIFF_SNAPSHOTS,
)
from ..models import Forecast, Tariff, tariff_for_day
from .base import PowerPilotModule

# Names that we transparently re-map to D+N sensors for future days. Anything
# else is read as-is (effectively today's state for future days).
_WORKDAY_ALIASES = ("binary_sensor.workday_today", "binary_sensor.workday")

# How many days of past snapshots to keep.
_SNAPSHOT_RETENTION_DAYS = 90


class TariffModule(PowerPilotModule):
    """Provides hourly distribution prices to the forecast."""

    domain = "tariff"

    def __init__(self, hass: HomeAssistant, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._store: Store | None = None
        # Snapshot dict: {iso_hour_str: {"price": float, "tariff_id": str,
        #                                "period_id": str | None, "backfilled": bool}}
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._tariffs: list[Tariff] = []
        self._workday_plus_n: list[str] = []

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
        """Re-parse tariffs + workday sensors from the current entry options."""
        raw_tariffs = self.config.get(CONF_TARIFFS) or []
        self._tariffs = [Tariff.from_dict(t) for t in raw_tariffs]
        raw_sensors = self.config.get(CONF_WORKDAY_PLUS_N_SENSORS) or []
        # Pad to MAX so index access is safe; treat empty strings as "missing".
        padded = list(raw_sensors)[:MAX_WORKDAY_PLUS_N]
        while len(padded) < MAX_WORKDAY_PLUS_N:
            padded.append("")
        self._workday_plus_n = padded

    # ------------------------------------------------------------------ update

    async def async_update(self) -> None:
        # Picks up live config changes the OptionsFlow may have written.
        self._reload_config()

        today = dt_util.now().date()
        active = tariff_for_day(self._tariffs, today)
        if active is None:
            if self._tariffs:
                self.log_warning(
                    f"Brak aktywnej taryfy dla {today.isoformat()} "
                    f"(skonfigurowanych taryf: {len(self._tariffs)}).",
                    extra={"configured_tariffs": len(self._tariffs)},
                )
            else:
                self.log_warning(
                    "Nie skonfigurowano żadnej taryfy dystrybucyjnej. "
                    "Optymalizator nie zna pełnej ceny — skonfiguruj taryfę "
                    "w Konfiguracji → PowerPilot → Konfiguruj → Taryfy.",
                    extra={"configured_tariffs": 0},
                )
            return

        self.log_info(
            f"Aktywna taryfa: {active.name} ({len(active.periods)} okresów, "
            f"baza {active.fallback_price_kwh:.4f} PLN/kWh). "
            f"Snapshot: {len(self._snapshots)} godzin.",
            extra={
                "tariff_name": active.name,
                "periods": len(active.periods),
                "fallback_price_kwh": active.fallback_price_kwh,
                "snapshot_hours": len(self._snapshots),
                "valid_from": active.valid_from.isoformat() if active.valid_from else None,
                "valid_to": active.valid_to.isoformat() if active.valid_to else None,
            },
        )

    # -------------------------------------------------------------- contribute

    def contribute(self, forecast: Forecast) -> None:
        if not forecast.slots:
            return
        if not self._tariffs:
            return  # No tariffs → leave slot.distribution_price_kwh as None

        now = dt_util.now()
        today = now.date()
        current_hour_start = now.replace(minute=0, second=0, microsecond=0)
        snapshot_dirty = False

        for index, slot in enumerate(forecast.slots):
            slot_dt = slot.start
            if slot_dt.tzinfo is None:
                # Shouldn't happen — forecast.build uses tz-aware datetimes — but
                # defend against bad fixtures.
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
                # Future hour: live evaluation.
                price, _, _ = self._resolve_price(slot_dt, day_offset)
                slot.distribution_price_kwh = price

        if snapshot_dirty:
            self._schedule_save()

    # ------------------------------------------------------------- resolution

    def _resolve_price(
        self, slot_dt: datetime, day_offset: int
    ) -> tuple[float | None, str | None, str | None]:
        """Return ``(price, tariff_id, period_id)`` for the given hour."""
        tariff = tariff_for_day(self._tariffs, slot_dt.date())
        if tariff is None:
            return None, None, None

        for period in tariff.periods:
            if not period.matches_hour(slot_dt.hour):
                continue
            if period.day_sensor is None:
                return period.price_kwh, tariff.id, period.id
            actual_sensor = self._effective_day_sensor(period.day_sensor, day_offset)
            if not actual_sensor:
                continue
            state = self.hass.states.get(actual_sensor)
            if state is None:
                continue
            if state.state == "on":
                return period.price_kwh, tariff.id, period.id

        return tariff.fallback_price_kwh, tariff.id, None

    def _effective_day_sensor(self, sensor_id: str, day_offset: int) -> str | None:
        """Map period.day_sensor to the right entity for the given day offset.

        For "today-context" workday sensors we substitute with the configured
        ``binary_sensor.workday_plus_N`` for D+1..D+7. Beyond 7 days we return
        ``None`` to skip the period (no classification possible). Other custom
        day sensors are returned as-is — their *current* state will be used,
        which is the best we can do without extra config.
        """
        if day_offset == 0:
            return sensor_id
        is_workday_alias = sensor_id in _WORKDAY_ALIASES
        if 1 <= day_offset <= MAX_WORKDAY_PLUS_N:
            if is_workday_alias:
                configured = self._workday_plus_n[day_offset - 1]
                return configured or None
            return sensor_id
        # Beyond D+7: only non-workday-alias sensors get evaluated (best-effort,
        # using today's state). Workday-alias periods are skipped.
        return None if is_workday_alias else sensor_id

    # --------------------------------------------------------------- snapshot

    @staticmethod
    def _snapshot_key(slot_dt: datetime) -> str:
        # Normalise to UTC ISO so DST transitions don't collide.
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
        # Coalesce rapid contribute() bursts into a single disk write.
        self._store.async_delay_save(self._serialise_snapshots, 30.0)

    def _serialise_snapshots(self) -> dict[str, Any]:
        return {"snapshots": self._snapshots}

    # ------------------------------------------------------------------ public

    def snapshot_for(self, hour_start: datetime) -> float | None:
        """Lookup helper used by ``coordinator.get_series`` for past hours."""
        return (self._snapshots.get(self._snapshot_key(hour_start)) or {}).get("price")

    @property
    def tariffs(self) -> list[Tariff]:
        return list(self._tariffs)
