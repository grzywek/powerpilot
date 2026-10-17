"""Apartment supply: is the apartment fed from the Victron (PPE 1) or meter 2?

A manual switch moves the apartment between the Victron output and the second
point of delivery. Nothing reports its position, so it is told from power
(parent spec ``docs/apartment-supply-planning.plan.md``, 2026-09-14):

* ``a`` = apartment power, ``r`` = Victron output − Σ the other sub-meters
  behind the Victron. Only ``a ≥ P_min`` decides anything: ``meter_2`` when
  ``r < share · a``, else ``victron``. The rule is proportional because the
  apartment meter and the Victron disagree by a few percent at sustained ~2 kW
  loads and report non-simultaneously: an absolute margin (``r ≥ a − 100 W``)
  gave 6-14 false meter-2 episodes in 14 days of real 30 s samples, depending on
  the sampling phase, a share of 0.5 none, while a simulated meter-2 supply was
  still detected.
* N decisive samples of the other state in a row confirm a change; otherwise
  the last confirmed state holds. Before the first confirmation it is unknown.

The pure part (:func:`classify_sample`, :class:`SupplyDetector`,
:func:`hour_supply`, :func:`classify_hours`, :func:`carry_hour_states`,
:func:`past_hour_states`) has no Home Assistant dependency.
:class:`SupplyModule` samples the configured sensors on a fixed tick and keeps
the confirmed transitions in an append-only log in its own store. At setup the
same detector first replays the recorder's state history of the inputs, on the
same grid, from the last stored transition (or from the oldest history) to now.
Hours older than the log are classified from hourly means
(:meth:`SupplyModule.async_hourly_states`) and carried forward through
undecided hours (:func:`carry_hour_states`); :meth:`SupplyModule.async_past_hour_states`
joins them with the log into one state per past hour for the meter tree
(``hierarchy.exclusive_series``). The module adds nothing to the forecast
itself: while the apartment is on meter 2 the consumption and climate modules
leave its subtree out of the Victron demand
(:meth:`SupplyModule.victron_detached_devices`), and while the state is unknown
the plan carries a reminder.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.history import state_changes_during_period
from homeassistant.components.recorder.statistics import statistics_during_period
from homeassistant.const import (
    EVENT_HOMEASSISTANT_STOP,
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_APARTMENT_METER,
    CONF_APARTMENT_POWER_SENSOR,
    CONF_CONSUMPTION_SENSOR,
    CONF_SENSOR_PARENTS,
    CONF_SUPPLY_CONFIRM_SAMPLES,
    CONF_SUPPLY_VICTRON_SHARE,
    CONF_SUPPLY_MIN_APARTMENT_W,
    CONF_VICTRON_OTHER_POWER_SENSORS,
    CONF_VICTRON_OUTPUT_POWER_SENSOR,
    DOMAIN,
    STORAGE_VERSION_SUPPLY,
    SUPPLY_HOUR_MIXED,
    SUPPLY_HOUR_UNKNOWN,
    SUPPLY_METER_2,
    SUPPLY_SOURCE_LIVE,
    SUPPLY_SOURCE_REPLAY,
    SUPPLY_VICTRON,
)
from ..hierarchy import metered_sensors, subtree
from ..models import SupplyTransition
from .base import PowerPilotModule

if TYPE_CHECKING:
    from ..coordinator import PowerPilotCoordinator

_LOGGER = logging.getLogger(__name__)

# Fixed sampling tick, not every state change: the Victron reports every few
# seconds, and N must mean a duration (≈1–1.5 min at N = 3).
SUPPLY_SAMPLE_INTERVAL = timedelta(seconds=30)

# Live sampling saves a replay checkpoint at most this often, so a restart
# replays only the downtime instead of everything since the last switch.
SUPPLY_CHECKPOINT_INTERVAL = timedelta(minutes=15)

# The replay reads the recorder's state history in windows of at most this
# length: a first run covers weeks of sensors reporting every few seconds
# (≈600 states per hour each), which is not loaded at once.
# Most recent transitions listed in the diagnostics (the total is reported too).
SUPPLY_STATUS_TRANSITIONS = 50

SUPPLY_REPLAY_CHUNK = timedelta(hours=6)

# Lower bound for "the oldest state the recorder holds" of an input.
_BEFORE_ANY_HISTORY = datetime(1970, 1, 1, tzinfo=timezone.utc)

_HOUR = timedelta(hours=1)

# Power units the inputs may report, as a factor to watts.
_WATTS_PER_UNIT = {UnitOfPower.WATT: 1.0, UnitOfPower.KILO_WATT: 1000.0}


def watts(raw: str, factor: float) -> float | None:
    """A recorded or current power state in W; ``None`` when it is not a reading."""
    if raw in (STATE_UNAVAILABLE, STATE_UNKNOWN):
        return None
    try:
        value = float(raw)
    except ValueError:
        return None
    if not math.isfinite(value):
        return None
    return value * factor


def victron_net(victron_w: float | None, others_w: Sequence[float | None]) -> float | None:
    """Victron output − the other sub-meters behind it; ``None`` when any is missing."""
    if victron_w is None or any(o is None for o in others_w):
        return None
    return victron_w - sum(o for o in others_w if o is not None)


def classify_sample(
    apartment_w: float,
    victron_net_w: float,
    min_apartment_w: float,
    victron_share: float,
) -> str | None:
    """The supply one sample points to; ``None`` = undecided (apartment below the minimum).

    Proportional: ``meter_2`` when the Victron covers less than ``victron_share``
    of the apartment's power, else ``victron``. The only implementation of the
    rule; the live tick, the replay and the hourly classification all call it.
    """
    if apartment_w < min_apartment_w:
        return None
    if victron_net_w < victron_share * apartment_w:
        return SUPPLY_METER_2
    return SUPPLY_VICTRON


class SupplyDetector:
    """Confirms supply changes from a stream of samples.

    * A sample with any input missing breaks the streak.
    * An undecided sample neither counts nor breaks it.
    * A decisive sample agreeing with the confirmed state breaks the streak and
      becomes the lower bound (``not_before``) of the next switch.
    * A decisive sample of the other state extends (or starts) the streak; the
      N-th in a row confirms, dated by the first sample of the streak.

    From unknown the same holds with ``not_before = None``; a streak of one state
    is restarted by a decisive sample of the other.
    """

    def __init__(
        self,
        min_apartment_w: float,
        victron_share: float,
        confirm_samples: int,
        state: str | None = None,
        *,
        last_decisive: datetime | None = None,
        source: str = SUPPLY_SOURCE_LIVE,
    ) -> None:
        if confirm_samples < 1:
            raise ValueError("confirm_samples must be at least 1")
        self.min_apartment_w = min_apartment_w
        self.victron_share = victron_share
        self.confirm_samples = confirm_samples
        self.state = state
        # Where the transitions it confirms come from ("live" / "replay").
        self.source = source
        # Last decisive sample of the confirmed state: the next switch's lower bound.
        self._last_decisive = last_decisive if state is not None else None
        self._streak_state: str | None = None
        self._streak_start: datetime | None = None
        self._streak = 0

    @property
    def streak(self) -> int:
        """Decisive samples of the other state seen in a row so far."""
        return self._streak

    @property
    def last_decisive(self) -> datetime | None:
        """Last decisive sample of the confirmed state: the next switch's lower bound."""
        return self._last_decisive

    def _reset_streak(self) -> None:
        self._streak_state = None
        self._streak_start = None
        self._streak = 0

    def observe(
        self, at: datetime, apartment_w: float | None, victron_net_w: float | None
    ) -> SupplyTransition | None:
        """Feed one sample; return a transition when one is confirmed."""
        if apartment_w is None or victron_net_w is None:
            self._reset_streak()
            return None
        decided = classify_sample(
            apartment_w, victron_net_w, self.min_apartment_w, self.victron_share
        )
        if decided is None:
            return None
        if decided == self.state:
            self._reset_streak()
            self._last_decisive = at
            return None
        if decided != self._streak_state:
            self._reset_streak()
            self._streak_state = decided
            self._streak_start = at
        self._streak += 1
        if self._streak < self.confirm_samples:
            return None
        streak_start = self._streak_start
        assert streak_start is not None  # set with the streak's first sample
        transition = SupplyTransition(
            state=decided,
            at=streak_start,
            not_before=self._last_decisive,
            apartment_w=apartment_w,
            victron_net_w=victron_net_w,
            source=self.source,
        )
        self.state = decided
        self._last_decisive = at
        self._reset_streak()
        return transition


def values_at(
    changes: Sequence[tuple[datetime, float | None]], instants: Sequence[datetime]
) -> list[float | None]:
    """The last known value at or before each instant, as the live tick reads it.

    ``changes`` and ``instants`` are in time order; before the first change the
    value is unknown (``None``), as is a change that is not a reading.
    """
    out: list[float | None] = []
    index = 0
    current: float | None = None
    for instant in instants:
        while index < len(changes) and changes[index][0] <= instant:
            current = changes[index][1]
            index += 1
        out.append(current)
    return out


def hour_supply(
    transitions: Sequence[SupplyTransition], covered_from: datetime | None, hour: datetime
) -> str:
    """The supply of the hour starting at ``hour`` according to the log.

    * ``unknown`` before the log was watching (``covered_from``), up to and
      including the hour of the first transition (the state before it is not
      known).
    * ``mixed`` when the hour ``[hour, hour + 1 h)`` overlaps any switch window
      ``[not_before, at]``; the switch happened somewhere inside it.
    * otherwise the state of the last transition at or before the hour.
    """
    if covered_from is None or not transitions:
        return SUPPLY_HOUR_UNKNOWN
    hour = hour.astimezone(timezone.utc)  # clock hours, whatever the zone
    first = transitions[0]
    if hour < covered_from or hour <= first.at:
        return SUPPLY_HOUR_UNKNOWN
    hour_end = hour + _HOUR
    for previous, transition in zip(transitions, transitions[1:]):
        # A window with no lower bound of its own reaches back to the previous
        # confirmation, the last moment the log knows the old state held.
        lower = transition.not_before if transition.not_before is not None else previous.at
        lower = min(lower, transition.at)
        if lower < hour_end and transition.at >= hour:
            return SUPPLY_HOUR_MIXED
    state = first.state
    for transition in transitions:
        if transition.at > hour:
            break
        state = transition.state
    return state


def classify_hours(
    means: Mapping[datetime, tuple[float | None, float | None]],
    min_apartment_w: float,
    victron_share: float,
) -> dict[datetime, str]:
    """The decided hours among past hours, from their means: (apartment W, Victron net W).

    The sample rule applied to hourly means (plan answer 2). Hours with a
    missing mean and undecided hours are no evidence and are left out; what a
    change between two decided hours means for the hours around it is
    :func:`carry_hour_states`' business. Keys are UTC hour starts.
    """
    decided: dict[datetime, str] = {}
    for hour, (apartment_w, victron_net_w) in means.items():
        if apartment_w is None or victron_net_w is None:
            continue
        state = classify_sample(apartment_w, victron_net_w, min_apartment_w, victron_share)
        if state is not None:
            decided[hour.astimezone(timezone.utc)] = state
    return decided


def _clock_hours(start: datetime, end: datetime) -> list[datetime]:
    """UTC starts of the clock hours in ``[start, end)``; ``start`` is an hour start."""
    hours: list[datetime] = []
    hour = start.astimezone(timezone.utc)
    end = end.astimezone(timezone.utc)
    while hour < end:
        hours.append(hour)
        hour += _HOUR
    return hours


def carry_hour_states(
    decided: Mapping[datetime, str], start: datetime, end: datetime
) -> dict[datetime, str]:
    """The supply of every clock hour in ``[start, end)`` from its decided hours.

    An undecided or missing hour is no evidence (plan answer 2, clarified): the
    state of a decided hour carries forward through such hours until the next
    decided hour. When that one has the other state the switch was somewhere in
    between, so every hour between them and both decided hours are ``mixed``.
    Before the first decided hour the state is ``unknown``; after the last one
    it carries to ``end``. Keys are UTC hour starts.
    """
    hours = _clock_hours(start, end)
    in_range = sorted(
        (hour.astimezone(timezone.utc), state)
        for hour, state in decided.items()
        if hours and hours[0] <= hour.astimezone(timezone.utc) <= hours[-1]
    )
    states = {hour: SUPPLY_HOUR_UNKNOWN for hour in hours}
    carried: str | None = None
    index = 0
    for hour in hours:
        if index < len(in_range) and in_range[index][0] == hour:
            carried = in_range[index][1]
            index += 1
        if carried is not None:
            states[hour] = carried
    for (earlier, before), (later, after) in zip(in_range, in_range[1:]):
        if before != after:
            hour = earlier
            while hour <= later:
                states[hour] = SUPPLY_HOUR_MIXED
                hour += _HOUR
    return states


def past_hour_states(
    start: datetime,
    end: datetime,
    decided: Mapping[datetime, str],
    transitions: Sequence[SupplyTransition],
    covered_from: datetime | None,
) -> dict[datetime, str]:
    """The supply of every clock hour in ``[start, end)``: the log, and before it the hourly states.

    Hours starting at or after ``covered_from`` are the log's
    (:func:`hour_supply`); earlier ones come from ``decided`` (the decided
    hours before the log, :func:`classify_hours`) carried forward up to the log
    (:func:`carry_hour_states`). Where the two meet, the log cannot tell the
    hours up to its first transition: a carried state agreeing with that
    transition fills them, a differing one means the switch happened between
    the last decided hour and that transition, and all those hours are
    ``mixed``. Keys are UTC hour starts.
    """
    hours = _clock_hours(start, end)
    if not hours:
        return {}
    first_hour = hours[0]
    log_start: datetime | None = None
    if covered_from is not None:
        covered = covered_from.astimezone(timezone.utc)
        log_start = covered.replace(minute=0, second=0, microsecond=0)
        if log_start < covered:
            log_start += _HOUR
    hourly_end = hours[-1] + _HOUR if log_start is None else max(log_start, first_hour)
    states = carry_hour_states(decided, first_hour, hourly_end)
    log_hours = [hour for hour in hours if log_start is not None and hour >= log_start]
    for hour in log_hours:
        states[hour] = hour_supply(transitions, covered_from, hour)
    before_log = {
        hour.astimezone(timezone.utc): state
        for hour, state in decided.items()
        if first_hour <= hour.astimezone(timezone.utc) < hourly_end
    }
    if before_log and transitions and log_start is not None:
        last_decided = max(before_log)
        carried = before_log[last_decided]
        first = transitions[0]
        agrees = carried == first.state
        hour = log_start if agrees else last_decided
        while hour <= first.at:
            states[hour] = carried if agrees else SUPPLY_HOUR_MIXED
            hour += _HOUR
    return {hour: states[hour] for hour in hours}


def _parse_moment(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"supply log needs {name!r} as an ISO timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"supply log {name!r} has no time zone: {value!r}")
    return parsed.astimezone(timezone.utc)


@dataclass(frozen=True)
class SupplyCheckpoint:
    """How far the detector got: every grid instant up to ``at`` is processed.

    Written only with no streak in progress, so resuming from it loses no
    partial streak; ``last_decisive`` is the detector's lower bound for the
    next switch at that moment.
    """

    at: datetime
    last_decisive: datetime | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "at": self.at.isoformat(),
            "last_decisive": (
                self.last_decisive.isoformat() if self.last_decisive is not None else None
            ),
        }

    @classmethod
    def from_dict(cls, data: Any) -> "SupplyCheckpoint":
        """Decode a stored checkpoint; ``ValueError`` when it is unusable."""
        if not isinstance(data, Mapping):
            raise ValueError("supply checkpoint must be a mapping")
        at = _parse_moment(data.get("at"), "checkpoint.at")
        raw = data.get("last_decisive")
        last_decisive = None if raw is None else _parse_moment(raw, "checkpoint.last_decisive")
        if last_decisive is not None and last_decisive > at:
            raise ValueError("supply checkpoint's last decisive sample is after it")
        return cls(at=at, last_decisive=last_decisive)


def decode_log(
    stored: Any,
) -> tuple[list[SupplyTransition], datetime | None, SupplyCheckpoint | None]:
    """The stored transition log, the instant it covers from and its checkpoint.

    ``ValueError`` when any part is unusable. A log written before
    ``covered_from`` existed covers from its first transition (``None`` when
    empty): the state before that one is unknown anyway. A log written before
    checkpoints existed has none.
    """
    if not isinstance(stored, Mapping):
        raise ValueError("supply log must be a mapping")
    raw = stored.get("transitions")
    if not isinstance(raw, list):
        raise ValueError("supply log has no transition list")
    transitions = [SupplyTransition.from_dict(item) for item in raw]
    for earlier, later in zip(transitions, transitions[1:]):
        if later.at < earlier.at:
            raise ValueError(f"supply log is out of order at {later.at.isoformat()}")
    covered_from: datetime | None
    if "covered_from" not in stored:
        covered_from = transitions[0].at if transitions else None
    elif stored["covered_from"] is None:
        if transitions:
            raise ValueError("supply log has transitions but no covered_from")
        covered_from = None
    else:
        covered_from = _parse_moment(stored["covered_from"], "covered_from")
        if transitions and covered_from > transitions[0].at:
            raise ValueError("supply log covers from after its first transition")
    raw_checkpoint = stored.get("checkpoint")
    if raw_checkpoint is None:
        return transitions, covered_from, None
    checkpoint = SupplyCheckpoint.from_dict(raw_checkpoint)
    if covered_from is None or checkpoint.at < covered_from:
        raise ValueError("supply checkpoint is earlier than what the log covers")
    return transitions, covered_from, checkpoint


class SupplyReplayError(Exception):
    """The recorder's history of the inputs cannot be replayed."""


class SupplyModule(PowerPilotModule):
    """Samples the supply inputs and keeps the confirmed transitions."""

    domain = "supply"

    def __init__(self, hass: HomeAssistant, coordinator: "PowerPilotCoordinator") -> None:
        super().__init__(hass, coordinator)
        self.transitions: list[SupplyTransition] = []
        # First instant the log has been watching; hours before it are not in it.
        self.covered_from: datetime | None = None
        self.detector: SupplyDetector | None = None
        # Why the stored log could not be used (the module is then inert).
        # Kept apart from ``last_error``, which every update clears.
        self.store_error: str | None = None
        # Why the history replay at setup failed (live sampling runs anyway).
        self.replay_error: str | None = None
        # Why the last save of the log failed (cleared by the next good one).
        self.save_error: str | None = None
        # How far the detector got, as last saved: a restart replays from here.
        self.checkpoint: SupplyCheckpoint | None = None
        self._last_tick_at: datetime | None = None
        self._store: Store | None = None
        self._replay_task: asyncio.Task | None = None
        self._unit_warned: set[tuple[str, str | None]] = set()
        self._missing_warned: set[str] = set()

    # ------------------------------------------------------------------
    # Configuration
    # ------------------------------------------------------------------
    @property
    def apartment_sensor(self) -> str:
        return str(self.config.get(CONF_APARTMENT_POWER_SENSOR) or "")

    @property
    def victron_sensor(self) -> str:
        return str(self.config.get(CONF_VICTRON_OUTPUT_POWER_SENSOR) or "")

    @property
    def other_sensors(self) -> list[str]:
        return [str(e) for e in (self.config.get(CONF_VICTRON_OTHER_POWER_SENSORS) or [])]

    @property
    def inputs(self) -> list[str]:
        """Apartment, Victron output, then the other sub-meters behind it."""
        return [self.apartment_sensor, self.victron_sensor, *self.other_sensors]

    @property
    def enabled(self) -> bool:
        """Detection runs only with all three sensor keys set."""
        return bool(self.apartment_sensor and self.victron_sensor and self.other_sensors)

    @property
    def apartment_meter(self) -> str:
        """The metered device that is the apartment; its subtree moves with it."""
        return str(self.config.get(CONF_APARTMENT_METER) or "")

    @property
    def active(self) -> bool:
        """The supply shapes the meter tree: detection on and the apartment's meter set."""
        return self.enabled and bool(self.apartment_meter)

    @property
    def min_apartment_w(self) -> float:
        return float(self.config[CONF_SUPPLY_MIN_APARTMENT_W])

    @property
    def victron_share(self) -> float:
        return float(self.config[CONF_SUPPLY_VICTRON_SHARE])

    @property
    def confirm_samples(self) -> int:
        return int(self.config[CONF_SUPPLY_CONFIRM_SAMPLES])

    @property
    def signal(self) -> str:
        """Dispatcher signal sent when the state or the streak changes."""
        return f"{DOMAIN}_{self.coordinator.entry.entry_id}_supply_updated"

    # ------------------------------------------------------------------
    # State
    # ------------------------------------------------------------------
    @property
    def last_transition(self) -> SupplyTransition | None:
        return self.transitions[-1] if self.transitions else None

    @property
    def state(self) -> str | None:
        """The confirmed supply; ``None`` before the first confirmation."""
        last = self.last_transition
        return last.state if last is not None else None

    @property
    def since(self) -> datetime | None:
        last = self.last_transition
        return last.at if last is not None else None

    @property
    def history_ready(self) -> bool:
        """Whether past hours can be told from the log as it will stand.

        Not before the stored log is loaded and the history replay has brought
        it up to now: hours read earlier would be classified from hourly means
        where the log has its own answer. Always ready when the supply does not
        shape the meter tree, and when the stored log is unusable (there is no
        log this run; every past hour is older than it).
        """
        return not self.active or self.detector is not None or self.store_error is not None

    def victron_detached_devices(self) -> set[str]:
        """Metered devices the Victron does not feed now: the apartment's subtree on meter 2.

        The forecast keeps the confirmed state for the whole horizon (plan,
        design 6); empty on the Victron and while the state is unknown (the
        apartment is planned on the Victron then, answer 1).
        """
        if not self.active or self.state != SUPPLY_METER_2:
            return set()
        return subtree(
            str(self.config.get(CONF_CONSUMPTION_SENSOR) or ""),
            metered_sensors(self.config),
            self.config.get(CONF_SENSOR_PARENTS) or {},
            self.apartment_meter,
        )

    def collect_reminders(self) -> list[str]:
        if self.active and self.state is None:
            return [
                "Nie wiadomo jeszcze, skąd zasilane jest mieszkanie — plan liczy "
                "jego zużycie po stronie Victrona."
            ]
        return []

    def status(self) -> dict[str, Any]:
        """Diagnostics for the panel status and the debug export."""
        return {
            "enabled": self.enabled,
            "active": self.active,
            "state": self.state,
            "since": self.since.isoformat() if self.since else None,
            "covered_from": self.covered_from.isoformat() if self.covered_from else None,
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "transitions_total": len(self.transitions),
            "transitions": [t.to_dict() for t in self.transitions[-SUPPLY_STATUS_TRANSITIONS:]],
            "store_error": self.store_error,
            "replay_error": self.replay_error,
            "save_error": self.save_error,
            "min_apartment_w": self.min_apartment_w,
            "victron_share": self.victron_share,
            "confirm_samples": self.confirm_samples,
            "apartment_sensor": self.apartment_sensor or None,
            "victron_sensor": self.victron_sensor or None,
            "other_sensors": self.other_sensors,
            "apartment_meter": self.apartment_meter or None,
        }

    def supply_for_hour(self, hour: datetime) -> str:
        """``victron`` / ``meter_2`` / ``mixed`` / ``unknown`` for the hour from ``hour``."""
        return hour_supply(self.transitions, self.covered_from, hour)

    # ------------------------------------------------------------------
    # Setup / persistence
    # ------------------------------------------------------------------
    async def async_setup(self) -> None:
        self._store = Store(
            self.hass,
            STORAGE_VERSION_SUPPLY,
            f"{DOMAIN}_{self.coordinator.entry.entry_id}_supply",
            atomic_writes=True,
        )
        if not self.enabled:
            return
        stored = await self._store.async_load()
        if stored is not None:
            try:
                self.transitions, self.covered_from, self.checkpoint = decode_log(stored)
            except ValueError as err:
                await self._async_move_store_aside(err)
                return
        # Weeks of history are read off the setup path; live sampling starts
        # once the replay has brought the detector up to now.
        self._replay_task = self.coordinator.entry.async_create_background_task(
            self.hass, self._async_replay_then_sample(), "powerpilot_supply_replay"
        )

    def _stored_detector(self, source: str) -> SupplyDetector:
        """A detector holding the stored log's last confirmed state.

        Its lower bound for the next switch is the checkpoint's when that is not
        older than the last transition; otherwise the confirmation itself, the
        last moment the log knows the state held.
        """
        last = self.last_transition
        checkpoint = self.checkpoint
        if last is None:
            last_decisive = None
        elif checkpoint is not None and checkpoint.at >= last.at:
            last_decisive = checkpoint.last_decisive
        else:
            last_decisive = last.at
        return SupplyDetector(
            self.min_apartment_w,
            self.victron_share,
            self.confirm_samples,
            state=last.state if last is not None else None,
            last_decisive=last_decisive,
            source=source,
        )

    async def _async_replay_then_sample(self) -> None:
        """Replay the recorder's history into the log, then sample live.

        A failed replay is logged and shown on the entity; the log stays as
        stored and live sampling starts from it all the same.
        """
        assert self._store is not None
        detector = self._stored_detector(SUPPLY_SOURCE_REPLAY)
        try:
            start, appended, samples, missing, last_instant = await self._async_replay(
                detector
            )
            transitions = [*self.transitions, *appended]
            covered_from = self.covered_from if self.covered_from is not None else start
            checkpoint = self.checkpoint
            if last_instant is not None and detector.streak == 0:
                checkpoint = SupplyCheckpoint(last_instant, detector.last_decisive)
            await self._store.async_save(self._payload(transitions, covered_from, checkpoint))
        except Exception as err:  # any recorder or store failure; surfaced below
            _LOGGER.exception(
                "Replaying the supply inputs' history failed; live sampling "
                "starts from the stored log"
            )
            self.replay_error = (
                f"Nie udało się odtworzyć historii zasilania mieszkania z recordera "
                f"({err}). Wykrywanie działa od teraz; wcześniejsze godziny "
                f"zostaną odtworzone przy następnym uruchomieniu, o ile recorder "
                f"jeszcze je przechowuje."
            )
            self.log_warning(self.replay_error)
            detector = self._stored_detector(SUPPLY_SOURCE_LIVE)
        else:
            self.transitions = transitions
            self.covered_from = covered_from
            self.checkpoint = checkpoint
            self.log_info(
                f"Odtworzono historię zasilania mieszkania: {len(appended)} "
                f"przejść, {samples} próbek, w tym {missing} bez kompletu odczytów.",
                extra={
                    "from": start.isoformat() if start is not None else None,
                    "transitions": len(appended),
                    "samples": samples,
                    "missing_samples": missing,
                },
            )
            detector.source = SUPPLY_SOURCE_LIVE
        self.detector = detector
        entry = self.coordinator.entry
        entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_tick, SUPPLY_SAMPLE_INTERVAL
            )
        )
        # Home Assistant does not unload entries when it stops: both save.
        entry.async_on_unload(self._async_save_checkpoint_now)
        entry.async_on_unload(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self._async_on_stop)
        )
        async_dispatcher_send(self.hass, self.signal)

    async def _async_move_store_aside(self, err: ValueError) -> None:
        """Keep an undecodable log for inspection and refuse to save over it.

        Home Assistant's ``Store`` itself moves a file that is not valid JSON
        aside; this covers valid JSON this module cannot decode. The module
        stays inert for this run — no sampling, no save — so the bad file is
        never replaced by an empty log.
        """
        assert self._store is not None
        path = self._store.path
        aside = f"{path}.corrupt-{dt_util.utcnow():%Y-%m-%dT%H%M%S}"
        try:
            await self.hass.async_add_executor_job(os.rename, path, aside)
        except OSError as move_err:
            _LOGGER.error(
                "Supply log %s cannot be decoded (%s) nor moved aside (%s); "
                "apartment supply detection stays off and the file is left as is",
                path,
                err,
                move_err,
            )
            self.store_error = (
                f"Dziennik zasilania mieszkania jest nieczytelny ({err}) i nie dało "
                f"się go odłożyć ({move_err}). Wykrywanie wyłączone — popraw lub "
                f"usuń plik {path} i uruchom ponownie."
            )
        else:
            _LOGGER.warning(
                "Supply log %s cannot be decoded (%s); moved to %s as corrupt. "
                "Apartment supply detection stays off until the next restart",
                path,
                err,
                aside,
            )
            self.store_error = (
                f"Dziennik zasilania mieszkania był nieczytelny ({err}) i został "
                f"odłożony do {aside}. Wykrywanie wstrzymane do restartu; po nim "
                f"dziennik zacznie się od nowa."
            )
        self.log_warning(self.store_error)

    @staticmethod
    def _payload(
        transitions: Sequence[SupplyTransition],
        covered_from: datetime | None,
        checkpoint: SupplyCheckpoint | None,
    ) -> dict[str, Any]:
        return {
            "transitions": [t.to_dict() for t in transitions],
            "covered_from": covered_from.isoformat() if covered_from is not None else None,
            "checkpoint": checkpoint.to_dict() if checkpoint is not None else None,
        }

    async def _async_save(self) -> None:
        """Save the log from live sampling; a failure is reported, sampling goes on.

        What failed to save stays in memory and goes out with the next save.
        """
        assert self._store is not None
        try:
            await self._store.async_save(
                self._payload(self.transitions, self.covered_from, self.checkpoint)
            )
        except Exception as err:  # any store failure; surfaced below
            _LOGGER.exception("Saving the supply log failed; sampling goes on")
            self.save_error = (
                f"Nie udało się zapisać dziennika zasilania mieszkania ({err}). "
                "Wykrywanie działa dalej; zapis zostanie ponowiony przy następnej "
                "zmianie lub punkcie kontrolnym."
            )
            self.log_warning(self.save_error)
        else:
            self.save_error = None

    async def _async_save_checkpoint_now(self) -> None:
        """Checkpoint the last live sample on unload or stop, unless mid-streak."""
        detector = self.detector
        at = self._last_tick_at
        if self._store is None or detector is None or at is None or detector.streak:
            return
        if self.checkpoint is not None and self.checkpoint.at >= at:
            return
        self.checkpoint = SupplyCheckpoint(at, detector.last_decisive)
        await self._async_save()

    async def _async_on_stop(self, _event: Event) -> None:
        await self._async_save_checkpoint_now()

    async def async_clear_data(self) -> None:
        # A replay still running would save the old log back after the wipe.
        if self._replay_task is not None and not self._replay_task.done():
            self._replay_task.cancel()
            with suppress(asyncio.CancelledError):
                await self._replay_task
        if self._store is not None:
            await self._store.async_remove()
        self.transitions = []
        self.covered_from = None
        self.checkpoint = None
        self._last_tick_at = None
        self.store_error = None
        self.replay_error = None
        self.save_error = None
        if self.detector is not None:
            self.detector = SupplyDetector(
                self.min_apartment_w, self.victron_share, self.confirm_samples
            )

    # ------------------------------------------------------------------
    # Units
    # ------------------------------------------------------------------
    def _unit_factor(self, entity_id: str, unit: str | None) -> float | None:
        """Watts per unit of ``unit``; ``None`` (warned once) when it is not power."""
        factor = _WATTS_PER_UNIT.get(unit)
        if factor is None and (entity_id, unit) not in self._unit_warned:
            self._unit_warned.add((entity_id, unit))
            self.log_warning(
                f"{entity_id}: jednostka {unit!r} nie jest mocą (W/kW) — "
                "próbka pominięta. Wskaż czujnik mocy w ustawieniach "
                "zasilania mieszkania.",
                extra={"entity_id": entity_id, "unit": unit},
            )
            _LOGGER.warning(
                "Supply input %s reports unit %r, not W/kW; its samples are skipped",
                entity_id,
                unit,
            )
        return factor

    def _history_factor(self, entity_id: str) -> float | None:
        """Watts per unit of the recorded history of ``entity_id``.

        History is read without attributes; the unit is the current state's
        (an unavailable state keeps its unit). ``None`` when there is no
        current state or its unit is not power.
        """
        state = self.hass.states.get(entity_id)
        if state is None:
            if entity_id not in self._missing_warned:
                self._missing_warned.add(entity_id)
                self.log_warning(
                    f"{entity_id}: brak encji — nie wiadomo, w jakiej jednostce "
                    "jest jej historia. Wskaż istniejący czujnik mocy w "
                    "ustawieniach zasilania mieszkania.",
                    extra={"entity_id": entity_id},
                )
                _LOGGER.warning(
                    "Supply input %s has no state; its history cannot be read", entity_id
                )
            return None
        return self._unit_factor(entity_id, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------
    def _read_watts(self, entity_id: str) -> float | None:
        """Current power of ``entity_id`` in W; ``None`` when not usable now."""
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        value = watts(state.state, 1.0)
        if value is None:
            return None
        factor = self._unit_factor(entity_id, state.attributes.get(ATTR_UNIT_OF_MEASUREMENT))
        return None if factor is None else value * factor

    def _sample(self) -> tuple[float | None, float | None]:
        apartment_w = self._read_watts(self.apartment_sensor)
        victron_w = self._read_watts(self.victron_sensor)
        others = [self._read_watts(e) for e in self.other_sensors]
        return apartment_w, victron_net(victron_w, others)

    async def _async_tick(self, now: datetime) -> None:
        if self.detector is None or self._store is None:
            return
        at = dt_util.as_utc(now)
        if self.covered_from is None:
            # Nothing was replayed: the log watches from its first live sample.
            self.covered_from = at
        streak_before = self.detector.streak
        apartment_w, victron_net_w = self._sample()
        transition = self.detector.observe(at, apartment_w, victron_net_w)
        self._last_tick_at = at
        if transition is not None:
            self.transitions.append(transition)
        # Never mid-streak: resuming from the checkpoint must lose no partial streak.
        checkpoint_due = self.detector.streak == 0 and (
            transition is not None
            or self.checkpoint is None
            or at - self.checkpoint.at >= SUPPLY_CHECKPOINT_INTERVAL
        )
        if checkpoint_due:
            self.checkpoint = SupplyCheckpoint(at, self.detector.last_decisive)
        if transition is not None or checkpoint_due:
            await self._async_save()
        if transition is not None:
            self.log_info(
                f"Zasilanie mieszkania: {transition.state} od {transition.at.isoformat()}",
                extra=transition.to_dict(),
            )
        if transition is not None or self.detector.streak != streak_before:
            async_dispatcher_send(self.hass, self.signal)

    # ------------------------------------------------------------------
    # History
    # ------------------------------------------------------------------
    async def _async_replay(
        self, detector: SupplyDetector
    ) -> tuple[datetime | None, list[SupplyTransition], int, int, datetime | None]:
        """Feed the recorder's history of the inputs through ``detector``.

        The live tick's grid (30 s), sampled as the tick samples: at each
        instant the last known state of every input. From the later of the
        checkpoint and the last stored transition — every transition found is
        new by construction, and with a checkpoint only the downtime is read —
        or, with neither, from the oldest instant the recorder holds all
        inputs, to now. Returns the first replayed instant (``None`` when there
        was no history), the confirmed transitions, the sample counts (all, and
        those with an input missing) and the last replayed instant.
        """
        inputs = self.inputs
        factors = {entity_id: self._history_factor(entity_id) for entity_id in inputs}
        unusable = [entity_id for entity_id, factor in factors.items() if factor is None]
        if unusable:
            raise SupplyReplayError(
                f"unit of {', '.join(unusable)} unknown or not W/kW"
            )
        resume = [
            moment
            for moment in (
                self.last_transition.at if self.last_transition is not None else None,
                self.checkpoint.at if self.checkpoint is not None else None,
            )
            if moment is not None
        ]
        if resume:
            start: datetime | None = max(resume)
        else:
            oldest = [await self._async_oldest_state(entity_id) for entity_id in inputs]
            start = None if any(o is None for o in oldest) else max(
                o for o in oldest if o is not None
            )
        appended: list[SupplyTransition] = []
        samples = missing = 0
        last_instant: datetime | None = None
        if start is None:
            return None, appended, samples, missing, last_instant
        instant = start
        while True:
            # Re-read the clock each window: the replay itself takes time, and
            # it catches up to the moment live sampling takes over.
            now = dt_util.utcnow()
            if instant >= now:
                break
            window_end = min(instant + SUPPLY_REPLAY_CHUNK, now)
            instants: list[datetime] = []
            while instant < window_end:
                instants.append(instant)
                instant += SUPPLY_SAMPLE_INTERVAL
            values = []
            for entity_id in inputs:
                # From one step before the window: the API leaves out a state
                # written exactly at its start, and the first instant needs the
                # state in force at it.
                changes = await self._async_state_history(
                    entity_id,
                    instants[0] - SUPPLY_SAMPLE_INTERVAL,
                    window_end,
                    factors[entity_id],
                )
                values.append(values_at(changes, instants))
            for index, at in enumerate(instants):
                apartment_w = values[0][index]
                net_w = victron_net(values[1][index], [v[index] for v in values[2:]])
                samples += 1
                if apartment_w is None or net_w is None:
                    missing += 1
                transition = detector.observe(at, apartment_w, net_w)
                if transition is not None:
                    appended.append(transition)
                last_instant = at
        return start, appended, samples, missing, last_instant

    async def _async_oldest_state(self, entity_id: str) -> datetime | None:
        """When the recorder's oldest state of ``entity_id`` was written."""
        changes = await get_instance(self.hass).async_add_executor_job(
            state_changes_during_period,
            self.hass,
            _BEFORE_ANY_HISTORY,
            dt_util.utcnow(),
            entity_id,
            True,  # no_attributes
            False,  # descending: the oldest first
            1,  # limit
            False,  # include_start_time_state
        )
        states = changes.get(entity_id) or []
        return dt_util.as_utc(states[0].last_updated) if states else None

    async def _async_state_history(
        self, entity_id: str, start: datetime, end: datetime, factor: float | None
    ) -> list[tuple[datetime, float | None]]:
        """``(time, W)`` state changes of ``entity_id`` after ``start``, before ``end``.

        Led by the state already in force at ``start``; a state that is not a
        reading is ``None`` (a missing input from then on).
        """
        assert factor is not None
        changes = await get_instance(self.hass).async_add_executor_job(
            state_changes_during_period,
            self.hass,
            start,
            end,
            entity_id,
            True,  # no_attributes: the unit is the current state's
            False,  # descending
            None,  # limit
            True,  # include_start_time_state
        )
        return [
            (dt_util.as_utc(state.last_updated), watts(state.state, factor))
            for state in changes.get(entity_id) or []
        ]

    async def async_past_hour_states(
        self, start: datetime, end: datetime
    ) -> dict[datetime, str]:
        """The supply of every clock hour in ``[start, end)``, for learning and the charts.

        Without detection or without the apartment's meter every hour is
        ``victron`` (today's tree). Otherwise :func:`past_hour_states` over the
        log and, for the hours before it, the hourly classification of the
        mean statistics — read only when the window reaches back before the
        log, and then up to where the log begins. Keys are UTC hour starts.
        """
        if not self.active:
            return {hour: SUPPLY_VICTRON for hour in _clock_hours(start, end)}
        covered_from = self.covered_from
        decided: dict[datetime, str] = {}
        if covered_from is None or start < covered_from:
            decided = await self.async_hourly_states(
                start, end if covered_from is None else covered_from
            )
        return past_hour_states(start, end, decided, self.transitions, covered_from)

    async def async_hourly_states(self, start: datetime, end: datetime) -> dict[datetime, str]:
        """Supply of the hours in ``[start, end)`` older than the log, from hourly means.

        Reads the inputs' hourly ``mean`` statistics; an input without a mean
        for an hour makes that hour's mean missing. Only the decided hours
        (:func:`classify_hours`) before ``covered_from`` are returned (all of
        them while the log covers nothing yet). Keys are UTC hour starts.
        """
        inputs = self.inputs
        factors = {entity_id: self._history_factor(entity_id) for entity_id in inputs}
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period,
            self.hass,
            start,
            end,
            set(inputs),
            "hour",
            None,
            {"mean"},
        )
        per_input: list[dict[datetime, float]] = []
        for entity_id in inputs:
            factor = factors[entity_id]
            series: dict[datetime, float] = {}
            if factor is not None:
                for row in rows.get(entity_id) or []:
                    mean = row.get("mean")
                    if mean is None or not math.isfinite(mean):
                        continue
                    series[_row_start(row)] = float(mean) * factor
            per_input.append(series)
        hours = set().union(*per_input)
        means = {
            hour: (
                per_input[0].get(hour),
                victron_net(per_input[1].get(hour), [s.get(hour) for s in per_input[2:]]),
            )
            for hour in hours
        }
        classified = classify_hours(means, self.min_apartment_w, self.victron_share)
        covered_from = self.covered_from
        return {
            hour: state
            for hour, state in sorted(classified.items())
            if covered_from is None or hour < covered_from
        }


def _row_start(row: Mapping[str, Any]) -> datetime:
    """A statistics row's start as an aware UTC datetime."""
    start = row["start"]
    if isinstance(start, (int, float)):
        return dt_util.utc_from_timestamp(start)
    return dt_util.as_utc(start)
