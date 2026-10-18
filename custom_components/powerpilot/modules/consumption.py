"""Consumption module.

Learns an hourly household consumption profile keyed by ``(weekday, hour)`` from
the configured main consumption sensor, using Home Assistant long-term statistics
(the recorder). Separately-metered devices (AC/heating, washer, boiler, iron, …)
are broken out into their own weekly profiles, leaving a clean **base** (the
uncontrollable background load). The weather-dependent meters
(``CONF_CLIMATE_SENSORS``) are broken out here as well, whether or not they are
repeated in the device list — otherwise the climate module's temperature model
would add their demand on top of a base that still contains it.

Meters may be *nested* — a washing machine sub-meter sits inside an apartment
sub-meter which sits inside the main (Victron output) sensor. The configured
``CONF_SENSOR_PARENTS`` map describes that tree; ``hierarchy.exclusive_series``
turns it into each node's **exclusive** (own) consumption so every kWh is
counted exactly once:

    exclusive(node) = reading(node) − Σ reading(direct children)
    base            = exclusive(root)

The apartment's sub-meter is fed from the Victron or from the second meter
(``modules/supply.py``). Each learned hour uses the supply really in place
(``SupplyModule.async_past_hour_states``): on meter 2 the apartment's subtree
is not subtracted from the base, and an hour of mixed or unknown supply gives
no base sample while the apartment and its sub-meters still learn. An hour a
node has no exclusive value for is never learned as 0 kWh.

The forecast demand is reconstructed as ``base + Σ(device profiles)`` — where
each device profile now stores its *exclusive* energy — so totals telescope back
to the main reading. Because the breakdown is stored per device, a smarter
forward model (climate by temperature, calendar-driven appliances) can later
*replace* a device profile without double counting.

While supply detection is active the apartment subtree's profiles go to
``HourSlot.apartment_kwh`` instead of the demand, in every state: the
optimizer places them on the Victron or on meter 2 per the hour's supply.

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
    CONF_EV_CHARGE_METER_SENSOR,
    CONF_SENSOR_PARENTS,
    DOMAIN,
)
from ..hierarchy import ExclusiveSeries, exclusive_series, metered_sensors
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

# The recorder's short-term statistics are 5-minute buckets, which is the
# finest resolution "how long did it actually run" can be answered at.
STAT_BUCKET_MIN = 5
# Below this per-bucket flow the sensor is idling, not running. 0.01 kWh in
# 5 minutes is a 0.12 kW average — far under any real charger, far over the
# jitter of a stationary counter.
_ACTIVE_BUCKET_KWH = 0.01
_ACTIVE_KW = 0.12


# Counter steps an energy meter may move in (kWh), coarsest first; finer
# than 0.1 kWh is precise enough for hourly energy.
_COARSE_STEPS = (1.0, 0.1)
# A counter is too coarse when its typical hour is under this many steps.
_COARSE_HOUR_STEPS = 3
# Non-zero hours needed before a counter's step is judged.
_COARSE_MIN_HOURS = 24


def coarse_energy_step(changes: list[float]) -> float | None:
    """The step a too-coarse energy counter moves in, else ``None``.

    Live 2026-10-18: the garage server's socket counts in 0.1 kWh steps under a
    steady ~0.205 kWh/h load, so its hours read 0.2 or 0.3 kWh and the meter
    tree's base came out negative in the 0.3 hours. A counter is too coarse
    when every non-zero hourly change is a multiple of a step of 0.1 kWh or
    more and its typical (median) hour is under three steps — an hour is then
    off by a step, a third or more of it. Over many hours the counter still
    sums right; only the split into hours is wrong.
    """
    moving = sorted(abs(c) for c in changes if abs(c) > 1e-9)
    if len(moving) < _COARSE_MIN_HOURS:
        return None
    median = moving[len(moving) // 2]
    for step in _COARSE_STEPS:
        if all(abs(c / step - round(c / step)) < 1e-6 for c in moving):
            return step if median < _COARSE_HOUR_STEPS * step else None
    return None


class ConsumptionModule(PowerPilotModule):
    """Provides learned base + per-device consumption per hour."""

    domain = "consumption"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self.base = WeeklyAccumulator()
        self.devices: dict[str, WeeklyAccumulator] = {}
        self._store: Store | None = None
        self._last_learn_day: date | None = None
        # Meter-tree fingerprint the stored profile was learned under. When the
        # configuration changes (a sub-meter added/removed or re-parented) the
        # already-folded days attribute energy to the OLD tree — e.g. the washer's
        # kWh still sit inside the apartment profile — so the profile is reset
        # and relearned from history instead of mixing two attributions forever.
        self._topology: list | None = None
        # Hours the last learning run could not attribute (``None`` before it).
        self.learning_dropped: dict | None = None
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
            self._topology = stored.get("topology")
        await self._maybe_learn()

    async def async_update(self) -> None:
        await self._maybe_learn()
        device_count = len(self.devices)
        self.log_info(
            f"Profil: baza {self.base.observed_days} dni / {self.base.samples} próbek, urządzeń {device_count}.",
            extra={
                "base_days": self.base.observed_days,
                "base_samples": self.base.samples,
                "devices": device_count,
                "device_ids": list(self.devices.keys()),
            },
        )

    async def async_recent_kwh(self, entity_id: str, hours: int) -> dict:
        """Recent hourly kWh for a sensor (used by the chart's real-data series)."""
        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        start = now - timedelta(hours=hours + 1)
        return await self._fetch_hourly_kwh(entity_id, start, now)

    async def async_range_kwh(
        self, entity_id: str, start: datetime, end: datetime
    ) -> dict[datetime, float]:
        """Hourly kWh for an arbitrary [start, end) window (public wrapper)."""
        return await self._fetch_hourly_kwh(entity_id, start, end)

    async def async_partial_kwh(
        self, entity_id: str, start: datetime, end: datetime
    ) -> float | None:
        """Total kWh in a sub-hour ``[start, end)`` window via 5-minute statistics.

        The in-progress clock hour has no compiled *hourly* statistic yet, so the
        chart's "real so far this hour" segment reads the short-term 5-minute
        buckets instead: energy sensors add up each bucket's ``change`` (the
        window's first bucket included), power sensors integrate
        ``mean × 5min``. Returns ``None`` when the sensor is unreadable (no unit
        / no statistics), ``0.0`` when simply nothing flowed.
        """
        if end <= start:
            return 0.0
        unit = self._sensor_unit(entity_id)
        kind = self._sensor_kind(unit)
        if kind is None:
            return None
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            {entity_id},
            "5minute",
            None,
            {"mean"} if kind == "power" else {"change"},
        )
        series = rows.get(entity_id, [])
        if not series:
            return None
        if kind == "power":
            factor = _POWER_UNITS[unit]
            total = 0.0
            for row in series:
                mean = row.get("mean")
                if mean is not None:
                    total += float(mean) * factor * (5.0 / 60.0)
            return total
        # ``change`` is the energy within each bucket. Differencing cumulative
        # sums instead loses the first bucket of the window (a sum row is the
        # total at the END of its bucket), and a dip stays the negative energy
        # the recorder booked rather than being clamped away.
        changes = [float(r["change"]) for r in series if r.get("change") is not None]
        if not changes:
            return None
        return sum(changes) * _ENERGY_UNITS[unit]

    async def async_active_minutes(
        self, entity_id: str, start: datetime, end: datetime
    ) -> dict[datetime, int]:
        """``{hour_start: minutes}`` the sensor was actually flowing.

        Counted from the recorder's 5-minute buckets, so every value is a
        multiple of 5 — a measurement of how long the thing ran, at that
        resolution. Deliberately NOT derived from the hour's energy and the
        rated power: a charger that tapered would then read as having run for
        half the time it really did, and the whole point of the number is to
        catch the plan and reality disagreeing.

        An hour appears with ``0`` when it was measured and nothing ran, and is
        absent only when there are no statistics for it at all. Those are
        different answers — "the charger stayed off" versus "we cannot say" —
        and the panel shows them differently.
        """
        if end <= start:
            return {}
        unit = self._sensor_unit(entity_id)
        kind = self._sensor_kind(unit)
        if kind is None:
            return {}
        # One bucket of lead-in: an energy counter's first bucket has no
        # predecessor to difference against, and would be lost otherwise.
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start - timedelta(minutes=STAT_BUCKET_MIN),
            end,
            {entity_id},
            "5minute",
            None,
            {"mean"} if kind == "power" else {"sum"},
        )
        series = rows.get(entity_id, [])
        if not series:
            return {}

        out: dict[datetime, int] = {}

        def _hour_of(row: dict) -> datetime | None:
            hour = self._row_hour(row)
            return None if hour < start or hour >= end else hour

        def _seen(row: dict) -> None:
            """This hour has statistics — its answer is 0, not "unknown"."""
            hour = _hour_of(row)
            if hour is not None:
                out.setdefault(hour, 0)

        def _active(row: dict) -> None:
            hour = _hour_of(row)
            if hour is not None:
                out[hour] = min(60, out.get(hour, 0) + STAT_BUCKET_MIN)

        if kind == "power":
            factor = _POWER_UNITS[unit]
            for row in series:
                mean = row.get("mean")
                if mean is None:
                    continue
                _seen(row)
                if float(mean) * factor > _ACTIVE_KW:
                    _active(row)
            return out

        factor = _ENERGY_UNITS[unit]
        prev: float | None = None
        for row in series:
            total = row.get("sum")
            if total is None:
                prev = None
                continue
            total = float(total)
            if prev is not None:
                _seen(row)
                # A negative delta is a dip the recorder booked (resets never
                # show here: the sum continues across them) — not flow.
                if (total - prev) * factor > _ACTIVE_BUCKET_KWH:
                    _active(row)
            prev = total
        return out

    async def async_diagnose_sensor(
        self, entity_id: str, start: datetime, end: datetime
    ) -> dict:
        """Introspect why a sensor does/doesn't yield historical data.

        Surfaces, for the debug panel, exactly what ``_fetch_hourly_kwh`` keys
        off: the live unit + ``state_class``, the detected kind, how many
        long-term-statistics rows exist for each aggregation (so a kWh sensor
        carrying only ``mean`` — ``state_class: measurement`` instead of
        ``total_increasing`` — is obvious), and the resulting hourly kWh series.
        """
        state = self.hass.states.get(entity_id)
        unit = state.attributes.get("unit_of_measurement") if state else None
        state_class = state.attributes.get("state_class") if state else None
        kind = self._sensor_kind(unit)

        async def _rows(stat_type: str) -> int:
            stats = await get_instance(self.hass).async_add_executor_job(
                statistics_during_period,
                self.hass,
                start,
                end,
                {entity_id},
                "hour",
                None,
                {stat_type},
            )
            return len(stats.get(entity_id, []))

        series = await self._fetch_hourly_kwh(entity_id, start, end)
        samples = [
            {"hour": h.isoformat(), "kwh": round(v, 3)}
            for h, v in list(series.items())[-3:]
        ]
        return {
            "entity_id": entity_id,
            "available": state is not None,
            "state": state.state if state else None,
            "unit_of_measurement": unit,
            "state_class": state_class,
            "detected_kind": kind,
            "stat_rows_sum": await _rows("sum"),
            "stat_rows_mean": await _rows("mean"),
            "series_hours": len(series),
            "samples": samples,
        }

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
                "topology": self._topology,
            }
        )

    async def async_clear_data(self) -> None:
        """Drop the learned consumption profile (base + per-device)."""
        if self._store is not None:
            await self._store.async_remove()
        self.base = WeeklyAccumulator()
        self.devices = {}
        self._last_learn_day = None

    # ------------------------------------------------------------------
    # Learning
    # ------------------------------------------------------------------
    def _current_topology(self) -> list:
        """JSON-friendly fingerprint of the meter tree the profile learns under."""
        main_sensor = self.config.get(CONF_CONSUMPTION_SENSOR)
        device_sensors = metered_sensors(self.config)
        ev_meter = self.config.get(CONF_EV_CHARGE_METER_SENSOR)
        if ev_meter and ev_meter not in device_sensors:
            device_sensors.append(ev_meter)
        parents = self.config.get(CONF_SENSOR_PARENTS) or {}
        supply = self.coordinator.supply
        return [
            main_sensor,
            sorted(device_sensors),
            sorted((str(k), str(v)) for k, v in parents.items()),
            # Which meter is the apartment and whether its supply is detected:
            # on meter 2 the apartment's energy no longer leaves the base.
            supply.apartment_meter or None,
            supply.enabled,
        ]

    async def _maybe_learn(self) -> None:
        main_sensor = self.config.get(CONF_CONSUMPTION_SENSOR)
        if not main_sensor:
            return
        supply = self.coordinator.supply
        if not supply.history_ready:
            # Days folded now would be attributed from hourly means where the
            # supply log, still being replayed, has its own answer; and a fold
            # is never revisited. The stored profile serves meanwhile.
            _LOGGER.debug("Consumption learning waits for the apartment supply history")
            return

        # A changed meter tree invalidates every already-folded day (their kWh
        # were attributed under the old hierarchy) → drop the profile and relearn
        # the whole window below under the new tree. Days already observed would
        # otherwise be skipped by the per-date dedup and never corrected.
        topology = self._current_topology()
        topology_changed = self._topology is not None and topology != self._topology
        if topology_changed:
            self.base = WeeklyAccumulator()
            self.devices = {}
            self._last_learn_day = None
            self.log_info(
                "Zmiana hierarchii liczników — profil zużycia uczony od nowa.",
                extra={"topology": topology},
            )
        self._topology = topology

        today = dt_util.now().date()
        if self._last_learn_day == today and self.base.observed_days > 0:
            return

        learn_days = int(self.config.get(CONF_CONSUMPTION_LEARN_DAYS, 21))
        device_sensors = metered_sensors(self.config)
        # Fold the EV charge meter into the exclusive tree so historical EV
        # charging is removed from the base load (it is planned separately by the
        # EV module). It is learned but NOT contributed to demand — see
        # device_value(). Skipped if it is already a configured device.
        ev_meter = self.config.get(CONF_EV_CHARGE_METER_SENSOR)
        if ev_meter and ev_meter not in device_sensors:
            device_sensors.append(ev_meter)
        parents = self.config.get(CONF_SENSOR_PARENTS) or {}

        start = dt_util.start_of_local_day(today - timedelta(days=learn_days))
        end = dt_util.start_of_local_day(today)  # only settled past days

        main_hourly = await self._fetch_hourly_kwh(main_sensor, start, end)
        series = {main_sensor: main_hourly}
        for eid in device_sensors:
            series[eid] = await self._fetch_hourly_kwh(eid, start, end)

        # Collapse the nested meter tree into per-node exclusive energy so a
        # sub-meter that lives inside another sub-meter is counted once, with
        # the apartment attributed to the Victron only where it really fed it.
        exclusive = exclusive_series(
            main_sensor,
            device_sensors,
            parents,
            series,
            apartment=supply.apartment_meter or None,
            supply=await supply.async_past_hour_states(start, end),
        )
        self._log_dropped_hours(exclusive, learn_days)
        base_excl = exclusive.series.get(main_sensor, {})
        device_excl = {eid: exclusive.series.get(eid, {}) for eid in device_sensors}

        changed = topology_changed  # a reset must persist even with nothing to fold
        day = start.date()
        while day < today:
            if not self.base.is_date_observed(day):
                if self._fold_day(day, base_excl, device_excl):
                    changed = True
            day += timedelta(days=1)

        self._last_learn_day = today
        if changed:
            await self._async_save()

    def _fold_day(
        self,
        day: date,
        base_excl: dict[datetime, float],
        device_excl: dict[str, dict[datetime, float]],
    ) -> bool:
        """Fold one settled day's exclusive hourly data into the accumulators.

        The day counts once the base has a near-complete day. Each profile then
        learns the hours it has a value for: an hour with no exclusive value
        (no reading, a child's reading missing, or for the base a mixed or
        unknown supply) is not 0 kWh, and a device does not
        lose an hour the base had to drop.
        """
        day_hours = [
            dt_util.start_of_local_day(day) + timedelta(hours=h) for h in range(24)
        ]
        present = [h for h in day_hours if h in base_excl]
        if len(present) < 20:  # require a near-complete day
            return False

        for hour in present:
            self.base.observe(hour, base_excl[hour])
        for eid, exclusive in device_excl.items():
            acc = self.devices.setdefault(eid, WeeklyAccumulator())
            for hour in day_hours:
                if hour in exclusive:
                    acc.observe(hour, exclusive[hour])

        self.base.mark_date_observed(day)
        for acc in self.devices.values():
            acc.mark_date_observed(day)
        return True

    def _log_dropped_hours(self, exclusive: ExclusiveSeries, days: int) -> None:
        """Say which hours of the learning window the meter tree could not attribute."""
        self.learning_dropped = {
            "window_days": days,
            "supply_dropped": exclusive.supply_dropped,
            "missing_child": dict(exclusive.missing_child),
            "negative": dict(exclusive.negative),
        }
        parts: list[str] = []
        if exclusive.supply_dropped:
            parts.append(
                f"baza: {exclusive.supply_dropped} godz. przy mieszanym lub "
                "nieznanym zasilaniu mieszkania"
            )
        for node, count in exclusive.missing_child.items():
            parts.append(f"{node.split('.')[-1]}: {count} godz. bez odczytu podlicznika")
        # Negative remainders are learned as they are (meter noise around a
        # near-zero value), so they are reported, but not as skipped hours.
        kept = [
            f"{node.split('.')[-1]}: {count} godz. z podlicznikami powyżej "
            "własnego odczytu (zachowane)"
            for node, count in exclusive.negative.items()
        ]
        if not parts and not kept:
            return
        message = f"Okno uczenia profilu zużycia ({days} dni)"
        if parts:
            message += " — pominięte godziny: " + "; ".join(parts)
        if kept:
            message += (" — " if parts else ": ") + "; ".join(kept)
        message += "."
        extra = {
            "window_days": days,
            "supply_dropped": exclusive.supply_dropped,
            "missing_child": dict(exclusive.missing_child),
            "negative": dict(exclusive.negative),
        }
        if exclusive.missing_child:
            self.log_warning(message, extra=extra)
        else:
            self.log_info(message, extra=extra)

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
        """Hourly kWh as the difference of consecutive cumulative ``sum`` rows.

        The recorder already carries ``sum`` across meter resets: a
        total_increasing state falling below 90 % of the previous one starts a
        new cycle and the sum keeps growing. A smaller fall is a dip it books
        as negative energy, so a negative difference is that hour's reading,
        never a reset — dropping it made the hour look unmeasured, and the
        hours no longer added up to the meter's total.
        """
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
                out[self._row_hour(row)] = (total - prev_sum) * factor
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
        # The EV charge meter is learned (so it can be subtracted from the base)
        # but never added back to demand — EV charging is planned by the EV module,
        # not part of the household background load. Likewise the climate sensor
        # once its temperature model is ready: the climate module forecasts that
        # device from the outside temperature instead of the weekly average.
        # The apartment's subtree, while supply detection is active, is
        # forecast apart (``HourSlot.apartment_kwh``, see ``contribute``).
        apartment = self.coordinator.supply.apartment_devices()
        return self._device_value(weekday, hour, lambda eid: eid not in apartment)

    def _device_value(self, weekday: int, hour: int, keep) -> float:
        ev_meter = self.config.get(CONF_EV_CHARGE_METER_SENSOR)
        climate = self.coordinator.climate
        return sum(
            acc.value(weekday, hour) or 0.0
            for eid, acc in self.devices.items()
            if eid != ev_meter and not climate.handles(eid) and keep(eid)
        )

    def contribute(self, forecast: Forecast) -> None:
        learned = self.base.observed_days > 0
        apartment = self.coordinator.supply.apartment_devices()
        for slot in forecast.slots:
            weekday, hour = slot.start.weekday(), slot.start.hour
            slot.base_consumption_kwh += self.base_value(weekday, hour)
            if learned:
                # Add back separately-metered devices with no smarter model
                # yet; the apartment's subtree goes to its own field.
                slot.extra_load_kwh += self._device_value(
                    weekday, hour, lambda eid: eid not in apartment
                )
                slot.apartment_kwh += self._device_value(
                    weekday, hour, lambda eid: eid in apartment
                )
