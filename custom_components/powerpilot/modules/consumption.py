"""Consumption module.

Learns an hourly household consumption profile keyed by ``(weekday, hour)`` from
the configured main consumption sensor, using Home Assistant long-term statistics
(the recorder). Separately-metered devices (AC/heating, washer, boiler, iron, …)
are broken out into their own weekly profiles, leaving a clean **base** (the
uncontrollable background load):

    base = main − Σ(device sensors)

The forecast demand is reconstructed as ``base + Σ(device profiles)`` so totals
stay correct today. Because the breakdown is stored per device, a smarter forward
model (climate by temperature, calendar-driven appliances) can later *replace* a
device profile without double counting.

A sensible default daily shape is used as a fallback until enough history has been
learned.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CONSUMPTION_LEARN_DAYS,
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    DOMAIN,
)
from ..models import Forecast
from ..profiles import WeeklyAccumulator
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1

# Relative weighting of a typical residential day (fallback before learning).
_DEFAULT_SHAPE = [
    0.5, 0.4, 0.4, 0.4, 0.4, 0.5,  # 00-05 night
    0.7, 1.0, 1.1, 0.9, 0.8, 0.8,  # 06-11 morning
    0.8, 0.8, 0.7, 0.7, 0.8, 1.0,  # 12-17 afternoon
    1.4, 1.5, 1.4, 1.1, 0.8, 0.6,  # 18-23 evening peak
]
_DEFAULT_DAILY_KWH = 12.0

# Unit → kWh-per-unit factor for an hourly value.
_POWER_UNITS = {"W": 0.001, "kW": 1.0}
_ENERGY_UNITS = {"Wh": 0.001, "kWh": 1.0}


class ConsumptionModule(PowerPilotModule):
    """Provides learned base + per-device consumption per hour."""

    domain = "consumption"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self.base = WeeklyAccumulator()
        self.devices: dict[str, WeeklyAccumulator] = {}
        self._store: Store | None = None
        self._last_learn_day: date | None = None
        self._default_profile = self._build_default_profile()

    # ------------------------------------------------------------------
    # Setup / persistence
    # ------------------------------------------------------------------
    async def async_setup(self) -> None:
        self._store = Store(
            self.hass,
            STORAGE_VERSION,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_consumption",
        )
        stored = await self._store.async_load()
        if stored:
            self.base = WeeklyAccumulator.from_dict(stored.get("base"))
            self.devices = {
                eid: WeeklyAccumulator.from_dict(payload)
                for eid, payload in (stored.get("devices") or {}).items()
            }
            last = stored.get("last_learn_day")
            self._last_learn_day = date.fromisoformat(last) if last else None
        await self._maybe_learn()

    async def async_update(self) -> None:
        await self._maybe_learn()

    async def _async_save(self) -> None:
        if self._store is None:
            return
        await self._store.async_save(
            {
                "base": self.base.to_dict(),
                "devices": {eid: acc.to_dict() for eid, acc in self.devices.items()},
                "last_learn_day": self._last_learn_day.isoformat()
                if self._last_learn_day
                else None,
            }
        )

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------
    async def _maybe_learn(self) -> None:
        main_sensor = self.config.get(CONF_CONSUMPTION_SENSOR)
        if not main_sensor:
            return
        today = dt_util.now().date()
        if self._last_learn_day == today and self.base.observed_days > 0:
            return

        learn_days = int(self.config.get(CONF_CONSUMPTION_LEARN_DAYS, 21))
        device_sensors = list(self.config.get(CONF_DEVICE_SENSORS) or [])

        start = dt_util.start_of_local_day(today - timedelta(days=learn_days))
        end = dt_util.start_of_local_day(today)  # only settled past days

        main_hourly = await self._fetch_hourly_kwh(main_sensor, start, end)
        device_hourly = {
            eid: await self._fetch_hourly_kwh(eid, start, end) for eid in device_sensors
        }

        changed = False
        day = start.date()
        while day < today:
            if not self.base.is_date_observed(day):
                if self._fold_day(day, main_hourly, device_hourly):
                    changed = True
            day += timedelta(days=1)

        self._last_learn_day = today
        if changed:
            await self._async_save()

    def _fold_day(
        self,
        day: date,
        main_hourly: dict[datetime, float],
        device_hourly: dict[str, dict[datetime, float]],
    ) -> bool:
        """Fold one settled day's hourly data into the accumulators."""
        day_hours = [
            dt_util.start_of_local_day(day) + timedelta(hours=h) for h in range(24)
        ]
        present = [h for h in day_hours if h in main_hourly]
        if len(present) < 20:  # require a near-complete day
            return False

        for hour in present:
            main_kwh = main_hourly.get(hour, 0.0)
            device_total = 0.0
            for eid, series in device_hourly.items():
                value = series.get(hour, 0.0)
                self.devices.setdefault(eid, WeeklyAccumulator()).observe(hour, value)
                device_total += value
            base_kwh = max(0.0, main_kwh - device_total)
            self.base.observe(hour, base_kwh)

        self.base.mark_date_observed(day)
        for acc in self.devices.values():
            acc.mark_date_observed(day)
        return True

    async def _fetch_hourly_kwh(
        self, entity_id: str, start: datetime, end: datetime
    ) -> dict[datetime, float]:
        """Return ``{hour_start: kWh}`` from long-term statistics."""
        unit = self._sensor_unit(entity_id)
        kind = self._sensor_kind(unit)
        if kind is None:
            _LOGGER.debug("Sensor %s has no power/energy unit (%s); skipping", entity_id, unit)
            return {}

        types = {"mean"} if kind == "power" else {"sum"}
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {entity_id},
            "hour",
            None,
            types,
        )
        series = rows.get(entity_id, [])
        if kind == "power":
            return self._power_series(series, unit)
        return self._energy_series(series, unit)

    def _power_series(self, series: list, unit: str) -> dict[datetime, float]:
        factor = _POWER_UNITS[unit]
        out: dict[datetime, float] = {}
        for row in series:
            mean = row.get("mean")
            if mean is None:
                continue
            out[self._row_hour(row)] = float(mean) * factor
        return out

    def _energy_series(self, series: list, unit: str) -> dict[datetime, float]:
        factor = _ENERGY_UNITS[unit]
        out: dict[datetime, float] = {}
        prev_sum: float | None = None
        for row in series:
            total = row.get("sum")
            if total is None:
                prev_sum = None
                continue
            total = float(total)
            if prev_sum is not None:
                delta = total - prev_sum
                if delta >= 0:  # ignore meter resets
                    out[self._row_hour(row)] = delta * factor
            prev_sum = total
        return out

    @staticmethod
    def _row_hour(row: dict) -> datetime:
        start = row["start"]
        if isinstance(start, (int, float)):
            start = dt_util.utc_from_timestamp(start)
        return dt_util.as_local(start).replace(minute=0, second=0, microsecond=0)

    def _sensor_unit(self, entity_id: str) -> str | None:
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return state.attributes.get("unit_of_measurement")

    @staticmethod
    def _sensor_kind(unit: str | None) -> str | None:
        if unit in _POWER_UNITS:
            return "power"
        if unit in _ENERGY_UNITS:
            return "energy"
        return None

    # ------------------------------------------------------------------
    # Forecast contribution
    # ------------------------------------------------------------------
    def _build_default_profile(self) -> dict[tuple[int, int], float]:
        profile: dict[tuple[int, int], float] = {}
        total_shape = sum(_DEFAULT_SHAPE)
        for weekday in range(7):
            weekend_factor = 1.1 if weekday >= 5 else 1.0
            daily = _DEFAULT_DAILY_KWH * weekend_factor
            for hour, weight in enumerate(_DEFAULT_SHAPE):
                profile[(weekday, hour)] = daily * weight / total_shape
        return profile

    def base_value(self, weekday: int, hour: int) -> float:
        learned = self.base.value(weekday, hour)
        if learned is not None:
            return learned
        return self._default_profile[(weekday, hour)]

    def device_value(self, weekday: int, hour: int) -> float:
        return sum(acc.value(weekday, hour) or 0.0 for acc in self.devices.values())

    def contribute(self, forecast: Forecast) -> None:
        learned = self.base.observed_days > 0
        for slot in forecast.slots:
            weekday, hour = slot.start.weekday(), slot.start.hour
            slot.base_consumption_kwh += self.base_value(weekday, hour)
            if learned:
                # Add back separately-metered devices with no smarter model yet.
                slot.base_consumption_kwh += self.device_value(weekday, hour)

