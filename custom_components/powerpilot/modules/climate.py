"""Climate module.

Learns how the configured weather-dependent loads (``CONF_CLIMATE_SENSORS``,
e.g. AC or heat-pump meters) consume energy as a function of the outside
temperature, and forecasts each of them from the weather entity's hourly
temperature forecast instead of the weekday+hour weekly average.

Data model: hourly ``(temperature, kWh)`` pairs per sensor, folded once per
settled day. When presence sensors are configured
(``CONF_CLIMATE_PRESENCE_SENSORS``), hours with nobody home are left out of
the fold, so the profile models occupied-house consumption only.
Temperatures come from the module's own history store — the
current temperature of the weather entity is recorded every update (weather
entities keep no recorder statistics), seeded once from the recorder's short
state history. The kWh side reads each device's long-term statistics, so the
profiles keep growing beyond the consumption module's learn window (capped at
a year).

Each profile is a (temperature-bin × hour-of-day) average with neighbour-bin
smoothing. Once a sensor's profile has enough days observed the module takes
that device over from the consumption module (see ``handles``): the weekly
profile stops contributing for it and this model adds the temperature-driven
expectation to each slot; hours beyond the temperature forecast fall back to
the device's weekly average so the plan horizon keeps its full demand.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_CLIMATE_PRESENCE_SENSORS,
    CONF_CLIMATE_SENSORS,
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

# States that count as "someone is home" for the presence gate. Anything else
# known (e.g. "not_home", "off") reads as away; unknown/unavailable states
# contribute nothing, so a flaky sensor cannot mark hours as empty.
PRESENCE_HOME_STATES = {"home", "on", "true"}
_UNKNOWN_STATES = {"unknown", "unavailable", "none", ""}


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

    def reset(self) -> None:
        """Drop all cells and observed days so the profile can be re-folded."""
        self._cells = {}
        self._observed_dates = set()

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

    def as_matrix(self) -> list[dict]:
        """Panel heatmap rows: one per observed temperature bin, 24 hour cells.

        Cells are raw per-cell averages (no smoothing) so the panel shows what
        was actually measured; ``None`` where a (bin, hour) has no samples.
        """
        rows: list[dict] = []
        for b in sorted({b for (b, _h) in self._cells}):
            values: list[float | None] = []
            for hour in range(24):
                cell = self._cells.get((b, hour))
                values.append(
                    round(cell[0] / cell[1], 3) if cell and cell[1] else None
                )
            rows.append(
                {
                    "temp_from": b * TEMP_BIN_C,
                    "temp_to": (b + 1) * TEMP_BIN_C,
                    "samples": int(
                        sum(
                            c[1]
                            for (cb, _h), c in self._cells.items()
                            if cb == b
                        )
                    ),
                    "values": values,
                }
            )
        return rows

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
    """Temperature-driven consumption model for the configured climate loads."""

    domain = "climate"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        # {device entity_id: its temperature profile}
        self.profiles: dict[str, TemperatureProfile] = {}
        # {local iso hour: °C} — own history, since weather entities have no
        # recorder statistics to read back from.
        self._temps: dict[str, float] = {}
        self._temps_backfilled = False
        self._store: Store | None = None
        self._last_learn_day: date | None = None
        # Presence-sensor set the profiles were folded with. When the
        # configuration diverges, the profiles are re-folded from source data
        # (own temp history + long-term kWh statistics) under the new gate.
        self._presence_fingerprint: list[str] | None = None

    # ------------------------------------------------------------------
    # Wiring
    # ------------------------------------------------------------------
    @property
    def sensors(self) -> list[str]:
        return list(self.config.get(CONF_CLIMATE_SENSORS) or [])

    @property
    def presence_sensors(self) -> list[str]:
        return list(self.config.get(CONF_CLIMATE_PRESENCE_SENSORS) or [])

    def profile_for(self, entity_id: str) -> TemperatureProfile:
        return self.profiles.setdefault(entity_id, TemperatureProfile())

    def is_ready(self, entity_id: str) -> bool:
        profile = self.profiles.get(entity_id)
        return profile is not None and profile.observed_days >= MIN_LEARN_DAYS

    def handles(self, entity_id: str) -> bool:
        """Whether this module owns the demand forecast for ``entity_id``."""
        return entity_id in self.sensors and self.is_ready(entity_id)

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
            self.profiles = {
                eid: TemperatureProfile.from_dict(payload)
                for eid, payload in (stored.get("profiles") or {}).items()
            }
            self._temps = {
                k: float(v) for k, v in (stored.get("temps") or {}).items()
            }
            self._temps_backfilled = bool(stored.get("temps_backfilled"))
            last = stored.get("last_learn_day")
            self._last_learn_day = date.fromisoformat(last) if last else None
            fingerprint = stored.get("presence_fingerprint")
            self._presence_fingerprint = (
                list(fingerprint) if fingerprint is not None else None
            )

    async def _async_save(self) -> None:
        if self._store is None:
            return
        await self._store.async_save(
            {
                "profiles": {
                    eid: profile.to_dict() for eid, profile in self.profiles.items()
                },
                "temps": self._temps,
                "temps_backfilled": self._temps_backfilled,
                "last_learn_day": (
                    self._last_learn_day.isoformat() if self._last_learn_day else None
                ),
                "presence_fingerprint": self._presence_fingerprint,
            }
        )

    async def async_clear_data(self) -> None:
        if self._store is not None:
            await self._store.async_remove()
        self.profiles = {}
        self._temps = {}
        self._temps_backfilled = False
        self._last_learn_day = None
        self._presence_fingerprint = None

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
        sensors = self.sensors
        if not sensors:
            return
        if not self.config.get(CONF_WEATHER_ENTITY):
            self.log_warning(
                "Sensory klimatyzacji wskazane, ale brak encji pogody — profile "
                "temperaturowe nie mogą się uczyć."
            )
            return

        # Drop profiles of sensors removed from the configuration.
        stale = [eid for eid in self.profiles if eid not in sensors]
        changed = bool(stale)
        for eid in stale:
            del self.profiles[eid]

        # A changed presence-sensor set means the existing cells were folded
        # under a different gate — re-fold everything from source data (own
        # temp history + long-term kWh statistics; no samples are lost, they
        # are just re-aggregated with the new filter). The whole rebuild
        # happens in the next learn pass, so readiness is not reset for long.
        fingerprint = sorted(self.presence_sensors)
        if (self._presence_fingerprint or []) != fingerprint:
            for profile in self.profiles.values():
                profile.reset()
            self._presence_fingerprint = fingerprint
            self._last_learn_day = None  # force the learn pass this update
            changed = True
            if self.profiles:
                self.log_info(
                    "Zmiana czujników obecności — profile temperaturowe "
                    "przeliczane od nowa z historii temperatur i statystyk kWh.",
                    extra={"presence_sensors": fingerprint},
                )

        changed = self._record_current_temperature() or changed
        if not self._temps_backfilled:
            changed = await self._backfill_temperatures() or changed
        changed = await self._maybe_learn() or changed
        if changed:
            await self._async_save()

        summary = " · ".join(
            f"{eid.split('.')[-1]}: {self.profile_for(eid).observed_days} dni"
            + ("" if self.is_ready(eid) else f" (aktywny od {MIN_LEARN_DAYS})")
            for eid in sensors
        )
        self.log_info(
            f"Profile temperaturowe — {summary}; temperatura zapisana dla "
            f"{len(self._temps)} godzin.",
            extra={
                "sensors": {
                    eid: {
                        "observed_days": self.profile_for(eid).observed_days,
                        "samples": self.profile_for(eid).samples,
                        "ready": self.is_ready(eid),
                    }
                    for eid in sensors
                },
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

    @staticmethod
    def _presence_map(
        states_by_entity: dict[str, list], start: datetime, end: datetime
    ) -> dict[str, bool]:
        """Per-hour "anyone home" flags from recorder state runs.

        Keys are local-hour ISO strings (like ``_temps``). An hour appears
        only when at least one presence entity had a *known* state overlapping
        it; the value is True when any entity read home/on at any point during
        the hour. Hours missing from the map are unknown — the learner treats
        them as present, so the gate only drops hours known to be empty.
        """
        presence: dict[str, bool] = {}
        for states in states_by_entity.values():
            for st, nxt in zip(states, [*states[1:], None]):
                value = str(st.state).lower()
                if value in _UNKNOWN_STATES:
                    continue
                home = value in PRESENCE_HOME_STATES
                seg_start = max(st.last_updated, dt_util.as_utc(start))
                seg_end = nxt.last_updated if nxt is not None else dt_util.as_utc(end)
                hour = dt_util.as_local(seg_start).replace(
                    minute=0, second=0, microsecond=0
                )
                while hour < seg_end:
                    key = hour.isoformat()
                    presence[key] = presence.get(key, False) or home
                    hour += timedelta(hours=1)
        return presence

    async def _async_presence_by_hour(
        self, start: datetime, end: datetime
    ) -> dict[str, bool]:
        """Presence gate for a learn window, read back from the recorder.

        The recorder keeps raw states only for its purge window (~10 days),
        but learning folds a day the morning after it settles, so the window
        normally fits; older hours simply stay unknown (= they learn).
        """
        sensors = self.presence_sensors
        if not sensors:
            return {}
        from homeassistant.components.recorder import get_instance, history

        states_by_entity: dict[str, list] = {}
        for entity_id in sensors:
            changes = await get_instance(self.hass).async_add_executor_job(
                history.state_changes_during_period,
                self.hass,
                start,
                end,
                entity_id,
                True,  # no_attributes — only the state matters
                False,  # descending
                None,  # limit
                True,  # include_start_time_state
            )
            states_by_entity[entity_id] = changes.get(entity_id, [])
        return self._presence_map(states_by_entity, start, end)

    async def _maybe_learn(self) -> bool:
        """Fold settled days of (temperature, device kWh) into the profiles.

        Runs once per day — and immediately when a sensor was just added to
        the configuration (its profile is empty), so a new device does not
        wait for the next day boundary to start learning.

        With presence sensors configured, only hours during which someone was
        home are folded in — the profile then models "consumption when the
        house is occupied" instead of averaging empty-house hours into it.
        A day whose data is complete still counts as observed even when every
        hour was away, so vacation days don't get re-queried forever.
        """
        today = dt_util.now().date()
        new_sensor = any(eid not in self.profiles for eid in self.sensors)
        if self._last_learn_day == today and not new_sensor:
            return False

        # {(window start, window end): hour → anyone home} — climate sensors
        # usually share the same unfolded window, so fetch presence once.
        presence_cache: dict[tuple[datetime, datetime], dict[str, bool]] = {}

        for eid in self.sensors:
            profile = self.profile_for(eid)
            candidate_days = sorted(
                {
                    d
                    for k in self._temps
                    if (d := date.fromisoformat(k[:10])) < today
                    and not profile.is_date_observed(d)
                }
            )
            if not candidate_days:
                continue

            start = dt_util.start_of_local_day(candidate_days[0])
            end = dt_util.start_of_local_day(today)
            kwh = await self.coordinator.consumption.async_range_kwh(eid, start, end)
            if (start, end) not in presence_cache:
                presence_cache[(start, end)] = await self._async_presence_by_hour(
                    start, end
                )
            presence = presence_cache[(start, end)]

            folded = 0
            away_hours = 0
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
                    # Presence gate: skip hours known to have had nobody home;
                    # unknown hours (no presence data) still learn.
                    if not presence.get(
                        self._hour_key(day_start + timedelta(hours=h)), True
                    ):
                        away_hours += 1
                        continue
                    profile.observe(temp, h, energy)
                profile.mark_date_observed(day)
                folded += 1

            if folded:
                away_note = (
                    f", pominięto {away_hours} godz. bez obecności" if away_hours else ""
                )
                self.log_info(
                    f"Profil temperaturowy {eid}: dodano {folded} dni "
                    f"(łącznie {profile.observed_days}{away_note}).",
                    extra={
                        "sensor": eid,
                        "folded_days": folded,
                        "away_hours_skipped": away_hours,
                    },
                )

        self._last_learn_day = today
        return True  # the learn cursor (and any folded days) must persist

    # ------------------------------------------------------------------
    # Forecast contribution
    # ------------------------------------------------------------------
    def forecast_kwh(self, entity_id: str, hour: datetime) -> float | None:
        """Expected kWh of one climate load for an hour, once its model is ready.

        Temperature-driven when a temperature is known for the hour; the
        device's weekly average otherwise (hours beyond the weather forecast
        horizon), so the plan's demand never silently loses the climate load.
        """
        if not self.is_ready(entity_id):
            return None
        temp = self.temperature_for(hour)
        if temp is not None:
            predicted = self.profiles[entity_id].predict(temp, hour.hour)
            if predicted is not None:
                return predicted
        weekly = self.coordinator.consumption.devices.get(entity_id)
        return weekly.value(hour.weekday(), hour.hour) if weekly else None

    def contribute(self, forecast: Forecast) -> None:
        sensors = self.sensors
        if not sensors:
            return
        active = [eid for eid in sensors if self.handles(eid)]
        learning = [eid for eid in sensors if eid not in active]
        if learning:
            self.log_info(
                "Profil temperaturowy uczy się: "
                + ", ".join(
                    f"{eid.split('.')[-1]} {self.profile_for(eid).observed_days}/{MIN_LEARN_DAYS} dni"
                    for eid in learning
                )
                + " — te urządzenia na razie planowane z profilu tygodniowego."
            )
        if not active:
            return
        total = 0.0
        hits = 0
        for slot in forecast.slots:
            slot_energy = 0.0
            for eid in active:
                energy = self.forecast_kwh(eid, slot.start)
                if energy and energy > 0:
                    slot_energy += energy
            if slot_energy > 0:
                slot.extra_load_kwh += slot_energy
                slot.tags.append("climate")
                total += slot_energy
                hits += 1
        self.log_info(
            f"Klimat ({', '.join(e.split('.')[-1] for e in active)}): "
            f"{total:.1f} kWh na {hits}h horyzontu (model temperaturowy).",
            extra={"hours": hits, "total_kwh": round(total, 2)},
        )
