"""Apartment supply: is the apartment fed from the Victron (PPE 1) or meter 2?

A manual switch moves the apartment between the Victron output and the second
point of delivery. Nothing reports its position, so it is told from power
(parent spec ``docs/apartment-supply-planning.plan.md``, 2026-09-14):

* ``a`` = apartment power, ``r`` = Victron output − Σ the other sub-meters
  behind the Victron. Only ``a ≥ P_min`` decides anything: ``victron`` when
  ``r ≥ a − margin``, else ``meter_2``.
* N decisive samples of the other state in a row confirm a change; otherwise
  the last confirmed state holds. Before the first confirmation it is unknown.

The pure part (:func:`classify_sample`, :class:`SupplyDetector`) has no Home
Assistant dependency. :class:`SupplyModule` samples the configured sensors on a
fixed tick and keeps the confirmed transitions in an append-only log in its own
store. It contributes nothing to the forecast yet.
"""

from __future__ import annotations

import logging
import math
import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

from homeassistant.const import (
    ATTR_UNIT_OF_MEASUREMENT,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    UnitOfPower,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import (
    CONF_APARTMENT_POWER_SENSOR,
    CONF_SUPPLY_CONFIRM_SAMPLES,
    CONF_SUPPLY_MARGIN_W,
    CONF_SUPPLY_MIN_APARTMENT_W,
    CONF_VICTRON_OTHER_POWER_SENSORS,
    CONF_VICTRON_OUTPUT_POWER_SENSOR,
    DOMAIN,
    STORAGE_VERSION_SUPPLY,
    SUPPLY_METER_2,
    SUPPLY_SOURCE_LIVE,
    SUPPLY_VICTRON,
)
from ..models import SupplyTransition
from .base import PowerPilotModule

if TYPE_CHECKING:
    from ..coordinator import PowerPilotCoordinator

_LOGGER = logging.getLogger(__name__)

# Fixed sampling tick, not every state change: the Victron reports every few
# seconds, and N must mean a duration (≈1–1.5 min at N = 3).
SUPPLY_SAMPLE_INTERVAL = timedelta(seconds=30)

# Power units the inputs may report, as a factor to watts.
_WATTS_PER_UNIT = {UnitOfPower.WATT: 1.0, UnitOfPower.KILO_WATT: 1000.0}


def classify_sample(
    apartment_w: float, victron_net_w: float, min_apartment_w: float, margin_w: float
) -> str | None:
    """The supply one sample points to; ``None`` = undecided (apartment below the minimum)."""
    if apartment_w < min_apartment_w:
        return None
    return SUPPLY_VICTRON if victron_net_w >= apartment_w - margin_w else SUPPLY_METER_2


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
        margin_w: float,
        confirm_samples: int,
        state: str | None = None,
        *,
        last_decisive: datetime | None = None,
        source: str = SUPPLY_SOURCE_LIVE,
    ) -> None:
        if confirm_samples < 1:
            raise ValueError("confirm_samples must be at least 1")
        self.min_apartment_w = min_apartment_w
        self.margin_w = margin_w
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
            apartment_w, victron_net_w, self.min_apartment_w, self.margin_w
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


def decode_log(stored: Any) -> list[SupplyTransition]:
    """The stored transition log; ``ValueError`` when any part is unusable."""
    if not isinstance(stored, Mapping):
        raise ValueError("supply log must be a mapping")
    raw = stored.get("transitions")
    if not isinstance(raw, list):
        raise ValueError("supply log has no transition list")
    transitions = [SupplyTransition.from_dict(item) for item in raw]
    for earlier, later in zip(transitions, transitions[1:]):
        if later.at < earlier.at:
            raise ValueError(f"supply log is out of order at {later.at.isoformat()}")
    return transitions


class SupplyModule(PowerPilotModule):
    """Samples the supply inputs and keeps the confirmed transitions."""

    domain = "supply"

    def __init__(self, hass: HomeAssistant, coordinator: "PowerPilotCoordinator") -> None:
        super().__init__(hass, coordinator)
        self.transitions: list[SupplyTransition] = []
        self.detector: SupplyDetector | None = None
        # Why the stored log could not be used (the module is then inert).
        # Kept apart from ``last_error``, which every update clears.
        self.store_error: str | None = None
        self._store: Store | None = None
        self._unit_warned: set[tuple[str, str | None]] = set()

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
    def enabled(self) -> bool:
        """Detection runs only with all three sensor keys set."""
        return bool(self.apartment_sensor and self.victron_sensor and self.other_sensors)

    @property
    def min_apartment_w(self) -> float:
        return float(self.config[CONF_SUPPLY_MIN_APARTMENT_W])

    @property
    def margin_w(self) -> float:
        return float(self.config[CONF_SUPPLY_MARGIN_W])

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
                self.transitions = decode_log(stored)
            except ValueError as err:
                await self._async_move_store_aside(err)
                return
        last = self.last_transition
        self.detector = SupplyDetector(
            self.min_apartment_w,
            self.margin_w,
            self.confirm_samples,
            state=last.state if last is not None else None,
            # The confirmation is the last moment the log knows the state held.
            last_decisive=last.at if last is not None else None,
        )
        self.coordinator.entry.async_on_unload(
            async_track_time_interval(
                self.hass, self._async_tick, SUPPLY_SAMPLE_INTERVAL
            )
        )

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

    def _payload(self) -> dict[str, Any]:
        return {"transitions": [t.to_dict() for t in self.transitions]}

    async def async_clear_data(self) -> None:
        if self._store is not None:
            await self._store.async_remove()
        self.transitions = []
        self.store_error = None
        if self.detector is not None:
            self.detector = SupplyDetector(
                self.min_apartment_w, self.margin_w, self.confirm_samples
            )

    # ------------------------------------------------------------------
    # Sampling
    # ------------------------------------------------------------------
    def _read_watts(self, entity_id: str) -> float | None:
        """Current power of ``entity_id`` in W; ``None`` when not usable now."""
        state = self.hass.states.get(entity_id)
        if state is None or state.state in (STATE_UNAVAILABLE, STATE_UNKNOWN):
            return None
        try:
            value = float(state.state)
        except ValueError:
            return None
        if not math.isfinite(value):
            return None
        unit = state.attributes.get(ATTR_UNIT_OF_MEASUREMENT)
        factor = _WATTS_PER_UNIT.get(unit)
        if factor is None:
            if (entity_id, unit) not in self._unit_warned:
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
            return None
        return value * factor

    def _sample(self) -> tuple[float | None, float | None]:
        apartment_w = self._read_watts(self.apartment_sensor)
        victron_w = self._read_watts(self.victron_sensor)
        others = [self._read_watts(e) for e in self.other_sensors]
        if victron_w is None or any(o is None for o in others):
            return apartment_w, None
        return apartment_w, victron_w - sum(o for o in others if o is not None)

    async def _async_tick(self, now: datetime) -> None:
        if self.detector is None or self._store is None:
            return
        streak_before = self.detector.streak
        apartment_w, victron_net_w = self._sample()
        transition = self.detector.observe(dt_util.as_utc(now), apartment_w, victron_net_w)
        if transition is not None:
            self.transitions.append(transition)
            await self._store.async_save(self._payload())
            self.log_info(
                f"Zasilanie mieszkania: {transition.state} od {transition.at.isoformat()}",
                extra=transition.to_dict(),
            )
        if transition is not None or self.detector.streak != streak_before:
            async_dispatcher_send(self.hass, self.signal)
