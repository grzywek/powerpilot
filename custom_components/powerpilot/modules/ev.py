"""EV module.

Computes how much energy the car needs and when it is available to charge, then
exposes a structured :class:`EVRequest` the optimizer can schedule into the
cheapest hours (respecting the phase shared with the inverter).

Three calendar-driven inputs feed the optimizer (events come from every
calendar configured in the integration-wide ``CONF_CALENDARS`` list, read by
the calendar module — which the registry updates *before* this one):

* **Deadline targets** — a calendar event ``"<keyword> 100%"`` spanning e.g.
  12:00–13:00 means *"the EV must be at 100 % SoC by 12:00"*. The optimizer is
  free to pick the cheapest available hours before that deadline.
* **Forced windows** — a bare calendar event ``"<keyword>"`` means *"charge at
  full power for the event's hours"* (manual choice, no SoC limit).
* **Trips** — events with a non-home ``location``. The calendar module turns
  them into away windows (event span extended by Google-Maps travel time plus
  the configured margins): those hours are *unavailable* for charging, the
  round trip drains the pack (learned kWh/km), and each trip adds an automatic
  deadline target — be at ``min SoC + round-trip energy`` before departure so
  the car always makes the trip with the safety reserve intact.

With no keyword events the module falls back to topping the car up to the
target SoC (from the target-SoC sensor, or :data:`DEFAULT_TARGET_SOC`) in the
cheapest available hours — the original Stage-0 behaviour.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CAPACITY_LEARN_DAYS,
    CONF_EV_BATTERY_KWH,
    CONF_EV_CALENDAR_KEYWORD,
    CONF_EV_CHARGER_KW,
    CONF_EV_CHARGER_PHASE,
    CONF_EV_CHARGER_PHASES,
    CONF_EV_CHARGING_SENSOR,
    CONF_EV_CONTIGUOUS_MAX_EXTRA_PCT,
    CONF_EV_EARLY_MAX_EXTRA_PCT,
    CONF_EV_ENABLED,
    CONF_EV_ENERGY_ADDED_SENSOR,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_ODOMETER_SENSOR,
    CONF_EV_PREFER_CONTIGUOUS,
    CONF_EV_PREFER_EARLY,
    CONF_EV_PRESENCE_ENTITIES,
    CONF_EV_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
    DRAIN_HORIZON_HOURS,
    DRAIN_LEARN_DAYS,
    EV_MIN_SOC_DEFAULT,
    EV_TARGET_SOC_DEFAULT,
    MAX_CAPACITY_SAMPLES,
    MIN_CAPACITY_SAMPLES,
    MIN_SESSION_KWH,
    MIN_SESSION_SOC,
    MIN_TRIP_KM,
    STORAGE_VERSION_EV,
)
from ..models import Forecast
from ..profiles import WeeklyAccumulator
from .base import PowerPilotModule
from .calendar import CalendarEvent, Trip

_LOGGER = logging.getLogger(__name__)

DEFAULT_TARGET_SOC = EV_TARGET_SOC_DEFAULT
DEFAULT_MIN_SOC = EV_MIN_SOC_DEFAULT
HOME_STATES = {"home", "on", "true", "connected"}
CHARGING_STATES = {"on", "true", "charging"}

# Matches a percentage anywhere in the event-summary remainder, e.g. "100%",
# "80 %", "55,5%".
_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


def _hour_floor(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


def _hours_between(start: datetime, end: datetime) -> list[datetime]:
    """Hour-bucket starts overlapping ``[start, end)`` (at least the start hour)."""
    if end <= start:
        return [_hour_floor(start)]
    out: list[datetime] = []
    hour = _hour_floor(start)
    while hour < end:
        out.append(hour)
        hour += timedelta(hours=1)
    return out


def _spread_energy(start: datetime, end: datetime, kwh: float) -> dict[datetime, float]:
    """Spread ``kwh`` evenly across the hour buckets of ``[start, end)``."""
    if kwh <= 0:
        return {}
    hours = _hours_between(start, end)
    per_hour = kwh / len(hours)
    return {hour: per_hour for hour in hours}


def _trip_energy_distance_km(trip: Trip) -> float | None:
    """Total resolved driving distance represented by a trip."""
    if trip.outbound_distance_km is not None or trip.return_distance_km is not None:
        return (trip.outbound_distance_km or 0.0) + (trip.return_distance_km or 0.0)
    if trip.distance_km is None:
        return None
    return 2.0 * trip.distance_km


def _value_at(history: list[tuple[datetime, float]], when: datetime) -> float | None:
    """Value of a (time, value) series at ``when`` (last sample at/before it)."""
    found: float | None = None
    for ts, value in history:
        if ts <= when:
            found = value
        else:
            break
    if found is None and history:
        found = history[0][1]  # nothing before → earliest known value
    return found


def _segment_sessions(
    energy_history: list[tuple[datetime, float]],
) -> list[tuple[datetime, datetime, float]]:
    """Split a session energy counter into ``(start, end, kWh)`` charging runs.

    The energy-added sensor is ``total_increasing`` and resets to ~0 at the start
    of each session, so a downward jump marks a new session; the energy delivered
    by a run is its peak value.
    """
    if not energy_history:
        return []
    sessions: list[tuple[datetime, datetime, float]] = []
    run_start = energy_history[0][0]
    run_max = energy_history[0][1]
    prev = energy_history[0][1]
    last_ts = energy_history[0][0]
    for ts, value in energy_history[1:]:
        if value < prev - 0.05:  # counter reset → previous session ended
            sessions.append((run_start, last_ts, run_max))
            run_start, run_max = ts, value
        else:
            run_max = max(run_max, value)
        prev, last_ts = value, ts
    sessions.append((run_start, last_ts, run_max))
    return sessions


def _capacity_samples(
    soc_history: list[tuple[datetime, float]],
    energy_history: list[tuple[datetime, float]],
) -> list[float]:
    """Per-session capacity (kWh) estimates: ``energy_added ÷ ΔSoC% × 100``.

    Only clean sessions count — enough energy and a big enough SoC swing that
    sensor noise doesn't dominate.
    """
    if len(soc_history) < 2 or len(energy_history) < 2:
        return []
    samples: list[float] = []
    for start, end, energy in _segment_sessions(energy_history):
        if energy < MIN_SESSION_KWH:
            continue
        soc_start = _value_at(soc_history, start)
        soc_end = _value_at(soc_history, end)
        if soc_start is None or soc_end is None:
            continue
        delta_soc = soc_end - soc_start
        if delta_soc < MIN_SESSION_SOC:
            continue
        samples.append(energy / delta_soc * 100.0)
    return samples


def _hourly_drain(
    soc_history: list[tuple[datetime, float]], capacity: float
) -> dict[datetime, float]:
    """``{hour_start: kWh out of the pack}`` from SoC drops, by the start hour."""
    out: dict[datetime, float] = {}
    if capacity <= 0 or len(soc_history) < 2:
        return out
    prev_ts, prev = soc_history[0]
    for ts, soc in soc_history[1:]:
        if soc < prev:
            hour = dt_util.as_local(prev_ts).replace(minute=0, second=0, microsecond=0)
            out[hour] = out.get(hour, 0.0) + (prev - soc) / 100.0 * capacity
        prev_ts, prev = ts, soc
    return out


def _kwh_per_km(
    soc_history: list[tuple[datetime, float]],
    odometer_history: list[tuple[datetime, float]],
    capacity: float,
) -> float | None:
    """Average consumption: total SoC-drop energy ÷ distance driven."""
    if capacity <= 0 or len(soc_history) < 2 or len(odometer_history) < 2:
        return None
    km = odometer_history[-1][1] - odometer_history[0][1]
    if km < MIN_TRIP_KM:
        return None
    energy_out = sum(_hourly_drain(soc_history, capacity).values())
    if energy_out <= 0:
        return None
    return energy_out / km


@dataclass
class EVChargeTarget:
    """A deadline by which the EV must reach ``target_soc`` (%).

    ``source`` distinguishes explicit keyword events (``"calendar"``) from
    automatic pre-trip requirements (``"trip"``) — trip targets are a *floor*
    (make the trip with the reserve intact), not a charge ceiling.
    """

    deadline: datetime
    target_soc: float
    label: str = ""
    source: str = "calendar"


@dataclass
class EVRequest:
    """Structured EV charging need passed to the optimizer."""

    enabled: bool = False
    required_kwh: float = 0.0
    charger_kw: float = 3.5
    phase: int = 1
    phases: int = 1
    battery_kwh: float = 60.0
    current_soc: float | None = None
    available_hours: set[datetime] = field(default_factory=set)
    # Calendar-driven plans.
    forced_hours: set[datetime] = field(default_factory=set)
    targets: list[EVChargeTarget] = field(default_factory=list)
    # Predicted energy out of the pack per hour (kWh) — trip driving. The
    # optimizer subtracts this from the projected EV SoC line and the allocator
    # compensates for it when sizing deadline targets.
    drain_kwh: dict[datetime, float] = field(default_factory=dict)
    # Safety reserve (%) the plan should never dip the car below.
    min_soc: float = 0.0
    # Placement preferences (see const.py CONF_EV_PREFER_*): trade a bounded
    # % of extra cost for an unbroken charging block / an earlier finish.
    prefer_contiguous: bool = False
    contiguous_max_extra_pct: float = 15.0
    prefer_early: bool = False
    early_max_extra_pct: float = 10.0

    @property
    def charger_power_kw(self) -> float:
        """Total charger draw (kW) = per-phase power × number of phases."""
        return max(self.charger_kw, 0.0) * max(self.phases, 1)

    @property
    def is_actionable(self) -> bool:
        return (
            self.enabled
            and self.battery_kwh > 0  # capacity learned → planning allowed
            and bool(self.available_hours)
            and (self.required_kwh > 0 or bool(self.targets) or bool(self.forced_hours))
        )


class EVModule(PowerPilotModule):
    """Provides the EV charging request and home-availability."""

    domain = "ev"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._soc: float | None = None
        self._energy_added: float | None = None
        self._home: bool | None = None
        self._charging: bool | None = None
        self._targets: list[EVChargeTarget] = []
        self._trip_targets: list[EVChargeTarget] = []
        self._forced_hours: set[datetime] = set()
        # Hours the car is away from home (calendar trips: located events
        # extended by travel time + margins) — the only source of
        # *unavailability*; absent that, every forecast hour is chargeable.
        self._unavailable_hours: set[datetime] = set()
        # Predicted per-hour drive drain (kWh) from calendar trips.
        self._trip_drain: dict[datetime, float] = {}
        self._request = EVRequest()
        # The integration's own writable target-SoC / min-SoC entities (see
        # number.py). Set by NumberEntity.async_added_to_hass; read-only here.
        self.target_soc_entity = None
        self.min_soc_entity = None
        # Learned battery capacity (kWh) — see _maybe_learn_capacity.
        self._capacity: float | None = None
        self._capacity_samples: list[float] = []
        self._capacity_source: str | None = None  # "learned" | "seed"
        self._last_capacity_learn: date | None = None
        self._store: Store | None = None
        # Learned driving consumption — kWh/km + a 7×24 drain profile (kWh out
        # of the pack per hour) that anticipates routine driving.
        self._kwh_per_km: float | None = None
        self._drain_profile = WeeklyAccumulator()
        self._last_drain_learn: date | None = None

    @property
    def enabled(self) -> bool:
        return bool(self.config.get(CONF_EV_ENABLED))

    @property
    def target_soc(self) -> float | None:
        """Charge target (%) from the integration's own number entity."""
        entity = self.target_soc_entity
        return entity.native_value if entity is not None else None

    @property
    def min_soc(self) -> float:
        """Safety reserve (%) from the integration's own number entity."""
        entity = self.min_soc_entity
        value = entity.native_value if entity is not None else None
        return float(value) if value is not None else DEFAULT_MIN_SOC

    async def async_setup(self) -> None:
        """Load the learned capacity, seed it from legacy config, refine it."""
        self._store = Store(
            self.hass,
            STORAGE_VERSION_EV,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_ev",
        )
        stored = await self._store.async_load()
        if stored:
            self._capacity_samples = list(stored.get("capacity_samples") or [])
            self._capacity = stored.get("capacity")
            self._capacity_source = stored.get("capacity_source")
            last = stored.get("last_capacity_learn")
            self._last_capacity_learn = date.fromisoformat(last) if last else None
            self._kwh_per_km = stored.get("kwh_per_km")
            self._drain_profile = WeeklyAccumulator.from_dict(stored.get("drain_profile"))
            drain_last = stored.get("last_drain_learn")
            self._last_drain_learn = date.fromisoformat(drain_last) if drain_last else None
        # One-time seed from the old manual capacity so upgrades aren't blank
        # while the first sessions are still being observed.
        if self._capacity is None:
            legacy = self.config.get(CONF_EV_BATTERY_KWH)
            if legacy:
                self._capacity = float(legacy)
                self._capacity_source = "seed"
        if self.enabled:
            await self._maybe_learn()

    async def async_update(self) -> None:
        if not self.enabled:
            self._request = EVRequest(enabled=False)
            self._targets = []
            self._trip_targets = []
            self._forced_hours = set()
            self._trip_drain = {}
            self.log_info("EV wyłączony w konfiguracji.")
            return

        # Live SoC only — never fabricate or carry forward a stale value. When
        # the sensor is unavailable the SoC forecast simply isn't drawn (same
        # hard rule as the house battery), rather than projecting from a guess.
        self._soc = self._read_float(self.config.get(CONF_EV_SOC_SENSOR))
        self._energy_added = self._read_float(
            self.config.get(CONF_EV_ENERGY_ADDED_SENSOR)
        )
        self._home = self._combined_home()
        self._charging = self._read_bool(
            self.config.get(CONF_EV_CHARGING_SENSOR), CHARGING_STATES
        )

        # Learn first: trip drain/targets below depend on the freshest capacity
        # and kWh/km estimates.
        await self._maybe_learn()
        self._load_calendar_plans()

        self.log_info(
            f"EV: SoC={self._soc if self._soc is not None else '–'}%, "
            f"pojemność={self._capacity if self._capacity is not None else '–'} kWh "
            f"({self._capacity_source or 'brak'}, {len(self._capacity_samples)} sesji), "
            f"zużycie={self._kwh_per_km if self._kwh_per_km is not None else '–'} kWh/km "
            f"({self._drain_profile.observed_days} dni), "
            f"cel={self.target_soc if self.target_soc is not None else '–'}%, "
            f"w domu={self._home}, ładuje={self._charging}, "
            f"godziny niedostępne={len(self._unavailable_hours)}, "
            f"deadline'y={len(self._targets)}, wyjazdy={len(self._trip_targets)}, "
            f"godziny ręczne={len(self._forced_hours)}.",
            extra={
                "soc": self._soc,
                "target_soc": self.target_soc,
                "min_soc": self.min_soc,
                "energy_added_kwh": self._energy_added,
                "home": self._home,
                "charging": self._charging,
                "unavailable_hours": len(self._unavailable_hours),
                "targets": len(self._targets),
                "trip_targets": len(self._trip_targets),
                "forced_hours": len(self._forced_hours),
                "trip_drain_kwh": round(sum(self._trip_drain.values()), 2),
            },
        )

    # ------------------------------------------------------------------
    # Battery capacity + driving-consumption learning
    # ------------------------------------------------------------------
    @property
    def capacity_kwh(self) -> float | None:
        """Best capacity estimate (learned median, or legacy seed, or None)."""
        return self._capacity

    def predicted_drain_kwh(self, hours: int = DRAIN_HORIZON_HOURS) -> float | None:
        """Expected driving drain (kWh) over the next ``hours`` from the profile."""
        if self._drain_profile.observed_days == 0:
            return None
        now = dt_util.now()
        total = 0.0
        for i in range(hours):
            moment = now + timedelta(hours=i)
            total += self._drain_profile.value(moment.weekday(), moment.hour) or 0.0
        return total

    async def _maybe_learn(self) -> None:
        """Re-derive capacity + driving consumption from history (once per day)."""
        today = dt_util.now().date()
        capacity_done = (
            self._capacity_source == "learned" and self._last_capacity_learn == today
        )
        drain_done = self._last_drain_learn == today
        if capacity_done and drain_done:
            return
        soc_eid = self.config.get(CONF_EV_SOC_SENSOR)
        if not soc_eid:
            return
        now = dt_util.now()
        window_start = now - timedelta(days=max(CAPACITY_LEARN_DAYS, DRAIN_LEARN_DAYS))
        soc_hist = await self._numeric_history(soc_eid, window_start, now)

        # Capacity from charging sessions (needs the energy-added counter).
        energy_eid = self.config.get(CONF_EV_ENERGY_ADDED_SENSOR)
        if energy_eid and not capacity_done:
            energy_hist = await self._numeric_history(energy_eid, window_start, now)
            self._last_capacity_learn = today
            samples = _capacity_samples(soc_hist, energy_hist)
            if samples:
                self._capacity_samples = samples[-MAX_CAPACITY_SAMPLES:]
                if len(self._capacity_samples) >= MIN_CAPACITY_SAMPLES:
                    self._capacity = round(statistics.median(self._capacity_samples), 1)
                    self._capacity_source = "learned"

        # Driving consumption (needs the odometer and a known capacity).
        odo_eid = self.config.get(CONF_EV_ODOMETER_SENSOR)
        if odo_eid and self._capacity and not drain_done:
            odo_hist = await self._numeric_history(odo_eid, window_start, now)
            kpk = _kwh_per_km(soc_hist, odo_hist, self._capacity)
            if kpk:
                self._kwh_per_km = round(kpk, 4)
            self._fold_drain_days(soc_hist, self._capacity, today)
            self._last_drain_learn = today

        await self._async_save()

    def _fold_drain_days(
        self, soc_hist: list[tuple[datetime, float]], capacity: float, today: date
    ) -> None:
        """Fold each settled day's hourly drain into the 7×24 profile (once)."""
        drops = _hourly_drain(soc_hist, capacity)
        covered = {dt_util.as_local(ts).date() for ts, _ in soc_hist}
        for day in covered:
            if day >= today or self._drain_profile.is_date_observed(day):
                continue
            day_start = dt_util.start_of_local_day(day)
            for offset in range(24):
                hour = day_start + timedelta(hours=offset)
                self._drain_profile.observe(hour, drops.get(hour, 0.0))
            self._drain_profile.mark_date_observed(day)

    async def _numeric_history(
        self, entity_id: str, start: datetime, end: datetime
    ) -> list[tuple[datetime, float]]:
        """Sorted ``(time, value)`` numeric state history for a sensor."""
        from homeassistant.components.recorder import get_instance, history

        changes = await get_instance(self.hass).async_add_executor_job(
            history.state_changes_during_period,
            self.hass,
            start,
            end,
            entity_id,
            True,  # no_attributes
            False,  # descending
            None,  # limit
            True,  # include_start_time_state
        )
        series: list[tuple[datetime, float]] = []
        for st in changes.get(entity_id, []):
            try:
                series.append((st.last_updated, float(st.state)))
            except (TypeError, ValueError):
                continue  # "unavailable" / "unknown"
        series.sort(key=lambda item: item[0])
        return series

    async def _async_save(self) -> None:
        if self._store is None:
            return
        await self._store.async_save(
            {
                "capacity": self._capacity,
                "capacity_samples": self._capacity_samples,
                "capacity_source": self._capacity_source,
                "last_capacity_learn": self._last_capacity_learn.isoformat()
                if self._last_capacity_learn
                else None,
                "kwh_per_km": self._kwh_per_km,
                "drain_profile": self._drain_profile.to_dict(),
                "last_drain_learn": self._last_drain_learn.isoformat()
                if self._last_drain_learn
                else None,
            }
        )

    # ------------------------------------------------------------------
    # Sensor reads
    # ------------------------------------------------------------------
    def _read_float(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _read_bool(self, entity_id: str | None, true_states: set[str]) -> bool | None:
        """Tri-state read: ``None`` when not configured/unavailable."""
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        value = str(state.state).lower()
        if value in ("unknown", "unavailable", "none", ""):
            return None
        return value in true_states

    def _read_presence(self, entity_id: str | None) -> bool | None:
        """Tri-state presence read: ``None`` when unset/unknown/unavailable.

        Unlike a plain HOME_STATES check, an unavailable tracker reads as
        *unknown* — not as "away" — so a flaky entity can't poison the
        combined answer below.
        """
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        value = str(state.state).lower()
        if value in ("unknown", "unavailable", "none", ""):
            return None
        return value in HOME_STATES

    def _combined_home(self) -> bool | None:
        """Combine the car tracker with the extra presence entities.

        Car trackers often poll rarely, so a fresh "not home" from any
        configured presence entity beats a stale "home" from the car: away
        wins, home requires every known reading to agree. ``None`` when no
        entity has a usable state.
        """
        readings = [self._read_presence(self.config.get(CONF_EV_LOCATION_SENSOR))]
        for entity_id in self.config.get(CONF_EV_PRESENCE_ENTITIES) or []:
            readings.append(self._read_presence(entity_id))
        known = [r for r in readings if r is not None]
        if not known:
            return None
        return all(known)

    # ------------------------------------------------------------------
    # Calendar (events + trips come from the calendar module, updated first)
    # ------------------------------------------------------------------
    def calendar_fingerprint(self) -> tuple:
        """Stable digest of the calendar-derived charging inputs.

        The coordinator compares this between planning cycles: a change means
        an event was added/edited/removed, which justifies releasing the
        frozen current-hour decision so charging can start mid-hour.
        """
        return (
            tuple(
                sorted(
                    (t.deadline.isoformat(), t.target_soc, t.source)
                    for t in (*self._targets, *self._trip_targets)
                )
            ),
            tuple(sorted(h.isoformat() for h in self._forced_hours)),
            tuple(sorted(h.isoformat() for h in self._unavailable_hours)),
        )

    def _load_calendar_plans(self) -> None:
        """Turn the calendar module's events/trips into charging inputs."""
        self._targets = []
        self._trip_targets = []
        self._forced_hours = set()
        self._unavailable_hours = set()
        self._trip_drain = {}

        calendar = self.coordinator.calendar
        now = dt_util.now()
        keyword = str(
            self.config.get(CONF_EV_CALENDAR_KEYWORD)
            or DEFAULTS[CONF_EV_CALENDAR_KEYWORD]
        ).strip()

        for event in calendar.events:
            self._parse_keyword_event(event, keyword, now)

        for trip in calendar.trips:
            self._apply_trip(trip, now)

    def _parse_keyword_event(
        self, event: CalendarEvent, keyword: str, now: datetime
    ) -> None:
        summary = event.summary
        if not summary or not keyword:
            return
        if not summary.lower().startswith(keyword.lower()):
            return

        remainder = summary[len(keyword) :].strip()
        match = _PERCENT_RE.search(remainder)
        if match:
            # Deadline target: be at <percent> by the event start.
            if event.start <= now:
                return  # deadline already passed — nothing to schedule
            try:
                percent = float(match.group(1).replace(",", "."))
            except ValueError:
                return
            percent = max(0.0, min(100.0, percent))
            self._targets.append(
                EVChargeTarget(deadline=event.start, target_soc=percent, label=summary)
            )
            return

        # Forced window: charge at full power for every hour the event covers.
        hour = max(event.start, now).replace(minute=0, second=0, microsecond=0)
        while hour < event.end:
            self._forced_hours.add(hour)
            hour += timedelta(hours=1)

    def _apply_trip(self, trip: Trip, now: datetime) -> None:
        """One trip → unavailable hours, drive drain and a pre-departure target.

        The away window (depart → return) can't be planned into (overrides
        forced/deadline windows too: a self-contradictory calendar just means
        no charging happens). The round trip drains the pack via the learned
        kWh/km; without that model (or without Google-Maps distance) the trip
        stays unavailability-only and a warning is logged instead of guessing.
        """
        # 1. Unavailability — every hour the away window touches.
        hour = max(trip.depart, now).replace(minute=0, second=0, microsecond=0)
        while hour < trip.return_end:
            self._unavailable_hours.add(hour)
            hour += timedelta(hours=1)

        # 2. Drive energy: each resolved leg is spread over its own span. Older
        # tests/records may only carry ``distance_km``; for live trips the
        # calendar module fills the explicit outbound/return distances.
        has_explicit_legs = (
            trip.outbound_distance_km is not None or trip.return_distance_km is not None
        )
        if has_explicit_legs:
            outbound_km = trip.outbound_distance_km
            return_km = trip.return_distance_km
        else:
            outbound_km = trip.distance_km
            return_km = trip.distance_km
        if outbound_km is None and return_km is None:
            return  # calendar module already logged the missing travel model
        if not self._kwh_per_km:
            self.log_warning(
                f"Wyjazd „{trip.label}”: brak nauczonego zużycia kWh/km — "
                "energia dojazdu nieuwzględniona w prognozie SoC.",
                extra={
                    "trip": trip.label,
                    "outbound_km": outbound_km,
                    "return_km": return_km,
                },
            )
            return
        outbound_kwh = (outbound_km or 0.0) * self._kwh_per_km
        return_kwh = (return_km or 0.0) * self._kwh_per_km
        for bucket, kwh in _spread_energy(
            trip.depart, trip.event_start, outbound_kwh
        ).items():
            self._trip_drain[bucket] = self._trip_drain.get(bucket, 0.0) + kwh
        for bucket, kwh in _spread_energy(
            trip.event_end, trip.return_end, return_kwh
        ).items():
            self._trip_drain[bucket] = self._trip_drain.get(bucket, 0.0) + kwh

        # 3. Pre-departure target: reserve + round trip must be in the pack.
        if trip.depart <= now or not self._capacity:
            return
        needed_soc = self.min_soc + (outbound_kwh + return_kwh) / self._capacity * 100.0
        self._trip_targets.append(
            EVChargeTarget(
                deadline=trip.depart,
                target_soc=min(100.0, needed_soc),
                label=f"Wyjazd: {trip.label}",
                source="trip",
            )
        )

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------
    def get_request(self, forecast: Forecast) -> EVRequest:
        if not self.enabled:
            return EVRequest(enabled=False)

        # Capacity is learned from charging sessions; until it's known the request
        # is not actionable (battery_kwh = 0 → EVRequest.is_actionable is False).
        battery_kwh = self._capacity or 0.0

        available_hours = {
            slot.start.replace(minute=0, second=0, microsecond=0)
            for slot in forecast.slots
        } - self._unavailable_hours

        # Explicit keyword plans take over routine sizing; automatic trip
        # targets do NOT — they are a floor on top of normal behaviour, so the
        # routine top-up keeps running alongside them.
        if self._targets or self._forced_hours:
            required_kwh = 0.0
        else:
            target_soc = (
                self.target_soc if self.target_soc is not None else DEFAULT_TARGET_SOC
            )
            current_soc = self._soc if self._soc is not None else target_soc
            current_energy = current_soc / 100.0 * battery_kwh
            predicted = self.predicted_drain_kwh()
            if predicted is not None and battery_kwh > 0:
                # Charge to cover the next look-ahead of predicted driving plus
                # the safety-reserve floor — never above the car's own target
                # SoC. Learned consumption drives routine charging.
                target_energy = min(
                    predicted + self.min_soc / 100.0 * battery_kwh,
                    target_soc / 100.0 * battery_kwh,
                )
                required_kwh = max(0.0, target_energy - current_energy)
            else:
                # No drain profile yet → top up to the target SoC as before.
                required_kwh = max(0.0, target_soc / 100.0 * battery_kwh - current_energy)

        self._request = EVRequest(
            enabled=True,
            required_kwh=required_kwh,
            charger_kw=float(self.config.get(CONF_EV_CHARGER_KW, 3.5)),
            phase=int(self.config.get(CONF_EV_CHARGER_PHASE, 1)),
            phases=int(self.config.get(CONF_EV_CHARGER_PHASES, 1)),
            battery_kwh=battery_kwh,
            current_soc=self._soc,
            available_hours=available_hours,
            forced_hours=set(self._forced_hours),
            targets=[*self._targets, *self._trip_targets],
            drain_kwh=dict(self._trip_drain),
            min_soc=self.min_soc,
            prefer_contiguous=bool(self.config.get(CONF_EV_PREFER_CONTIGUOUS)),
            contiguous_max_extra_pct=float(
                self.config.get(CONF_EV_CONTIGUOUS_MAX_EXTRA_PCT, 15.0) or 0.0
            ),
            prefer_early=bool(self.config.get(CONF_EV_PREFER_EARLY)),
            early_max_extra_pct=float(
                self.config.get(CONF_EV_EARLY_MAX_EXTRA_PCT, 10.0) or 0.0
            ),
        )
        return self._request

    def collect_reminders(self) -> list[str]:
        if not self.enabled:
            return []
        reminders: list[str] = []
        need = (
            self._request.required_kwh > 0
            or bool(self._request.targets)
            or bool(self._request.forced_hours)
        )
        # "Plug in" only makes sense when the car is actually home and idle —
        # away from home there's nothing the user can do about it right now.
        if need and self._home is not False and self._charging is False:
            reminders.append("Podłącz samochód — zaplanowane jest ładowanie EV.")
        # Plan-vs-reality: a forced window is due this hour but the charger is
        # idle, or it's charging somewhere other than home (doesn't realize
        # the home charging plan the optimizer priced in).
        now_hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        due_now = (
            now_hour in self._request.forced_hours
            and now_hour in self._request.available_hours
        )
        if due_now and self._charging is False:
            reminders.append(
                "EV powinien się teraz ładować (okno z kalendarza), "
                "ale ładowarka nie pobiera mocy."
            )
        elif due_now and self._charging and self._home is False:
            reminders.append(
                "EV ładuje się poza domem — to nie jest zaplanowana sesja z prognozy."
            )
        reminders.extend(self._deadline_feasibility_reminders())
        return reminders

    def _deadline_feasibility_reminders(self) -> list[str]:
        """Warn when a deadline target physically can't be reached in time.

        Compares the energy still missing to the target against what the
        charger can deliver in the available hours before the deadline. Needs
        the live SoC and a known capacity — without them there is nothing to
        compare against.
        """
        request = self._request
        if (
            not request.enabled
            or request.current_soc is None
            or request.battery_kwh <= 0
        ):
            return []
        power = request.charger_power_kw
        if power <= 0:
            return []
        out: list[str] = []
        current_kwh = request.current_soc / 100.0 * request.battery_kwh
        for target in sorted(request.targets, key=lambda t: t.deadline):
            hours_before = [
                h for h in request.available_hours if h < target.deadline
            ]
            achievable_kwh = len(hours_before) * power
            drain_before = sum(
                kwh for hour, kwh in request.drain_kwh.items()
                if hour < target.deadline
            )
            need_kwh = (
                target.target_soc / 100.0 * request.battery_kwh
                - (current_kwh - drain_before)
            )
            if need_kwh > achievable_kwh + 0.05:
                label = target.label or target.deadline.isoformat()
                out.append(
                    f"EV nie zdąży osiągnąć {target.target_soc:.0f}% przed "
                    f"„{label}” — brakuje "
                    f"{need_kwh - achievable_kwh:.1f} kWh mocy ładowania w "
                    f"dostępnych godzinach."
                )
        return out

    def plan_summary(self) -> dict:
        """Serialisable EV plan/telemetry snapshot for the panel."""
        now_hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        return {
            "enabled": self.enabled,
            "available": now_hour not in self._unavailable_hours,
            "home": self._home,
            "soc": self._soc,
            "target_soc": self.target_soc,
            "soc_limit": self.soc_limit_now(),
            "energy_added_kwh": self._energy_added,
            "charging": self._charging,
            "charger_power_kw": self.charger_power_kw,
            "capacity_kwh": self._capacity,
            "capacity_source": self._capacity_source,
            "capacity_sessions": len(self._capacity_samples),
            "capacity_ready": self._capacity is not None,
            "min_capacity_sessions": MIN_CAPACITY_SAMPLES,
            "kwh_per_km": self._kwh_per_km,
            "drain_days": self._drain_profile.observed_days,
            "drain_next24_kwh": self.predicted_drain_kwh(24),
            "min_soc": self.min_soc,
            "targets": [
                {
                    "deadline": target.deadline.isoformat(),
                    "target_soc": target.target_soc,
                    "label": target.label,
                    "source": target.source,
                }
                for target in sorted(
                    [*self._targets, *self._trip_targets], key=lambda t: t.deadline
                )
            ],
            "forced_hours": [hour.isoformat() for hour in sorted(self._forced_hours)],
            # Away windows (trips) for the chart shading + the EV card.
            "trips": self.trips_payload(),
        }

    def trips_payload(self) -> list[dict]:
        """Current calendar trips, serialised for the panel and for snapshot
        records (so past events stay visible after leaving the calendar).
        Energy is the round trip out of the pack (``None`` without a model)."""
        return [
            {
                "label": trip.label,
                "location": trip.location,
                "event_start": trip.event_start.isoformat(),
                "event_end": trip.event_end.isoformat(),
                "depart": trip.depart.isoformat(),
                "return_end": trip.return_end.isoformat(),
                "distance_km": trip.distance_km,
                "duration_min": trip.duration_min,
                "origin_location": trip.origin_location,
                "return_location": trip.return_location,
                "outbound_distance_km": trip.outbound_distance_km,
                "return_distance_km": trip.return_distance_km,
                "outbound_duration_min": trip.outbound_duration_min,
                "return_duration_min": trip.return_duration_min,
                "energy_kwh": (
                    round(distance * self._kwh_per_km, 2)
                    if self._kwh_per_km
                    and (distance := _trip_energy_distance_km(trip)) is not None
                    else None
                ),
            }
            for trip in self.coordinator.calendar.trips
        ]

    def request_debug(self) -> dict:
        """The exact EVRequest last fed to the optimizer — the allocator inputs.

        Everything needed to reproduce ``_plan_ev`` offline: live SoC, pack size,
        per-phase power × phases, the resulting full charge power, the top-up
        deficit, calendar targets/forced windows and how many hours the car was
        deemed available for.
        """
        r = self._request
        avail = sorted(r.available_hours)
        return {
            "enabled": r.enabled,
            "is_actionable": r.is_actionable,
            "current_soc": r.current_soc,
            "battery_kwh": r.battery_kwh,
            "charger_kw": r.charger_kw,
            "phases": r.phases,
            "charger_power_kw": round(r.charger_power_kw, 3),
            "required_kwh": round(r.required_kwh, 3),
            "available_hours_count": len(avail),
            "available_from": avail[0].isoformat() if avail else None,
            "available_to": avail[-1].isoformat() if avail else None,
            "forced_hours": [h.isoformat() for h in sorted(r.forced_hours)],
            "min_soc": r.min_soc,
            "drain_total_kwh": round(sum(r.drain_kwh.values()), 3),
            "drain_hours": {
                h.isoformat(): round(kwh, 3) for h, kwh in sorted(r.drain_kwh.items())
            },
            "targets": [
                {
                    "deadline": t.deadline.isoformat(),
                    "target_soc": t.target_soc,
                    "label": t.label,
                    "source": t.source,
                }
                for t in sorted(r.targets, key=lambda t: t.deadline)
            ],
        }

    def soc_limit_now(self) -> float | None:
        """The SoC (%) the car should be allowed to charge to right now.

        A bare calendar window means "charge with no limit" → 100 %. With
        keyword deadline targets the soonest upcoming one sets the ceiling.
        Otherwise the integration's own target-SoC entity (or the built-in
        default) applies — raised when an upcoming trip needs more than that
        (trip targets are a floor, never a cap).
        """
        if not self.enabled:
            return None
        now_hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        if now_hour in self._forced_hours:
            return 100.0
        if self._targets:
            upcoming = sorted(self._targets, key=lambda t: t.deadline)
            return upcoming[0].target_soc
        if self._forced_hours:
            return 100.0
        base = self.target_soc if self.target_soc is not None else DEFAULT_TARGET_SOC
        if self._trip_targets:
            base = max(base, max(t.target_soc for t in self._trip_targets))
        return base

    @property
    def charger_power_kw(self) -> float:
        """Total charger draw (kW) at full power across all phases."""
        per_phase = float(self.config.get(CONF_EV_CHARGER_KW, 3.5))
        phases = int(self.config.get(CONF_EV_CHARGER_PHASES, 1) or 1)
        return per_phase * max(phases, 1)

    @property
    def soc(self) -> float | None:
        return self._soc

    @property
    def home(self) -> bool | None:
        return self._home

    @property
    def charging(self) -> bool | None:
        return self._charging
