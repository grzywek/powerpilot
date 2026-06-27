"""EV module.

Computes how much energy the car needs and when it is available to charge, then
exposes a structured :class:`EVRequest` the optimizer can schedule into the
cheapest hours (respecting the phase shared with the inverter).

Two charging modes feed the optimizer:

* **Deadline targets** — a calendar event ``"<keyword> 100%"`` spanning e.g.
  12:00–13:00 means *"the EV must be at 100 % SoC by 12:00"*. The optimizer is
  free to pick the cheapest available hours before that deadline.
* **Forced windows** — a bare calendar event ``"<keyword>"`` means *"charge at
  full power for the event's hours"* (manual choice, no SoC limit).

With no calendar events the module falls back to topping the car up to the
target SoC (from the target-SoC sensor, or :data:`DEFAULT_TARGET_SOC`) in the
cheapest available hours — the original Stage-0 behaviour.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from ..const import (
    CONF_EV_BATTERY_KWH,
    CONF_EV_CALENDAR,
    CONF_EV_CALENDAR_KEYWORD,
    CONF_EV_CHARGER_CONNECTED_SENSOR,
    CONF_EV_CHARGER_KW,
    CONF_EV_CHARGER_PHASE,
    CONF_EV_CHARGER_PHASES,
    CONF_EV_CHARGING_SENSOR,
    CONF_EV_ENABLED,
    CONF_EV_ENERGY_ADDED_SENSOR,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_RANGE_KM,
    CONF_EV_SOC_SENSOR,
    CONF_EV_TARGET_SOC_SENSOR,
    CONF_EV_WEEKLY_KM,
    DEFAULTS,
)
from ..models import Forecast
from .base import PowerPilotModule

_LOGGER = logging.getLogger(__name__)

DEFAULT_TARGET_SOC = 80.0
HOME_STATES = {"home", "on", "true", "connected"}
CONNECTED_STATES = {"on", "true", "connected", "home", "plugged", "plugged_in"}
CHARGING_STATES = {"on", "true", "charging"}

# How far ahead calendar events are read (matches the optimizer horizon cap).
CALENDAR_LOOKAHEAD_HOURS = 96

# Matches a percentage anywhere in the event-summary remainder, e.g. "100%",
# "80 %", "55,5%".
_PERCENT_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")


@dataclass
class EVChargeTarget:
    """A deadline by which the EV must reach ``target_soc`` (%)."""

    deadline: datetime
    target_soc: float
    label: str = ""


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

    @property
    def charger_power_kw(self) -> float:
        """Total charger draw (kW) = per-phase power × number of phases."""
        return max(self.charger_kw, 0.0) * max(self.phases, 1)

    @property
    def is_actionable(self) -> bool:
        return (
            self.enabled
            and bool(self.available_hours)
            and (self.required_kwh > 0 or bool(self.targets) or bool(self.forced_hours))
        )


class EVModule(PowerPilotModule):
    """Provides the EV charging request and home-availability."""

    domain = "ev"

    def __init__(self, hass, coordinator) -> None:
        super().__init__(hass, coordinator)
        self._soc: float | None = None
        self._target_soc: float | None = None
        self._energy_added: float | None = None
        self._connected: bool | None = None
        self._charging: bool | None = None
        self._available: bool = False
        self._targets: list[EVChargeTarget] = []
        self._forced_hours: set[datetime] = set()
        self._request = EVRequest()

    @property
    def enabled(self) -> bool:
        return bool(self.config.get(CONF_EV_ENABLED))

    async def async_update(self) -> None:
        if not self.enabled:
            self._request = EVRequest(enabled=False)
            self._targets = []
            self._forced_hours = set()
            self.log_info("EV wyłączony w konfiguracji.")
            return

        self._soc = self._read_float(self.config.get(CONF_EV_SOC_SENSOR))
        self._target_soc = self._read_float(self.config.get(CONF_EV_TARGET_SOC_SENSOR))
        self._energy_added = self._read_float(
            self.config.get(CONF_EV_ENERGY_ADDED_SENSOR)
        )
        self._connected = self._read_bool(
            self.config.get(CONF_EV_CHARGER_CONNECTED_SENSOR), CONNECTED_STATES
        )
        self._charging = self._read_bool(
            self.config.get(CONF_EV_CHARGING_SENSOR), CHARGING_STATES
        )
        self._available = self._compute_available()

        await self._async_load_calendar()

        self.log_info(
            f"EV: SoC={self._soc if self._soc is not None else '–'}%, "
            f"cel={self._target_soc if self._target_soc is not None else '–'}%, "
            f"podłączony={self._connected}, ładuje={self._charging}, "
            f"dostępny={self._available}, deadline'y={len(self._targets)}, "
            f"godziny ręczne={len(self._forced_hours)}.",
            extra={
                "soc": self._soc,
                "target_soc": self._target_soc,
                "energy_added_kwh": self._energy_added,
                "connected": self._connected,
                "charging": self._charging,
                "available": self._available,
                "targets": len(self._targets),
                "forced_hours": len(self._forced_hours),
            },
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

    def _read_home(self, entity_id: str | None) -> bool | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        return str(state.state).lower() in HOME_STATES

    def _compute_available(self) -> bool:
        """Whether the EV can charge now.

        The plug status is the stronger signal (a connected charger means the
        car is home and ready); when configured it decides on its own. Otherwise
        the location tracker decides. With neither configured we assume the car
        is available.
        """
        if self._connected is not None:
            return self._connected
        home = self._read_home(self.config.get(CONF_EV_LOCATION_SENSOR))
        if home is not None:
            return home
        return True

    # ------------------------------------------------------------------
    # Calendar
    # ------------------------------------------------------------------
    async def _async_load_calendar(self) -> None:
        """Parse the configured calendar into deadline targets + forced windows."""
        self._targets = []
        self._forced_hours = set()

        cal_entity = self.config.get(CONF_EV_CALENDAR)
        if not cal_entity:
            return

        now = dt_util.now()
        end = now + timedelta(hours=CALENDAR_LOOKAHEAD_HOURS)
        events = await self._async_fetch_events(cal_entity, now, end)
        keyword = str(
            self.config.get(CONF_EV_CALENDAR_KEYWORD)
            or DEFAULTS[CONF_EV_CALENDAR_KEYWORD]
        ).strip()

        for event in events:
            self._parse_event(event, keyword, now)

    async def _async_fetch_events(
        self, cal_entity: str, start: datetime, end: datetime
    ) -> list[dict]:
        """Read events via the public ``calendar.get_events`` service.

        Returns ``[]`` (and logs) when the calendar entity is unavailable — there
        is no alternative source, so the plan simply runs without calendar input.
        """
        try:
            response = await self.hass.services.async_call(
                "calendar",
                "get_events",
                {
                    "entity_id": cal_entity,
                    "start_date_time": start.isoformat(),
                    "end_date_time": end.isoformat(),
                },
                blocking=True,
                return_response=True,
            )
        except Exception as err:  # noqa: BLE001 - entity/service may be missing
            self.log_warning(
                f"Nie udało się odczytać kalendarza {cal_entity}: {err}.",
                extra={"calendar": cal_entity},
            )
            return []

        data = (response or {}).get(cal_entity) or {}
        return list(data.get("events") or [])

    def _parse_event(self, event: dict, keyword: str, now: datetime) -> None:
        summary = str(event.get("summary") or "").strip()
        if not summary or not keyword:
            return
        if not summary.lower().startswith(keyword.lower()):
            return

        remainder = summary[len(keyword) :].strip()
        bounds = self._event_bounds(event)
        if bounds is None:
            return
        start, end = bounds

        match = _PERCENT_RE.search(remainder)
        if match:
            # Deadline target: be at <percent> by the event start.
            if start <= now:
                return  # deadline already passed — nothing to schedule
            try:
                percent = float(match.group(1).replace(",", "."))
            except ValueError:
                return
            percent = max(0.0, min(100.0, percent))
            self._targets.append(
                EVChargeTarget(deadline=start, target_soc=percent, label=summary)
            )
            return

        # Forced window: charge at full power for every hour the event covers.
        hour = max(start, now).replace(minute=0, second=0, microsecond=0)
        while hour < end:
            self._forced_hours.add(hour)
            hour += timedelta(hours=1)

    def _event_bounds(self, event: dict) -> tuple[datetime, datetime] | None:
        """Localised (start, end) for an event; ``None`` if unparseable."""
        start = self._parse_dt(event.get("start"))
        end = self._parse_dt(event.get("end"))
        if start is None or end is None or end <= start:
            return None
        return start, end

    @staticmethod
    def _parse_dt(value) -> datetime | None:
        """Parse a calendar ``start``/``end`` (datetime or all-day date)."""
        if not value:
            return None
        text = str(value)
        parsed = dt_util.parse_datetime(text)
        if parsed is None:
            day = dt_util.parse_date(text)
            if day is None:
                return None
            parsed = datetime(day.year, day.month, day.day)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return dt_util.as_local(parsed)

    # ------------------------------------------------------------------
    # Request building
    # ------------------------------------------------------------------
    def get_request(self, forecast: Forecast) -> EVRequest:
        if not self.enabled:
            return EVRequest(enabled=False)

        battery_kwh = float(self.config.get(CONF_EV_BATTERY_KWH, 60.0))

        available_hours = (
            {
                slot.start.replace(minute=0, second=0, microsecond=0)
                for slot in forecast.slots
            }
            if self._available
            else set()
        )

        # Calendar plans take over when present; otherwise top up to the target.
        if self._targets or self._forced_hours:
            required_kwh = 0.0
        else:
            target_soc = (
                self._target_soc if self._target_soc is not None else DEFAULT_TARGET_SOC
            )
            current_soc = self._soc if self._soc is not None else target_soc
            required_kwh = max(0.0, (target_soc - current_soc) / 100.0 * battery_kwh)

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
            targets=list(self._targets),
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
        if not self._available and need:
            reminders.append("Podłącz samochód — zaplanowane jest ładowanie EV.")
        # Plan-vs-reality: a forced window is due this hour but the charger is idle.
        now_hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        due_now = (
            now_hour in self._request.forced_hours
            and now_hour in self._request.available_hours
        )
        if due_now and self._connected and self._charging is False:
            reminders.append(
                "EV powinien się teraz ładować (okno z kalendarza), "
                "ale ładowarka nie pobiera mocy."
            )
        return reminders

    def plan_summary(self) -> dict:
        """Serialisable EV plan/telemetry snapshot for the panel."""
        return {
            "enabled": self.enabled,
            "available": self._available,
            "soc": self._soc,
            "target_soc": self._target_soc,
            "soc_limit": self.soc_limit_now(),
            "energy_added_kwh": self._energy_added,
            "connected": self._connected,
            "charging": self._charging,
            "charger_power_kw": self.charger_power_kw,
            "targets": [
                {
                    "deadline": target.deadline.isoformat(),
                    "target_soc": target.target_soc,
                    "label": target.label,
                }
                for target in sorted(self._targets, key=lambda t: t.deadline)
            ],
            "forced_hours": [hour.isoformat() for hour in sorted(self._forced_hours)],
        }

    def soc_limit_now(self) -> float | None:
        """The SoC (%) the car should be allowed to charge to right now.

        A bare calendar window means "charge with no limit" → 100 %. With
        deadline targets the soonest upcoming one sets the ceiling. Otherwise the
        car's own target sensor (or the built-in default) applies.
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
        return self._target_soc if self._target_soc is not None else DEFAULT_TARGET_SOC

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
    def connected(self) -> bool | None:
        return self._connected

    @property
    def charging(self) -> bool | None:
        return self._charging

    @property
    def weekly_km(self) -> int:
        return int(self.config.get(CONF_EV_WEEKLY_KM, 0))

    @property
    def kwh_per_km(self) -> float:
        battery = float(self.config.get(CONF_EV_BATTERY_KWH, 60.0))
        rng = float(self.config.get(CONF_EV_RANGE_KM, 400.0)) or 1.0
        return battery / rng
