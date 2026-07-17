"""Climate module.

Learns how the configured weather-dependent load (``CONF_CLIMATE_SENSOR``, e.g.
the AC meter) consumes energy as a function of the outside temperature, and
forecasts it from the weather entity's hourly temperature forecast instead of
the weekday+hour weekly average.

Data model: hourly ``(temperature, kWh)`` pairs, folded once per settled day.
Temperatures come from the module's own history store — the current temperature
of the weather entity is recorded every update (weather entities keep no
recorder statistics), seeded once from the recorder's short state history.
The kWh side reads the device's long-term statistics, so the profile keeps
growing beyond the consumption module's learn window (capped at a year).

The profile is a (temperature-bin × hour-of-day) average with neighbour-bin
smoothing. Once enough days are observed the module takes over the device from
the consumption module (see ``handles``): the weekly profile stops contributing
for that sensor and this model adds the temperature-driven expectation to each
slot; hours beyond the temperature forecast fall back to the device's weekly
average so the plan horizon keeps its full demand.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CLIMATE_SENSOR,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    STORAGE_VERSION_CLIMATE,
)
from ..models import Forecast
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

TEMP_BIN_C = 2.0  # profile resolution: one bin per 2 °C
MIN_CELL_SAMPLES = 3  # samples needed before a (bin, hour) cell is trusted
MIN_LEARN_DAYS = 14  # observed days before the model replaces the weekly profile
TEMP_HISTORY_DAYS = 400  # how long hourly temperature observations are kept
MIN_DAY_HOURS = 12  # hour-pairs needed to fold a day into the profile


class TemperatureProfile:
    """Average kWh per (temperature bin × hour of day), with smoothing."""

    def __init__(self) -> None:
        # {(bin, hour): [sum_kwh, count]}
        self._cells: dict[tuple[int, int], list[float]] = {}
        self._observed_dates: set[str] = set()

    @staticmethod
    def _bin(temperature: float) -> int:
        return int(temperature // TEMP_BIN_C)

    def is_date_observed(self, day: date) -> bool:
        return day.isoformat() in self._observed_dates

    def mark_date_observed(self, day: date) -> None:
        self._observed_dates.add(day.isoformat())

    def observe(self, temperature: float, hour: int, kwh: float) -> None:
        key = (self._bin(temperature), hour)
        cell = self._cells.setdefault(key, [0.0, 0.0])
        cell[0] += kwh
        cell[1] += 1

    @property
    def observed_days(self) -> int:
        return len(self._observed_dates)

    @property
    def samples(self) -> int:
        return int(sum(c[1] for c in self._cells.values()))

    def predict(self, temperature: float, hour: int) -> float | None:
        """Expected kWh for the hour at the given outside temperature.

        Averages the (bin, hour) cell with its ±1 neighbour bins (the centre
        bin weighted double) so a sparse profile still answers between bins.
        Falls back to the bins' all-hours average when the hour cells are too
        thin; ``None`` when the temperature region has no data at all.
        """
        b = self._bin(temperature)
        neighbours = ((b - 1, 1.0), (b, 2.0), (b + 1, 1.0))

        def _avg(cells: dict[tuple[int, int], list[float]], match_hour: bool) -> tuple[float, float]:
            total = weight = 0.0
            for nb, w in neighbours:
                for (cb, ch), (s, c) in cells.items():
                    if cb != nb or c <= 0:
                        continue
                    if match_hour and ch != hour:
                        continue
                    total += w * s / c * min(c, MIN_CELL_SAMPLES)
                    weight += w * min(c, MIN_CELL_SAMPLES)
            return total, weight

        total, weight = _avg(self._cells, match_hour=True)
        if weight >= MIN_CELL_SAMPLES:
            return total / weight
        total, weight = _avg(self._cells, match_hour=False)
        if weight >= MIN_CELL_SAMPLES:
            return total / weight
        return None

    def to_dict(self) -> dict:
        return {
            "cells": {f"{b}:{h}": [s, c] for (b, h), (s, c) in self._cells.items()},
            "observed_dates": sorted(self._observed_dates),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "TemperatureProfile":
        profile = cls()
        if not data:
            return profile
        for key, (s, c) in (data.get("cells") or {}).items():
            b, h = (int(x) for x in key.split(":"))
            profile._cells[(b, h)] = [float(s), float(c)]
        profile._observed_dates = set(data.get("observed_dates") or [])
        return profile


class ClimateModule(PowerPilotModule):
    """Temperature-driven consumption model for the configured climate load."""

    domain = "climate"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self.profile = TemperatureProfile()
        # {local iso hour: °C} — own history, since weather entities have no
        # recorder statistics to read back from.
        self._temps: dict[str, float] = {}
        self._temps_backfilled = False
        self._store: Store | None = None
        self._last_learn_day: date | None = None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    @property
    def sensor(self) -> str | None:
        return self.config.get(CONF_CLIMATE_SENSOR)

    @property
    def ready(self) -> bool:
        return (
            bool(self.sensor)
            and self.profile.observed_days >= MIN_LEARN_DAYS
        )

    def handles(self, entity_id: str) -> bool:
        """Whether this module owns the demand forecast for ``entity_id``."""
        return bool(self.sensor) and entity_id == self.sensor and self.ready

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------
    async def async_setup(self) -> None:
        self._store = Store(
            self.hass,
            STORAGE_VERSION_CLIMATE,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_climate",
        )
        stored = await self._store.async_load()
        if stored:
            self.profile = TemperatureProfile.from_dict(stored.get("profile"))
            self._temps = {
                k: float(v) for k, v in (stored.get("temps") or {}).items()
            }
            self._temps_backfilled = bool(stored.get("temps_backfilled"))
            last = stored.get("last_learn_day")
            self._last_learn_day = date.fromisoformat(last) if last else None

    async def _async_save(self) -> None:
        if self._store is None:
            return
        await self._store.async_save(
            {
                "profile": self.profile.to_dict(),
                "temps": self._temps,
                "temps_backfilled": self._temps_backfilled,
                "last_learn_day": (
                    self._last_learn_day.isoformat() if self._last_learn_day else None
                ),
            }
        )

    async def async_clear_data(self) -> None:
        if self._store is not None:
            await self._store.async_remove()
        self.profile = TemperatureProfile()
        self._temps = {}
        self._temps_backfilled = False
        self._last_learn_day = None

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------
    @staticmethod
    def _hour_key(hour: datetime) -> str:
        return dt_util.as_local(hour).replace(
            minute=0, second=0, microsecond=0
        ).isoformat()

    def temperature_for(self, hour: datetime) -> float | None:
        """Best-known outside temperature for a local hour.

        Own recorded history first (realized), then the weather module's
        forecast (future hours).
        """
        recorded = self._temps.get(self._hour_key(hour))
        if recorded is not None:
            return recorded
        return self.coordinator.weather.temperature_at(
            hour.replace(minute=0, second=0, microsecond=0)
        )

    async def async_update(self) -> None:
        if not self.sensor:
            return
        if not self.config.get(CONF_WEATHER_ENTITY):
            self.log_warning(
                "Sensor klimatyzacji wskazany, ale brak encji pogody — profil "
                "temperaturowy nie może się uczyć."
            )
            return

        changed = self._record_current_temperature()
        if not self._temps_backfilled:
            changed = await self._backfill_temperatures() or changed
        changed = await self._maybe_learn() or changed
        if changed:
            await self._async_save()

        self.log_info(
            f"Profil temperaturowy ({self.sensor}): {self.profile.observed_days} dni / "
            f"{self.profile.samples} próbek, temperatura zapisana dla "
            f"{len(self._temps)} godzin"
            + ("." if self.ready else f" — model aktywny od {MIN_LEARN_DAYS} dni nauki."),
            extra={
                "sensor": self.sensor,
                "observed_days": self.profile.observed_days,
                "samples": self.profile.samples,
                "ready": self.ready,
            },
        )

    def _record_current_temperature(self) -> bool:
        temp = self.coordinator.weather.current_temperature
        if temp is None:
            return False
        key = self._hour_key(dt_util.now())
        if self._temps.get(key) == temp:
            return False
        self._temps[key] = temp
        self._prune_temps()
        return True

    def _prune_temps(self) -> None:
        cutoff = self._hour_key(dt_util.now() - timedelta(days=TEMP_HISTORY_DAYS))
        self._temps = {k: v for k, v in self._temps.items() if k >= cutoff}

    async def _backfill_temperatures(self) -> bool:
        """One-time seed of the temperature history from recorder states.

        The recorder keeps raw weather-entity states (with attributes) for its
        purge window (~10 days by default) — enough to start learning without
        waiting weeks for the forward recording to accumulate.
        """
        from homeassistant.components.recorder import get_instance, history

        entity_id = self.config.get(CONF_WEATHER_ENTITY)
        end = dt_util.now()
        start = end - timedelta(days=10)
        changes = await get_instance(self.hass).async_add_executor_job(
            history.state_changes_during_period,
            self.hass,
            start,
            end,
            entity_id,
            False,  # no_attributes — the temperature lives in attributes
            False,  # descending
            None,  # limit
            True,  # include_start_time_state
        )
        added = 0
        for st in changes.get(entity_id, []):
            temp = st.attributes.get("temperature")
            if temp is None:
                continue
            try:
                value = float(temp)
            except (TypeError, ValueError):
                continue
            key = self._hour_key(st.last_updated)
            if key not in self._temps:
                self._temps[key] = value
                added += 1
        self._temps_backfilled = True
        if added:
            self.log_info(
                f"Zaczątek historii temperatury z recordera: {added} godzin.",
                extra={"hours": added},
            )
        return True

    async def _maybe_learn(self) -> bool:
        """Fold settled days of (temperature, device kWh) into the profile."""
        today = dt_util.now().date()
        if self._last_learn_day == today:
            return False

        # Days with recorded temperatures that are not folded yet, oldest first.
        candidate_days = sorted(
            {
                d
                for k in self._temps
                if (d := date.fromisoformat(k[:10])) < today
                and not self.profile.is_date_observed(d)
            }
        )
        if not candidate_days:
            self._last_learn_day = today
            return True  # persist the cursor

        start = dt_util.start_of_local_day(candidate_days[0])
        end = dt_util.start_of_local_day(today)
        kwh = await self.coordinator.consumption.async_range_kwh(
            self.sensor, start, end
        )

        folded = 0
        for day in candidate_days:
            day_start = dt_util.start_of_local_day(day)
            pairs: list[tuple[float, int, float]] = []
            for h in range(24):
                hour = day_start + timedelta(hours=h)
                temp = self._temps.get(self._hour_key(hour))
                energy = kwh.get(hour)
                if temp is not None and energy is not None:
                    pairs.append((temp, h, energy))
            if len(pairs) < MIN_DAY_HOURS:
                continue  # retried next learn pass once more data lands
            for temp, h, energy in pairs:
                self.profile.observe(temp, h, energy)
            self.profile.mark_date_observed(day)
            folded += 1

        self._last_learn_day = today
        if folded:
            self.log_info(
                f"Profil temperaturowy: dodano {folded} dni "
                f"(łącznie {self.profile.observed_days}).",
                extra={"folded_days": folded},
            )
        return True

    # ------------------------------------------------------------------
    # Forecast contribution
    # ------------------------------------------------------------------
    def forecast_kwh(self, hour: datetime) -> float | None:
        """Expected climate-load kWh for an hour, once the model is ready.

        Temperature-driven when a temperature is known for the hour; the
        device's weekly average otherwise (hours beyond the weather forecast
        horizon), so the plan's demand never silently loses the climate load.
        """
        if not self.ready:
            return None
        temp = self.temperature_for(hour)
        if temp is not None:
            predicted = self.profile.predict(temp, hour.hour)
            if predicted is not None:
                return predicted
        weekly = self.coordinator.consumption.devices.get(self.sensor)
        return weekly.value(hour.weekday(), hour.hour) if weekly else None

    def contribute(self, forecast: Forecast) -> None:
        if not self.sensor:
            return
        if not self.ready:
            self.log_info(
                f"Profil temperaturowy uczy się: {self.profile.observed_days}/"
                f"{MIN_LEARN_DAYS} dni — urządzenie na razie planowane z profilu "
                "tygodniowego."
            )
            return
        total = 0.0
        hits = 0
        for slot in forecast.slots:
            energy = self.forecast_kwh(slot.start)
            if energy and energy > 0:
                slot.extra_load_kwh += energy
                slot.tags.append("climate")
                total += energy
                hits += 1
        self.log_info(
            f"Klimat ({self.sensor}): {total:.1f} kWh na {hits}h horyzontu "
            "(model temperaturowy).",
            extra={"hours": hits, "total_kwh": round(total, 2)},
        )
