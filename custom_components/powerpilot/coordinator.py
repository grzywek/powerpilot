"""PowerPilot DataUpdateCoordinator.

Runs the full pipeline on a fixed interval:

    modules.update → ForecastBuilder.build → Optimizer.optimize → Plan

and exposes the resulting :class:`Plan` to the entities.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta
from typing import Final

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers.event import (
    async_track_point_in_time,
    async_track_state_change_event,
)
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .battery import BatteryModel
from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_WEAR_COST,
    CONF_CALENDARS,
    CONF_CHARGE_CURVE,
    CONF_CHARGE_EFFICIENCY,
    CONF_CHARGE_EFFICIENCY_CURVE,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_EV_CHARGING_SENSOR,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_PLUG_SENSOR,
    CONF_EV_PRESENCE_ENTITIES,
    CONF_GRID_VOLTAGE,
    CONF_INVERTER_MAX_CHARGE_KW,
    CONF_MIN_CHARGE_POWER_KW,
    CONF_INVERTER_MAX_DISCHARGE_KW,
    CONF_MAIN_FUSE_A,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PHASES,
    CONF_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
    InverterMode,
    MODE_CODE,
    MODE_CODE_INV,
    PRICE_ROUNDING_PER_BUCKET,
    PRICE_TYPE_CERTAIN,
    PRICE_TYPE_ESTIMATED,
    PRICE_TYPE_FORECAST,
    PTYPE_CODE,
    PTYPE_CODE_INV,
    STORAGE_VERSION_SNAPSHOTS,
)
from . import pricing
from .forecast import ForecastBuilder
from .models import Decision, Plan, tariff_for_day
from .modules.ev import CHARGING_STATES, HOME_STATES, PLUGGED_STATES
from .modules.snapshots import SnapshotStore
from .modules import (
    CalendarModule,
    ClimateModule,
    ConsumptionModule,
    EVModule,
    LoadsModule,
    ModuleRegistry,
    PriceModule,
    TariffModule,
    WeatherModule,
)
from .optimizer import ChargeCurve, Optimizer, OptimizerConfig

_LOGGER = logging.getLogger(__name__)

# Why a mid-hour re-plan ran, in the panel's language. The reactive listener
# knows which entity flipped; without that label a recorded revision only says
# "the plan changed", not "because the cable went in" — which is the whole point
# of recording it. Role → (config key it comes from) and the words for the flip.
_TRIGGER_ROLE_KEYS: Final[dict[str, str]] = {
    CONF_EV_PLUG_SENSOR: "plug",
    CONF_EV_CHARGING_SENSOR: "charging",
    CONF_EV_LOCATION_SENSOR: "location",
}
_TRIGGER_LABELS: Final[dict[str, str]] = {
    "plug": "kabel EV",
    "charging": "ładowanie EV",
    "location": "lokalizacja auta",
    "presence": "obecność",
    "calendar": "kalendarz",
}
# Role → (states that read as "yes", word for yes, word for no). Mirrors the EV
# module's own dialect sets, so the label matches what the planner concluded.
_TRIGGER_STATES: Final[dict[str, tuple[set[str], str, str]]] = {
    "plug": (PLUGGED_STATES, "podłączono", "odłączono"),
    "charging": (CHARGING_STATES, "start", "stop"),
    "location": (HOME_STATES, "w domu", "poza domem"),
    "presence": (HOME_STATES, "w domu", "poza domem"),
}
_UNKNOWN_STATES: Final = ("unknown", "unavailable", "none", "")

# A re-plan counts as a revision only past these thresholds (see
# ``_plan_row_differs``): 0.1 kW of charge power, 0.05 kWh of EV energy.
_REVISION_POWER_EPS: Final = 0.1
_REVISION_EV_EPS: Final = 0.05
# How long a charger may overrun the end of a planned hour before the spill into
# the next hour stops reading as "that session finishing" (see ``_ev_off_plan``).
_EV_SPILL_MINUTES: Final = 10


class PowerPilotCoordinator(DataUpdateCoordinator[Plan]):
    """Coordinates modules, forecast and optimizer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.config: dict = {**DEFAULTS, **entry.data, **entry.options}
        self._battery_energy_cost = 0.0
        self.events: deque = deque(maxlen=50)
        # What triggered the re-plan currently being computed (entity flips seen
        # by the reactive listener), and the entity → role map that names them.
        # Consumed by the snapshot recorder, which labels each mid-hour revision
        # with its cause.
        self._pending_triggers: list[str] = []
        self._trigger_roles: dict[str, str] = {}

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=None,
        )
        self._unsub_hour_boundary: CALLBACK_TYPE | None = None

        self.registry = ModuleRegistry()
        self.consumption = ConsumptionModule(hass, self)
        self.prices = PriceModule(hass, self)
        self.tariff = TariffModule(hass, self)
        self.loads = LoadsModule(hass, self)
        self.weather = WeatherModule(hass, self)
        self.climate = ClimateModule(hass, self)
        self.ev = EVModule(hass, self)
        self.calendar = CalendarModule(hass, self)

        # Order matters: prices/weather first, then derived loads, then the
        # calendar (events + trips), then EV — which consumes the calendar's
        # trips for unavailability, drive drain and pre-departure targets.
        for module in (
            self.prices,
            self.tariff,
            self.consumption,
            self.weather,
            self.climate,
            self.loads,
            self.calendar,
            self.ev,
        ):
            self.registry.register(module)

        self.forecast_builder = ForecastBuilder(self.registry)

        # Optimizer snapshots ("Symulacje" tab): one vintage per clock hour.
        self.snapshots = SnapshotStore()
        self._snapshot_store: Store | None = None
        self._last_snapshot_hour: datetime | None = None

    @callback
    def async_start_hour_boundary_updates(self) -> None:
        """Start exact clock-hour refreshes for hour-indexed control outputs."""
        self._schedule_hour_boundary_update()

    @callback
    def async_stop_hour_boundary_updates(self) -> None:
        """Cancel the pending clock-hour refresh, if any."""
        if self._unsub_hour_boundary is not None:
            self._unsub_hour_boundary()
            self._unsub_hour_boundary = None

    @callback
    def async_start_reactive_listeners(self) -> CALLBACK_TYPE | None:
        """Refresh the plan when calendars, presence or the EV plug change.

        The hourly cadence stays the baseline; this listener closes the gap
        between "the world changed" and the next boundary: an event added to
        the calendar, the car driving off or getting plugged back in. Calendar
        entities fire on any change (their next-event attributes matter);
        presence/plug/charging entities only on an actual state flip, so GPS
        attribute chatter from phone trackers doesn't re-run the optimizer
        every few minutes. ``async_request_refresh`` debounces bursts.
        """
        calendar_ids = [str(e) for e in (self.config.get(CONF_CALENDARS) or [])]
        flip_ids = [
            str(e)
            for e in (
                [
                    self.config.get(CONF_EV_LOCATION_SENSOR),
                    self.config.get(CONF_EV_PLUG_SENSOR),
                    self.config.get(CONF_EV_CHARGING_SENSOR),
                ]
                + list(self.config.get(CONF_EV_PRESENCE_ENTITIES) or [])
            )
            if e
        ]
        entities = calendar_ids + flip_ids
        if not entities:
            return None
        presence_set = set(flip_ids)

        # Entity → role, so a recorded re-plan can name what caused it.
        roles: dict[str, str] = {eid: "calendar" for eid in calendar_ids}
        for conf_key, role in _TRIGGER_ROLE_KEYS.items():
            entity_id = self.config.get(conf_key)
            if entity_id:
                roles[str(entity_id)] = role
        for entity_id in self.config.get(CONF_EV_PRESENCE_ENTITIES) or []:
            roles.setdefault(str(entity_id), "presence")
        self._trigger_roles = roles

        @callback
        def _on_state_change(event) -> None:
            entity_id = event.data.get("entity_id")
            old = event.data.get("old_state")
            new = event.data.get("new_state")
            if new is None:
                return
            if entity_id in presence_set and old is not None and old.state == new.state:
                return
            self._note_trigger(entity_id, new)
            self.hass.async_create_task(self.async_request_refresh())

        return async_track_state_change_event(self.hass, entities, _on_state_change)

    @callback
    def _note_trigger(self, entity_id: str | None, new_state) -> None:
        """Remember what caused the upcoming re-plan, in the panel's language.

        Labels dedupe by role, so a burst across several presence entities reads
        as one "obecność" and the list stays bounded by the number of roles.
        """
        role = self._trigger_roles.get(str(entity_id or ""))
        if role is None:
            return
        label = _TRIGGER_LABELS.get(role, role)
        states = _TRIGGER_STATES.get(role)
        if states is not None:
            truthy, yes, no = states
            value = str(new_state.state).lower()
            if value not in _UNKNOWN_STATES:
                label = f"{label}: {yes if value in truthy else no}"
        if label not in self._pending_triggers:
            self._pending_triggers.append(label)

    def _consume_trigger(self) -> str | None:
        """Take the reason for this run — cleared so it can't label a later one."""
        if not self._pending_triggers:
            return None
        reason = ", ".join(self._pending_triggers)
        self._pending_triggers = []
        return reason

    @callback
    def _schedule_hour_boundary_update(self) -> None:
        self.async_stop_hour_boundary_updates()
        now = dt_util.now()
        next_hour = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        self._unsub_hour_boundary = async_track_point_in_time(
            self.hass, self._async_handle_hour_boundary, next_hour
        )

    async def _async_handle_hour_boundary(self, _: datetime) -> None:
        self._unsub_hour_boundary = None
        try:
            if self.data is not None:
                self.async_set_updated_data(self.data)
            await self.async_refresh()
        finally:
            if not self.hass.is_stopping:
                self._schedule_hour_boundary_update()

    def current_decision(
        self, plan: Plan | None = None, moment: datetime | None = None
    ) -> Decision | None:
        """Decision covering the current clock hour in the latest plan."""
        active_plan = self.data if plan is None else plan
        if active_plan is None:
            return None
        return active_plan.decision_at(moment or dt_util.now())

    async def async_setup_modules(self) -> None:
        await self.registry.async_setup_all()
        await self._async_setup_snapshots()

    async def _async_setup_snapshots(self) -> None:
        self._snapshot_store = Store(
            self.hass,
            STORAGE_VERSION_SNAPSHOTS,
            f"{DOMAIN}_{self.entry.entry_id}_snapshots",
        )
        stored = await self._snapshot_store.async_load()
        self.snapshots = SnapshotStore.from_dict(stored)
        self.snapshots.prune()
        # Restore the modelled battery energy cost (no sensor exists for it, so a
        # restart would otherwise reset it to 0 and wipe the cost basis of energy
        # already stored — making "Cena w baterii" drop to ~0 until it rebuilds).
        if stored:
            self._battery_energy_cost = float(stored.get("battery_energy_cost") or 0.0)
            # Restore the once-per-hour snapshot guard so a mid-hour restart
            # leaves the already-recorded vintage untouched.
            self._last_snapshot_hour = (
                dt_util.parse_datetime(stored.get("last_snapshot_hour") or "")
                or None
            )

    def _snapshots_payload(self) -> dict:
        return {
            **self.snapshots.to_dict(),
            "battery_energy_cost": round(self._battery_energy_cost, 6),
            # Persisted so a restart within the same hour doesn't re-record the
            # active hour's vintage (which would overwrite the charging plan with
            # a restart-time re-plan and corrupt the forecast statistics).
            "last_snapshot_hour": (
                self._last_snapshot_hour.isoformat()
                if self._last_snapshot_hour
                else None
            ),
        }

    async def _async_save_snapshots(self) -> None:
        if self._snapshot_store is None:
            return
        self._snapshot_store.async_delay_save(self._snapshots_payload, 30.0)

    async def async_clear_data(self) -> None:
        """Wipe all persisted data/cache for this entry, keeping configuration.

        Removes every storage file (optimizer snapshots plus each module's
        learned consumption profile / price archive / tariff snapshots),
        cancelling any pending delayed save, and resets in-memory state. The
        config entry (data/options) is left untouched. Callers should reload the
        entry afterwards so modules re-initialise from a clean slate.
        """
        if self._snapshot_store is not None:
            await self._snapshot_store.async_remove()
        self.snapshots = SnapshotStore()
        self._last_snapshot_hour = None
        self._battery_energy_cost = 0.0
        self.events.clear()
        await self.registry.async_clear_all()

    def _build_battery(self, soc: float) -> BatteryModel:
        return BatteryModel(
            capacity_kwh=float(self.config[CONF_BATTERY_CAPACITY_KWH]),
            charge_efficiency=float(self.config[CONF_CHARGE_EFFICIENCY]),
            discharge_efficiency=float(self.config[CONF_DISCHARGE_EFFICIENCY]),
            wear_cost=float(self.config[CONF_BATTERY_WEAR_COST]),
            min_soc=float(self.config[CONF_MIN_SOC]),
            max_soc=float(self.config[CONF_MAX_SOC]),
            soc=soc,
            energy_cost=self._battery_energy_cost,
        )

    def _read_soc(self) -> float | None:
        """Live battery SoC (%) from the sensor, or ``None`` when unavailable.

        Never fabricates a value — a missing/unparseable reading returns ``None``
        so the optimizer holds the previous plan rather than planning from a
        guessed SoC (a wrong seed silently mis-plans charge/discharge).
        """
        entity_id = self.config.get(CONF_SOC_SENSOR)
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None:
            return None
        try:
            return float(state.state)
        except (TypeError, ValueError):
            return None

    def _build_optimizer(self) -> Optimizer:
        curve = ChargeCurve(
            default_kw=float(self.config[CONF_INVERTER_MAX_CHARGE_KW]),
            segments=list(self.config.get(CONF_CHARGE_CURVE) or []),
        )
        # Physical grid connection power: phases × phase voltage × main fuse.
        phases = float(self.config.get(CONF_PHASES, 0) or 0)
        voltage = float(self.config.get(CONF_GRID_VOLTAGE, 0) or 0)
        fuse_a = float(self.config.get(CONF_MAIN_FUSE_A, 0) or 0)
        connection_power_kw = phases * voltage * fuse_a / 1000.0
        return Optimizer(
            OptimizerConfig(
                inverter_max_charge_kw=float(self.config[CONF_INVERTER_MAX_CHARGE_KW]),
                inverter_max_discharge_kw=float(self.config[CONF_INVERTER_MAX_DISCHARGE_KW]),
                charge_curve=curve,
                connection_power_kw=connection_power_kw,
                # Single-phase headroom for battery-charging alongside the EV.
                phase_capacity_kw=voltage * fuse_a / 1000.0,
                charge_efficiency_curve=list(
                    self.config.get(CONF_CHARGE_EFFICIENCY_CURVE) or []
                ),
                min_charge_power_kw=float(
                    self.config.get(
                        CONF_MIN_CHARGE_POWER_KW, DEFAULTS[CONF_MIN_CHARGE_POWER_KW]
                    )
                ),
            )
        )

    async def _async_update_data(self) -> Plan:
        await self.registry.async_update_all()

        # Claimed at the top: a run already in flight when the entity flipped
        # would otherwise attach the label to a plan computed before it. A
        # re-plan straight after a restart has no flip behind it at all — say so
        # rather than leaving its revision unexplained.
        trigger = self._consume_trigger() or (
            "start integracji" if self.data is None else None
        )

        # The plan is only as good as its starting SoC. If the sensor is
        # momentarily unavailable, never seed from a fabricated value — keep the
        # last good plan and retry next cycle (a wrong seed mis-plans the battery).
        soc = self._read_soc()
        if soc is None:
            if self.data is not None:
                self.log_info(
                    "optimizer",
                    "Czujnik SoC chwilowo niedostępny — trzymam poprzedni plan, "
                    "ponowię w kolejnym cyklu.",
                    extra={"soc_sensor": self.config.get(CONF_SOC_SENSOR)},
                )
                return self.data
            raise UpdateFailed(
                "Czujnik SoC niedostępny — nie mogę policzyć planu bez stanu ESS."
            )

        forecast = await self.hass.async_add_executor_job(self.forecast_builder.build)
        ev_request = self.ev.get_request(forecast)
        reminders = self.registry.collect_reminders()

        battery = self._build_battery(soc)
        optimizer = self._build_optimizer()
        # The optimizer models the first slot as the REMAINDER of the running
        # hour, so a mid-hour re-run (restart, calendar edit, the EV getting
        # plugged/unplugged) is physically consistent and may legitimately
        # change the active hour's action — no frozen decision needed.
        plan = optimizer.optimize(forecast, battery, ev_request, reminders)

        current = self.current_decision(plan)
        if current is not None:
            self._battery_energy_cost = current.battery_energy_cost

        await self._maybe_record_snapshot(forecast, plan, trigger)
        self._record_event(plan)
        return plan

    # ------------------------------------------------------------------
    # Optimizer snapshots ("Symulacje" tab)
    # ------------------------------------------------------------------
    @staticmethod
    def _slot_ptype(slot) -> str:
        """Price provenance for a forecast slot (mirrors the prices module)."""
        if slot.price_confirmed:
            return PRICE_TYPE_CERTAIN
        if "price_estimated" in slot.tags:
            return PRICE_TYPE_ESTIMATED
        return PRICE_TYPE_FORECAST

    def _plan_row(self, plan: Plan, hour: datetime) -> dict | None:
        """What ``plan`` says about ``hour`` itself, compacted for a revision."""
        decision = plan.decision_at(hour)
        if decision is None:
            return None
        return {
            "ev": round(decision.ev_charge_kwh, 3),
            "ev_min": decision.ev_charge_minutes,
            "chg": round(decision.battery_charge_kwh, 3),
            "pw": round(decision.charge_power_kw, 3),
            "grid": round(decision.grid_buy_kwh, 3),
            "mode": MODE_CODE.get(decision.inverter_mode, "p"),
        }

    @staticmethod
    def _plan_row_differs(row: dict, baseline: dict) -> bool:
        """Did the running hour's plan materially change?

        Compared on quantities that do NOT shrink with the hour's remainder: the
        inverter mode, whether the car charges at all, and the charge POWER. The
        kWh figures fall proportionally on every mid-hour re-run (slot 0 covers
        only the rest of the hour), so comparing those would mark every refresh
        as a revision and bury the one that matters under the noise.
        """
        if row.get("mode") != baseline.get("mode"):
            return True
        charging = (row.get("ev") or 0.0) > _REVISION_EV_EPS
        was_charging = (baseline.get("ev") or 0.0) > _REVISION_EV_EPS
        if charging != was_charging:
            return True
        power, was_power = row.get("pw"), baseline.get("pw")
        if (power is None) != (was_power is None):
            return True
        return (
            power is not None
            and was_power is not None
            and abs(power - was_power) >= _REVISION_POWER_EPS
        )

    def _record_revision(
        self, hour: datetime, now: datetime, plan: Plan, trigger: str | None
    ) -> bool:
        """Append a mid-hour re-plan of ``hour`` when it changes something.

        Measured against whatever is already on record for the hour — the newest
        revision, or the hour-start vintage when there is none — so each entry
        marks an actual change of mind rather than a repeat of the last one.
        """
        row = self._plan_row(plan, hour)
        if row is None:
            return False
        previous = self.snapshots.revisions_at(hour)
        baseline = (
            previous[-1]
            if previous
            else {
                "ev": self.snapshots.run0_at(hour, "ev"),
                "pw": self.snapshots.run0_at(hour, "charge_pw"),
                "mode": self.snapshots.run0_at(hour, "mode"),
            }
        )
        if not self._plan_row_differs(row, baseline):
            return False
        return self.snapshots.add_revision(
            hour, {"at": now.isoformat(), "why": trigger, **row}
        )

    async def _maybe_record_snapshot(
        self, forecast, plan, trigger: str | None = None
    ) -> None:
        """Persist one columnar snapshot per clock hour (a 'vintage').

        Re-runs inside the same hour leave the vintage frozen — it is the plan
        the hour STARTED with, the honest baseline for forecast accuracy — and
        are appended as revisions instead, so a mid-hour change of mind stays
        visible rather than vanishing without trace.
        """
        now = dt_util.now()
        hour = now.replace(minute=0, second=0, microsecond=0)
        if self._last_snapshot_hour is not None and hour <= self._last_snapshot_hour:
            if self._record_revision(hour, now, plan, trigger):
                await self._async_save_snapshots()
            return

        slots = forecast.slots
        decisions = plan.decisions
        if not slots:
            return

        def _r(value, ndigits=4):
            return round(value, ndigits) if value is not None else None

        record: dict = {
            "run_at": now.isoformat(),
            "start": slots[0].start.isoformat(),
            # This hour is watched for mid-hour re-plans. Older vintages carry no
            # such marker, and for them "no revision recorded" must not be read as
            # "the plan never changed" — nobody was looking.
            "revcap": True,
            "n": len(slots),
            "horizon_hours": len(decisions),
            "total_cost": _r(plan.total_cost, 2),
            "buy": [_r(s.buy_price) for s in slots],
            "dist": [_r(s.distribution_price_kwh) for s in slots],
            "ptype": [PTYPE_CODE[self._slot_ptype(s)] for s in slots],
            "cons_fc": [_r(s.total_consumption_kwh, 3) for s in slots],
            "base_fc": [_r(s.base_consumption_kwh, 3) for s in slots],
            "mode": [MODE_CODE.get(d.inverter_mode, "p") for d in decisions],
            "soc": [_r(d.battery_soc, 1) for d in decisions],
            # Forecast EV SoC trajectory, so a time-traveled future view can draw
            # the EV SoC line as it was planned. Captured from now on; older
            # vintages predate this field and leave the line blank.
            "ev_soc": [_r(d.ev_soc, 1) for d in decisions],
            "grid": [_r(d.grid_buy_kwh, 3) for d in decisions],
            # Planned battery flows + EV, so the chart's forecast tooltip column
            # can be reconstructed for past hours. Captured from now on.
            "charge": [_r(d.battery_charge_kwh, 3) for d in decisions],
            "charge_pw": [_r(d.charge_power_kw, 3) for d in decisions],
            "dischg": [_r(d.battery_discharge_kwh, 3) for d in decisions],
            "ev": [_r(d.ev_charge_kwh, 3) for d in decisions],
            # Planned charging minutes within each hour (full charger power),
            # so past/pinned views can show how long the charger was meant to
            # run. Captured from now on; older vintages leave it blank.
            "ev_min": [d.ev_charge_minutes for d in decisions],
            "cost": [_r(d.hour_cost, 4) for d in decisions],
            # Realized battery energy cost (no sensor exists for it) so the chart
            # can show "Cena w baterii" for past hours by reading index 0 of the
            # vintage recorded at each hour. Captured from now on; older vintages
            # predate this field and stay blank.
            "bcost": [_r(d.battery_energy_cost, 4) for d in decisions],
            # Calendar trips as this plan saw them — past events must stay
            # visible on the chart even after being removed from the calendar,
            # and a stale-lead view must show the events *that plan* knew about.
            "trips": self.ev.trips_payload(),
        }
        self.snapshots.add(record)
        self.snapshots.prune()
        self._last_snapshot_hour = hour
        await self._async_save_snapshots()

    async def get_accuracy(self, lead_hours: int = 24, days: int = 7) -> dict:
        """Forecast-vs-actual error for past hours, at a given lead time.

        For each past target hour H, picks the vintage produced at most recently
        at or before ``H - lead_hours`` and compares its predicted consumption /
        price for H against the realized actuals. ``bias`` = mean(predicted −
        actual); a negative value means the optimizer systematically
        *under*-estimates.
        """
        from .const import CONF_CONSUMPTION_SENSOR

        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        window_start = now - timedelta(days=max(days, 1))

        main_sensor = self.config.get(CONF_CONSUMPTION_SENSOR)
        actual_cons: dict = {}
        if main_sensor:
            actual_cons = await self.consumption.async_range_kwh(
                main_sensor, window_start - timedelta(hours=1), now
            )

        def _pred_for(target: datetime):
            run_key = self.snapshots.nearest_run_at(target - timedelta(hours=lead_hours))
            if run_key is None:
                return None
            rec = self.snapshots.get(run_key)
            if rec is None:
                return None
            start = dt_util.parse_datetime(rec["start"])
            if start is None:
                return None
            idx = round((target - start).total_seconds() / 3600.0)
            if idx < 0 or idx >= rec.get("n", 0):
                return None
            cons_seq = rec.get("cons_fc") or []
            buy_seq = rec.get("buy") or []
            return {
                "cons": cons_seq[idx] if idx < len(cons_seq) else None,
                "buy": buy_seq[idx] if idx < len(buy_seq) else None,
            }

        hours: list[dict] = []
        errors: list[float] = []
        price_errors: list[float] = []
        bias_sum = [0.0] * 24
        bias_cnt = [0] * 24
        h = window_start
        while h < now:
            pred = _pred_for(h)
            act_c = actual_cons.get(h)
            # Price truth = the settled (certain) archive entry; comparing a
            # prediction against another forecast would understate the error.
            arch = self.prices.archive.get(h) or {}
            act_p = (
                arch.get("energy")
                if arch.get("type") == PRICE_TYPE_CERTAIN
                else None
            )
            pred_c = pred["cons"] if pred else None
            pred_p = pred["buy"] if pred else None
            err = (
                round(pred_c - act_c, 3)
                if (pred_c is not None and act_c is not None)
                else None
            )
            price_err = (
                round(pred_p - act_p, 4)
                if (pred_p is not None and act_p is not None)
                else None
            )
            if err is not None:
                errors.append(err)
                bias_sum[h.hour] += err
                bias_cnt[h.hour] += 1
            if price_err is not None:
                price_errors.append(price_err)
            hours.append(
                {
                    "start": h.isoformat(),
                    "predicted_cons": round(pred_c, 3) if pred_c is not None else None,
                    "actual_cons": round(act_c, 3) if act_c is not None else None,
                    "error": err,
                    "predicted_price": pred_p,
                    "actual_price": round(act_p, 4) if act_p is not None else None,
                    "price_error": price_err,
                }
            )
            h += timedelta(hours=1)

        bias_by_hour = [
            round(bias_sum[i] / bias_cnt[i], 3) if bias_cnt[i] else None
            for i in range(24)
        ]
        mae = round(sum(abs(e) for e in errors) / len(errors), 3) if errors else None
        bias = round(sum(errors) / len(errors), 3) if errors else None
        price_mae = (
            round(sum(abs(e) for e in price_errors) / len(price_errors), 4)
            if price_errors
            else None
        )
        price_bias = (
            round(sum(price_errors) / len(price_errors), 4) if price_errors else None
        )
        return {
            "lead_hours": lead_hours,
            "days": days,
            "samples": len(errors),
            "mae": mae,
            "bias": bias,
            "price_samples": len(price_errors),
            "price_mae": price_mae,
            "price_bias": price_bias,
            "bias_by_hour": bias_by_hour,
            "hours": hours,
        }

    # ------------------------------------------------------------------
    # Frontend support: event log + feature status
    # ------------------------------------------------------------------
    def _record_event(self, plan: Plan) -> None:
        current = self.current_decision(plan)
        errors = [
            f"{m.domain}: {m.last_error}" for m in self.registry.modules if m.last_error
        ]
        self.events.appendleft(
            {
                "time": dt_util.now().isoformat(),
                "type": "plan",
                "module": "coordinator",
                "message": f"Plan {len(plan.decisions)}h horizon, action={current.inverter_mode if current else None}",
                "horizon_hours": len(plan.decisions),
                "action": current.inverter_mode if current else None,
                "ev_charge": current.ev_charge if current else None,
                "battery_soc": round(current.battery_soc, 1) if current else None,
                "errors": errors,
            }
        )

    def log_info(self, module: str, message: str, extra: dict | None = None) -> None:
        """Push a structured info event (visible in the panel log table)."""
        event: dict = {
            "time": dt_util.now().isoformat(),
            "type": "info",
            "module": module,
            "message": message,
        }
        if extra:
            event["extra"] = extra
        self.events.appendleft(event)

    def log_warning(self, module: str, message: str, extra: dict | None = None) -> None:
        event: dict = {
            "time": dt_util.now().isoformat(),
            "type": "warning",
            "module": module,
            "message": message,
        }
        if extra:
            event["extra"] = extra
        self.events.appendleft(event)

    def get_log(self) -> list[dict]:
        return list(self.events)

    def get_profiles(self) -> dict:
        """7×24 learned consumption profiles for the panel heatmaps."""
        from .modules.climate import MIN_LEARN_DAYS

        return {
            "consumption": self.consumption.base.as_matrix(),
            "consumption_days": self.consumption.base.observed_days,
            "devices": {
                eid: acc.as_matrix() for eid, acc in self.consumption.devices.items()
            },
            # Temperature profiles of the weather-dependent loads (bin × hour),
            # keyed by sensor — includes still-learning ones so the panel can
            # show progress toward the takeover threshold.
            "climate": {
                eid: {
                    "observed_days": self.climate.profile_for(eid).observed_days,
                    "samples": self.climate.profile_for(eid).samples,
                    "ready": self.climate.is_ready(eid),
                    "min_learn_days": MIN_LEARN_DAYS,
                    "presence_sensors": len(self.climate.presence_sensors),
                    "matrix": self.climate.profile_for(eid).as_matrix(),
                }
                for eid in self.climate.sensors
            },
        }

    async def async_consumption_stats(self, days: int = 63) -> dict:
        """Per-profile daily kWh history + week/month trend KPIs for the panel.

        For the whole-house meter and each sub-metered device, bucket the last
        ``days`` of hourly recorder energy into local-day totals, then derive
        rolling-window indicators (7-day and 30-day totals vs the preceding
        window) so the panel can show "consumption up/down XX %". Today is left
        out of the comparison windows because it is only partially elapsed.
        """
        from collections import defaultdict

        from .const import (
            CONF_CONSUMPTION_SENSOR,
            CONF_DEVICE_SENSORS,
            CONF_SENSOR_PARENTS,
        )
        from .hierarchy import exclusive_series

        now = dt_util.now()
        today = dt_util.start_of_local_day(now)
        start = today - timedelta(days=days - 1)
        end_date = now.date()

        main = self.config.get(CONF_CONSUMPTION_SENSOR)
        device_ids = list(self.config.get(CONF_DEVICE_SENSORS) or [])

        # Hourly energy per sensor over the window (one recorder read each).
        hourly: dict[str, dict] = {}
        if main:
            hourly[main] = await self.consumption.async_range_kwh(main, start, now)
        for eid in device_ids:
            hourly[eid] = await self.consumption.async_range_kwh(eid, start, now)

        # Collapse nested meters to exclusive (own) energy so device totals never
        # double-count a sub-meter and "Tło" is the true background load. This is
        # exactly what the optimizer/simulation consumes (base + Σ exclusive
        # device profiles telescope back to the whole-house reading), and it
        # matches the learned 7×24 heatmaps — so every view here is consistent.
        background = None
        exclusive: dict = {}
        if main:
            parents = self.config.get(CONF_SENSOR_PARENTS) or {}
            exclusive = exclusive_series(main, device_ids, parents, hourly)
            background = exclusive.get(main, {})

        def _name(eid: str) -> str:
            state = self.hass.states.get(eid)
            return (state and state.attributes.get("friendly_name")) or eid

        def _profile(key: str, name: str, series: dict, icon: str) -> dict:
            daily: dict = defaultdict(float)
            for hour, kwh in series.items():
                daily[dt_util.as_local(hour).date()] += kwh
            out_series = []
            day = start.date()
            while day <= end_date:
                out_series.append(
                    {
                        "date": day.isoformat(),
                        "kwh": round(daily.get(day, 0.0), 3),
                        "partial": day == end_date,
                    }
                )
                day += timedelta(days=1)

            def window(offset: int, length: int) -> float:
                return sum(
                    daily.get(end_date - timedelta(days=i), 0.0)
                    for i in range(offset, offset + length)
                )

            def pct(cur: float, prev: float) -> float | None:
                if prev <= 1e-6:
                    return None
                return round((cur - prev) / prev * 100.0, 1)

            # Complete days only — start one day back so today's partial is excluded.
            week, prev_week = window(1, 7), window(8, 7)
            month, prev_month = window(1, 30), window(31, 30)
            return {
                "key": key,
                "name": name,
                "icon": icon,
                "daily": out_series,
                "avg_daily": round(week / 7.0, 3),
                "week_total": round(week, 3),
                "week_change_pct": pct(week, prev_week),
                "month_total": round(month, 3),
                "month_change_pct": pct(month, prev_month),
            }

        profiles: list[dict] = []
        if main:
            profiles.append(_profile("__main__", "Dom (całość)", hourly[main], "mdi:home-lightning-bolt"))
            if device_ids and background is not None:
                profiles.append(_profile("__base__", "Tło (baza)", background, "mdi:home-outline"))
        for eid in device_ids:
            # Own (exclusive) energy — for a meter nesting sub-meters this excludes
            # the children, consistent with the optimizer and the learned heatmap.
            own = exclusive.get(eid, hourly.get(eid, {}))
            profiles.append(_profile(eid, _name(eid), own, "mdi:flash"))

        return {
            "generated_at": now.isoformat(),
            "window_days": days,
            "learned_days": self.consumption.base.observed_days,
            "profiles": profiles,
        }

    async def get_diagnostics(self) -> dict:
        """Readiness report: does the optimizer have every input it needs?

        For each configured sensor it reports the unit, ``state_class``, whether
        HA actually keeps long-term statistics for it, and how many recent hours
        are readable — turning "why is this empty?" into a one-glance verdict
        (configured? recorded? right unit?). Non-sensor inputs (price source,
        tariff, EV) get a high-level check. ``ready`` is true when no *required*
        input is in error.
        """
        from .const import (
            CONF_BATTERY_CHARGE_SENSOR,
            CONF_BATTERY_DISCHARGE_SENSOR,
            CONF_BUY_PRICE_SENSOR,
            CONF_CONSUMPTION_SENSOR,
            CONF_DEVICE_SENSORS,
            CONF_EV_ENABLED,
            CONF_EV_SOC_SENSOR,
            CONF_GRID_IMPORT_SENSOR,
            CONF_PRADCAST_API_KEY,
            CONF_PRICE_SOURCE,
            CONF_SOC_SENSOR,
            CONF_WEATHER_ENTITY,
            PRICE_SOURCE_PRADCAST,
        )

        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        win_start = now - timedelta(hours=48)

        async def _sensor_item(
            key: str, label: str, conf_key: str, required: bool, raw: bool = False
        ) -> dict:
            """Diagnose one configured sensor into a readiness verdict.

            ``raw=True`` is for value sensors (e.g. SoC %) whose history lives in
            ``mean`` statistics rather than the kWh ``sum`` deltas energy/power
            sensors use.
            """
            eid = self.config.get(conf_key)
            item: dict = {
                "key": key,
                "label": label,
                "required": required,
                "entity_id": eid,
                "detail": None,
            }
            if not eid:
                item["status"] = "error" if required else "skip"
                item["message"] = (
                    "Nie skonfigurowany" if required else "Pominięty (opcjonalny)"
                )
                return item
            try:
                detail = await self.consumption.async_diagnose_sensor(
                    eid, win_start, now
                )
            except Exception as err:  # never let one sensor break the report
                item["status"] = "error"
                item["message"] = f"Błąd odczytu: {err!r}"
                return item
            item["detail"] = detail
            if not detail["available"]:
                item["status"] = "error"
                item["message"] = "Encja niedostępna w HA"
            elif raw:
                if detail["stat_rows_mean"] > 0:
                    item["status"] = "ok"
                    item["message"] = f"{detail['stat_rows_mean']} godz. statystyk / 48h"
                else:
                    item["status"] = "error"
                    item["message"] = (
                        "Brak statystyk (wykluczony z recordera lub brak state_class)"
                    )
            elif detail["detected_kind"] is None:
                item["status"] = "error"
                item["message"] = (
                    f"Nierozpoznana jednostka: {detail['unit_of_measurement']!r} "
                    "(oczekiwane W/kW/Wh/kWh)"
                )
            elif detail["series_hours"] > 0:
                item["status"] = "ok"
                item["message"] = f"{detail['series_hours']} godz. danych / 48h"
            elif (
                detail["detected_kind"] == "energy"
                and detail["stat_rows_sum"] == 0
                and detail["stat_rows_mean"] > 0
            ):
                item["status"] = "warn"
                item["message"] = (
                    "kWh ze state_class=measurement → brak sum; ustaw "
                    "total/total_increasing"
                )
            else:
                item["status"] = "error"
                item["message"] = (
                    "Brak statystyk godzinowych (encja wykluczona z recordera?)"
                )
            return item

        # ---- Required for the optimizer to plan correctly ----
        required_items: list[dict] = [
            await _sensor_item(
                "consumption",
                "Zużycie domu (zapotrzebowanie)",
                CONF_CONSUMPTION_SENSOR,
                required=True,
            ),
            await _sensor_item(
                "soc", "🔋 SoC ESS", CONF_SOC_SENSOR, required=True, raw=True
            ),
        ]

        # Price source (sensor or Pradcast) — readiness = a price for "now".
        price_source = self.config.get(CONF_PRICE_SOURCE)
        has_price = self.prices.price_at(now) is not None
        if price_source == PRICE_SOURCE_PRADCAST:
            configured = bool(self.config.get(CONF_PRADCAST_API_KEY))
        else:
            configured = bool(self.config.get(CONF_BUY_PRICE_SENSOR))
        price_item = {
            "key": "prices",
            "label": f"Ceny energii ({price_source})",
            "required": True,
            "entity_id": self.config.get(CONF_BUY_PRICE_SENSOR)
            if price_source != PRICE_SOURCE_PRADCAST
            else None,
            "detail": {"archive_hours": len(self.prices.archive)},
        }
        if not configured:
            price_item["status"] = "error"
            price_item["message"] = (
                "Brak klucza API Pradcast"
                if price_source == PRICE_SOURCE_PRADCAST
                else "Brak sensora ceny"
            )
        elif has_price:
            price_item["status"] = "ok"
            price_item["message"] = (
                f"Cena na teraz dostępna · archiwum {len(self.prices.archive)} godz."
            )
        else:
            price_item["status"] = "warn"
            price_item["message"] = "Skonfigurowane, ale brak ceny na bieżącą godzinę"
        required_items.append(price_item)

        # Distribution tariff (affects total price; optional but recommended).
        tariff_active = bool(
            self.tariff.tariffs and tariff_for_day(self.tariff.tariffs, now.date())
        )
        required_items.append(
            {
                "key": "tariff",
                "label": "Taryfa dystrybucyjna",
                "required": False,
                "entity_id": None,
                "detail": {"count": len(self.tariff.tariffs)},
                "status": "ok" if tariff_active else "warn",
                "message": (
                    f"Aktywna ({len(self.tariff.tariffs)} skonfig.)"
                    if tariff_active
                    else "Brak aktywnej taryfy — cena dystrybucji = 0"
                ),
            }
        )

        # ---- Battery & grid actuals (chart history + cost tracking) ----
        battery_items = [
            await _sensor_item(
                "battery_charge",
                "🔋 Sensor ładowania ESS",
                CONF_BATTERY_CHARGE_SENSOR,
                required=False,
            ),
            await _sensor_item(
                "battery_discharge",
                "🔋 Sensor rozładowania ESS",
                CONF_BATTERY_DISCHARGE_SENSOR,
                required=False,
            ),
            await _sensor_item(
                "grid_import",
                "Sensor importu z sieci",
                CONF_GRID_IMPORT_SENSOR,
                required=False,
            ),
        ]

        # ---- Optional inputs ----
        optional_items: list[dict] = []
        for eid in self.config.get(CONF_DEVICE_SENSORS) or []:
            di = await self.consumption.async_diagnose_sensor(eid, win_start, now)
            name = eid.split(".")[-1]
            optional_items.append(
                {
                    "key": f"device:{eid}",
                    "label": f"Urządzenie: {name}",
                    "required": False,
                    "entity_id": eid,
                    "detail": di,
                    "status": "ok" if di["series_hours"] > 0 else "warn",
                    "message": (
                        f"{di['series_hours']} godz. danych / 48h"
                        if di["series_hours"] > 0
                        else "Brak danych godzinowych"
                    ),
                }
            )
        # Weather entities keep no recorder statistics (their state is a
        # condition string) — the readiness check is "does it serve an hourly
        # forecast via weather.get_forecasts", not the sensor stats probe.
        weather_item: dict = {
            "key": "weather",
            "label": "Encja pogody",
            "required": False,
            "entity_id": self.config.get(CONF_WEATHER_ENTITY),
            "detail": None,
        }
        weather_eid = self.config.get(CONF_WEATHER_ENTITY)
        if not weather_eid:
            weather_item["status"] = "skip"
            weather_item["message"] = "Pominięty (opcjonalny)"
        else:
            weather_state = self.hass.states.get(weather_eid)
            if weather_state is None:
                weather_item["status"] = "error"
                weather_item["message"] = "Encja niedostępna w HA"
            else:
                fc_hours = len(await self.weather.async_hourly_forecast(weather_eid))
                temp_now = weather_state.attributes.get("temperature")
                weather_item["detail"] = {
                    "temperature_now": temp_now,
                    "forecast_hours": fc_hours,
                }
                if fc_hours > 0:
                    weather_item["status"] = "ok"
                    weather_item["message"] = (
                        f"Prognoza godzinowa: {fc_hours} h"
                        + (f" · temperatura teraz: {temp_now} °C" if temp_now is not None else "")
                    )
                else:
                    weather_item["status"] = "error"
                    weather_item["message"] = (
                        "Brak prognozy godzinowej (weather.get_forecasts type=hourly)"
                    )
        optional_items.append(weather_item)
        if self.config.get(CONF_EV_ENABLED):
            optional_items.append(
                await _sensor_item(
                    "ev_soc", "SoC samochodu (EV)", CONF_EV_SOC_SENSOR, required=False, raw=True
                )
            )

        groups = [
            {"title": "Wymagane do optymalizacji", "items": required_items},
            {"title": "🔋 ESS i sieć (dane rzeczywiste)", "items": battery_items},
            {"title": "Opcjonalne", "items": optional_items},
        ]

        summary = {"ok": 0, "warn": 0, "error": 0, "skip": 0}
        ready = True
        for group in groups:
            for item in group["items"]:
                summary[item["status"]] = summary.get(item["status"], 0) + 1
                if item["required"] and item["status"] == "error":
                    ready = False

        return {
            "generated_at": dt_util.now().isoformat(),
            "ready": ready,
            "summary": summary,
            "groups": groups,
        }

    async def get_debug(self, hours: int | None = None) -> dict:
        """Diagnostic snapshot for troubleshooting optimizer decisions.

        Bundles the (secret-redacted) config, the current plan with per-hour
        decision traces, feature status, learned profiles and the recent + full
        forecast series — everything needed to reason about why the optimizer
        made a given decision, in one copy/paste-able JSON blob.

        ``hours`` bounds the dump to the next N hours (and 12 h of history) so
        it can be pasted into an LLM without burning tokens on the far tail:
        the plan/series/EV hours beyond the window are cut (each with an
        explicit summary of what was dropped — a trimmed dump must never read
        as "that was everything") and the static learned profiles are left
        out. ``None`` → the full horizon, profiles included.
        """
        secret_hints = ("api_key", "token", "password", "secret")

        def _redact(cfg: dict) -> dict:
            out: dict = {}
            for key, value in cfg.items():
                if any(hint in str(key).lower() for hint in secret_hints):
                    out[key] = "***redacted***" if value else None
                else:
                    out[key] = value
            return out

        from .const import (
            CONF_BATTERY_CHARGE_SENSOR,
            CONF_BATTERY_DISCHARGE_SENSOR,
            CONF_BUY_PRICE_SENSOR,
            CONF_CONSUMPTION_SENSOR,
            CONF_DEVICE_SENSORS,
            CONF_GRID_IMPORT_SENSOR,
            CONF_SOC_SENSOR,
        )

        plan = self.data
        now_hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
        window_end = now_hour + timedelta(hours=hours) if hours else None
        if window_end is not None:
            series = await self.get_series(
                past_hours=12, end=window_end.isoformat()
            )
        else:
            series = await self.get_series(past_hours=48)

        plan_dict = plan.as_dict() if plan else None
        if plan_dict is not None and window_end is not None:
            for key in ("hours", "forecast"):
                rows = plan_dict.get(key) or []
                kept = [
                    r
                    for r in rows
                    if (start := dt_util.parse_datetime(r.get("start") or ""))
                    is not None
                    and start < window_end
                ]
                plan_dict[key] = kept
                plan_dict[f"{key}_beyond_window"] = len(rows) - len(kept)

        # Per-sensor readability diagnostic: pinpoints why a historical series is
        # empty (unrecognised unit, no statistics, or a sum/mean mismatch).
        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        diag_start = now - timedelta(hours=48)
        diag_targets: list[tuple[str, str]] = []
        for key in (
            CONF_CONSUMPTION_SENSOR,
            CONF_BATTERY_CHARGE_SENSOR,
            CONF_BATTERY_DISCHARGE_SENSOR,
            CONF_GRID_IMPORT_SENSOR,
            CONF_SOC_SENSOR,
            CONF_BUY_PRICE_SENSOR,
        ):
            sid = self.config.get(key)
            if sid:
                diag_targets.append((key, sid))
        for sid in self.config.get(CONF_DEVICE_SENSORS) or []:
            diag_targets.append((CONF_DEVICE_SENSORS, sid))

        sensor_reads: list[dict] = []
        for key, sid in diag_targets:
            try:
                row = await self.consumption.async_diagnose_sensor(sid, diag_start, now)
            except Exception as err:  # diagnostics must never break the debug dump
                row = {"entity_id": sid, "error": repr(err)}
            row["config_key"] = key
            sensor_reads.append(row)

        ev_debug = self._ev_debug(plan)
        if window_end is not None:
            ev_hours = ev_debug.get("hours") or []
            kept_ev = [
                h
                for h in ev_hours
                if (start := dt_util.parse_datetime(h.get("start") or ""))
                is not None
                and start < window_end
            ]
            dropped = [h for h in ev_hours if h not in kept_ev]
            ev_debug["hours"] = kept_ev
            # The total EV energy beyond the window stays visible — a trimmed
            # dump showing 10 kWh must not read as "only 10 kWh planned".
            ev_debug["beyond_window"] = {
                "hours": len(dropped),
                "ev_charge_kwh": round(
                    sum(h["ev_charge_kwh"] for h in dropped), 3
                ),
            }

        log = self.get_log()
        log_truncated = 0
        if hours:
            log_truncated = max(0, len(log) - 12)
            log = log[:12]

        return {
            "generated_at": dt_util.now().isoformat(),
            "window_hours": hours,
            "config": _redact(dict(self.config)),
            "plan": plan_dict,
            "ev_debug": ev_debug,
            "status": self.get_status(),
            # Learned profiles are static reference data — only the full dump
            # carries them; the windowed dump marks the omission explicitly.
            "profiles": self.get_profiles() if not hours else "omitted (windowed dump)",
            "series": series,
            "sensor_reads": sensor_reads,
            "log": log,
            "log_truncated": log_truncated,
        }

    def _ev_debug(self, plan: Plan | None) -> dict:
        """EV allocator inputs + per-hour output, for diagnosing charge decisions.

        ``allocator_version`` proves which code is actually loaded: if it's
        missing or stale after a deploy, only the config entry was reloaded and
        Home Assistant needs a full restart to re-import the changed modules.
        ``hours`` pairs each planned charge with its price so a fractional hour
        is immediately explainable (price order, deadline, or the 100% ceiling).
        """
        from .optimizer import EV_ALLOCATOR_VERSION

        request = self.ev.request_debug()
        forced = set(self.ev._request.forced_hours)
        available = self.ev._request.available_hours
        hours: list[dict] = []
        if plan is not None:
            for slot, decision in zip(plan.forecast.slots, plan.decisions):
                if decision.ev_charge_kwh <= 0 and slot.start not in forced:
                    continue
                hours.append(
                    {
                        "start": slot.start.isoformat(),
                        "buy_price": slot.buy_price,
                        "total_price_kwh": slot.total_price_kwh,
                        "ev_charge_kwh": round(decision.ev_charge_kwh, 3),
                        "ev_grid_kwh": round(decision.ev_grid_kwh, 3),
                        "ev_minutes": decision.ev_charge_minutes,
                        "ev_soc": decision.ev_soc,
                        "available": slot.start in available,
                        "forced": slot.start in forced,
                    }
                )
        return {
            "allocator_version": EV_ALLOCATOR_VERSION,
            "request": request,
            "planned_kwh_total": round(
                sum(h["ev_charge_kwh"] for h in hours), 3
            ),
            "hours": hours,
        }

    def get_price_archive(self, date_str: str | None) -> dict:
        """Hourly price archive for a single day (the "Ceny" tab).

        Reads the permanent energy archive (certain/forecast) or derives the
        estimated price for hours with no fetched data, pairs each with the
        distribution price (snapshot for past hours, live-resolved for future
        ones) and the gross full price. Estimated rows carry the three weekly
        samples + weights so the UI can explain the calculation on hover.
        """
        from datetime import date as _date

        if date_str:
            try:
                target = _date.fromisoformat(date_str)
            except ValueError:
                target = dt_util.now().date()
        else:
            target = dt_util.now().date()

        def _r(value: float | None, ndigits: int = 4) -> float | None:
            return round(value, ndigits) if value is not None else None

        start = dt_util.start_of_local_day(target)
        hours: list[dict] = []
        for index in range(24):
            hour = start + timedelta(hours=index)
            entry = self.prices.archive.get(hour)
            breakdown = None
            p10 = p90 = None
            fc_energy = fc_fetched_at = None
            if entry is not None:
                energy = entry["energy"]
                price_type = entry["type"]
                source = entry["source"]
                fetched_at = entry["fetched_at"]
                p10 = entry.get("p10")
                p90 = entry.get("p90")
                # Last forecast the source published before this hour settled
                # (stashed by the archive when certain replaced forecast).
                fc_energy = entry.get("fc_energy")
                fc_fetched_at = entry.get("fc_fetched_at")
            else:
                energy, samples = self.prices.archive.estimate(hour)
                if energy is None:
                    price_type = None
                    source = None
                else:
                    price_type = PRICE_TYPE_ESTIMATED
                    source = "estimate"
                    breakdown = [
                        {**s, "value": _r(s["value"])} for s in samples
                    ]
                fetched_at = None

            dist = self.tariff.distribution_for(hour)  # gross PLN/kWh
            formula = entry.get("formula") if entry is not None else None

            # Comparison columns for the accuracy view: what the (weekly-model)
            # estimate says for this hour, and — for settled hours — the last
            # source forecast before RDN settlement. The estimate is derived
            # from 1/2/3-weeks-earlier archive entries, which are certain long
            # before this hour, so computing it on read is deterministic.
            est_energy: float | None = None
            if price_type != PRICE_TYPE_ESTIMATED:
                est_energy, _est_bd = self.prices.archive.estimate(hour)

            row = {
                "start": hour.isoformat(),
                "type": price_type,
                "source": source,
                "fetched_at": fetched_at,
                "p10": _r(p10),
                "p90": _r(p90),
                "estimate_breakdown": breakdown,
                # Energy-side (gross PLN/kWh) comparators, same basis as the
                # archived value the optimizer consumed.
                "forecast_energy_kwh": _r(fc_energy),
                "forecast_fetched_at": fc_fetched_at,
                "estimate_energy_kwh": _r(est_energy),
            }

            if formula is not None and dist is not None:
                # New-format hour: rebuild the seller-style breakdown from the
                # net components + parameters frozen at fetch time, so changing
                # the config later never rewrites this row.
                dist_vat = self.tariff.vat_rate_for(hour)
                dist_net = dist / (1.0 + dist_vat) if dist_vat else dist
                bd = pricing.assemble(
                    tge=formula.get("tge"),
                    markup=formula.get("markup", 0.0),
                    dist_net=dist_net,
                    excise=formula.get("excise", 0.0),
                    vat_rate=formula.get("vat", 0.0),
                    rounding=formula.get("rounding", PRICE_ROUNDING_PER_BUCKET),
                    dist_vat_rate=dist_vat,
                )
                row.update(
                    {
                        # Gross per-side values kept for the chart's stacked bars.
                        "energy_price_kwh": _r(bd["energy_gross"]),
                        "distribution_price_kwh": _r(dist),
                        "total_price_kwh": _r(bd["total"], 2),
                        "fixed_cost_hourly": None,
                        # Net cost components + combined tax bucket (TGE / marża /
                        # dystrybucja / akcyza / podatki) for the breakdown tooltip.
                        "tge_kwh": _r(bd["tge"]),
                        "markup_kwh": _r(bd["markup"]),
                        "distribution_net_kwh": _r(dist_net),
                        "excise_kwh": _r(bd["excise"]),
                        "taxes_kwh": _r(bd["taxes"], 2),
                        "vat_rate": formula.get("vat", 0.0),
                    }
                )
            else:
                # Legacy / estimated / sensor hour: render exactly as before
                # (single energy bucket, fixed charge folded in) so history that
                # predates the new pricing model is preserved unchanged.
                fixed_hourly = self.tariff.fixed_hourly_for(hour)
                total = (
                    energy + dist + (fixed_hourly or 0.0)
                    if (energy is not None and dist is not None)
                    else None
                )
                row.update(
                    {
                        "energy_price_kwh": _r(energy),
                        "distribution_price_kwh": _r(dist),
                        "total_price_kwh": _r(total),
                        "fixed_cost_hourly": _r(fixed_hourly),
                    }
                )

            hours.append(row)
        return {"date": target.isoformat(), "hours": hours}

    async def _recent_soc(
        self, start: datetime, end: datetime, entity_id: str | None = None
    ) -> dict:
        """SoC (%) at each hour's END, sampled from a sensor's history.

        ``out[h]`` is the instantaneous SoC at ``h + 1h`` — the boundary the
        battery crosses *leaving* hour ``h`` — so ``soc_start``/``soc_end`` line
        up with the actual sensor readings. (The hourly **mean** would smear a
        charging ramp across the hour: a battery going 4 %→28 % over the 13:00
        hour means ~15 % and would show "4 → 15" instead of "4 → 28".) Returns
        ``{}`` when the sensor has no usable history for the window.

        Defaults to the home-battery SoC sensor; pass ``entity_id`` to sample a
        different one (e.g. the EV SoC sensor).
        """
        from homeassistant.components.recorder import get_instance, history

        from .const import CONF_SOC_SENSOR

        if entity_id is None:
            entity_id = self.config.get(CONF_SOC_SENSOR)
        if not entity_id:
            return {}

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
            except (ValueError, TypeError):
                continue  # "unavailable" / "unknown"

        if not series:
            return {}

        series.sort(key=lambda item: item[0])
        out: dict = {}
        boundary = dt_util.as_local(start).replace(minute=0, second=0, microsecond=0)
        last = dt_util.as_local(end).replace(minute=0, second=0, microsecond=0)
        idx = 0
        last_val: float | None = None
        while boundary <= last:
            cutoff = dt_util.as_utc(boundary)
            while idx < len(series) and series[idx][0] <= cutoff:
                last_val = series[idx][1]
                idx += 1
            if last_val is not None:
                # SoC at this boundary is the END-of-hour value for the prior hour.
                out[boundary - timedelta(hours=1)] = last_val
            boundary += timedelta(hours=1)
        return out

    def _device_forecast_kwh(self, eid: str, hour: datetime) -> float | None:
        """Per-device forecast for the chart series.

        The climate-owned devices read their temperature models (matching what
        the optimizer plans); every other device uses the learned weekly average.
        """
        if self.climate.handles(eid):
            value = self.climate.forecast_kwh(eid, hour)
            if value is not None:
                return round(value, 3)
        if eid in self.consumption.devices:
            return round(
                self.consumption.devices[eid].value(hour.weekday(), hour.hour) or 0.0,
                3,
            )
        return None

    async def get_series(
        self,
        past_hours: int = 24,
        start: str | None = None,
        end: str | None = None,
        forecast_lead: int = 0,
        forecast_run_at: str | None = None,
    ) -> dict:
        """Unified hourly series for the chart panel.

        Three usage modes:

        * ``past_hours`` (default, back-compat) — last N hours from recorder plus the
          full plan horizon into the future.
        * ``start`` ISO — past hours from ``start`` up to ``end`` (or now), then
          the plan horizon if ``end`` is in the future / omitted.
        * ``start`` + ``end`` — strict window; plan slots only included when they
          fall inside it.

        Each hour carries every field the chart needs to distinguish real vs
        forecast data, confirmed vs forecast prices, the planned inverter mode,
        per-device consumption breakdown and PLN-per-hour cost.

        ``forecast_lead`` picks how many hours out each past hour's "prognoza"
        comparison is read from (0 = the freshest plan made as the hour began).
        ``forecast_run_at`` (ISO datetime) instead pins the ENTIRE prognoza to
        the single vintage in force at that moment (the newest one made at or
        before it) — hours that vintage never covered show realized data only.
        It takes precedence over ``forecast_lead``.
        """
        from .const import (
            CONF_BATTERY_CHARGE_SENSOR,
            CONF_BATTERY_DISCHARGE_SENSOR,
            CONF_CONSUMPTION_SENSOR,
            CONF_DEVICE_SENSORS,
            CONF_EV_BEHIND_METER,
            CONF_EV_CHARGE_EFFICIENCY,
            CONF_EV_CHARGER_KW,
            CONF_EV_CHARGER_PHASES,
            CONF_EV_ENERGY_ADDED_SENSOR,
            CONF_EV_SOC_SENSOR,
            CONF_GRID_IMPORT_SENSOR,
            CONF_SENSOR_PARENTS,
        )
        from .hierarchy import exclusive_series

        real_now = dt_util.now()
        now = real_now.replace(minute=0, second=0, microsecond=0)  # current hour start

        # Resolve [past_start, past_end] window for recorder reads.
        if start:
            try:
                past_start = dt_util.as_local(dt_util.parse_datetime(start)).replace(
                    minute=0, second=0, microsecond=0
                )
            except (TypeError, ValueError, AttributeError):
                past_start = now - timedelta(hours=past_hours)
        else:
            past_start = now - timedelta(hours=past_hours)
        if end:
            try:
                window_end = dt_util.as_local(dt_util.parse_datetime(end)).replace(
                    minute=0, second=0, microsecond=0
                )
            except (TypeError, ValueError, AttributeError):
                window_end = None
        else:
            window_end = None
        past_end = min(window_end, now) if window_end else now
        if past_end < past_start:
            past_end = past_start

        learned = self.consumption.base.observed_days > 0

        # Recorder reads: main consumption, SoC, per-device.
        main_sensor = self.config.get(CONF_CONSUMPTION_SENSOR)
        main_real: dict = {}
        if main_sensor and past_end > past_start:
            main_real = await self.consumption.async_range_kwh(
                main_sensor, past_start - timedelta(hours=1), past_end
            )
        soc_real = (
            await self._recent_soc(past_start - timedelta(hours=1), past_end)
            if past_end > past_start
            else {}
        )
        ev_soc_real = (
            await self._recent_soc(
                past_start - timedelta(hours=1),
                past_end,
                self.config.get(CONF_EV_SOC_SENSOR),
            )
            if past_end > past_start
            else {}
        )
        device_ids = list(self.config.get(CONF_DEVICE_SENSORS) or [])
        device_real: dict[str, dict] = {}
        for eid in device_ids:
            if past_end > past_start:
                device_real[eid] = await self.consumption.async_range_kwh(
                    eid, past_start - timedelta(hours=1), past_end
                )
            else:
                device_real[eid] = {}

        # Collapse nested meters into exclusive (own) energy so the stacked
        # device bars sum to the main reading instead of double-counting a
        # sub-meter that lives inside another sub-meter.
        if main_sensor:
            parents = self.config.get(CONF_SENSOR_PARENTS) or {}
            exclusive_real = exclusive_series(
                main_sensor, device_ids, parents, {main_sensor: main_real, **device_real}
            )
            device_real = {eid: exclusive_real.get(eid, {}) for eid in device_ids}

        # Optional real battery / grid sensors (kW or kWh, auto-detected).
        async def _read_opt(conf_key: str) -> dict:
            sensor = self.config.get(conf_key)
            if not sensor or past_end <= past_start:
                return {}
            return await self.consumption.async_range_kwh(
                sensor, past_start - timedelta(hours=1), past_end
            )

        bat_charge_real = await _read_opt(CONF_BATTERY_CHARGE_SENSOR)
        bat_discharge_real = await _read_opt(CONF_BATTERY_DISCHARGE_SENSOR)
        grid_import_real = await _read_opt(CONF_GRID_IMPORT_SENSOR)
        # Realized EV charging per hour from the session energy-added counter
        # (total_increasing; async_range_kwh already ignores session resets).
        ev_charge_real = await _read_opt(CONF_EV_ENERGY_ADDED_SENSOR)

        # Realized inverter mode from the measured battery flows (so the history
        # shows what the inverter *actually* did, not a plan). Charging wins ties.
        _mode_eps = 0.05  # kWh — ignore sensor noise / negligible flow

        def _real_mode(charge: float | None, discharge: float | None) -> str:
            c, d = charge or 0.0, discharge or 0.0
            if c > _mode_eps and c >= d:
                return InverterMode.CHARGE
            if d > _mode_eps:
                return InverterMode.DISCHARGE
            return InverterMode.PASSTHROUGH

        def _side(
            grid: float | None = None,
            discharge: float | None = None,
            base: float | None = None,
            ev: float | None = None,
            charge: float | None = None,
            devices: dict | None = None,
            soc_start: float | None = None,
            soc_end: float | None = None,
            ev_soc_start: float | None = None,
            ev_soc_end: float | None = None,
        ) -> dict | None:
            """One side (realized or forecast) of the tooltip's split breakdown.

            Per-component values keyed so the panel can render the colored
            position breakdown for each side: ``grid``/``discharge`` are the
            up-stack (sources); ``base``/``devices``/``ev``/``charge`` the
            down-stack (consumption). ``None`` when nothing is known for the side.
            """
            dev = {
                k: round(v, 3) for k, v in (devices or {}).items() if v is not None
            }
            scalars = [grid, discharge, base, ev, charge, soc_start, soc_end]
            if all(v is None for v in scalars) and not dev:
                return None

            def _r(v: float | None, d: int = 3) -> float | None:
                return round(v, d) if v is not None else None

            return {
                "grid": _r(grid),
                "discharge": _r(discharge),
                "base": _r(base),
                "ev": _r(ev),
                "charge": _r(charge),
                "devices": dev,
                "soc_start": _r(soc_start, 1),
                "soc_end": _r(soc_end, 1),
                "ev_soc_start": _r(ev_soc_start, 1),
                "ev_soc_end": _r(ev_soc_end, 1),
            }

        hours: list[dict] = []

        # SoC *entering* each hour (start-of-hour state). `decision.battery_soc`
        # and the recorder mean are END-of-hour values, so to draw the SoC line
        # rising/falling across the bar that caused the change, the chart needs
        # the value the battery enters each hour with. Track it as we walk
        # forward; seed from the hour just before the window (the recorder reads
        # one extra hour back exactly for this).
        prev_soc = soc_real.get(past_start - timedelta(hours=1))
        prev_ev_soc = ev_soc_real.get(past_start - timedelta(hours=1))

        # Forecast read, honouring the requested lead.
        #
        # lead 0 keeps the freshest per-hour read (`run0_at`, blank when no plan
        # was made at that exact hour).
        #
        # lead N ("−6h" etc.) pins the ENTIRE prognoza — past and future — to the
        # SINGLE plan made ~N hours ago and reads its own coherent trajectory. SoC
        # then rises only where THAT plan charges. Sliding a different vintage
        # under each hour (the old behaviour) stitched re-seeded plans into a
        # nonsensical SoC line — phantom rises in hours with no planned charging.
        # Hours the pinned plan never covered (before it was made / past its
        # horizon) get a blank forecast, which is correct: it said nothing there.
        sn = self.snapshots
        lead = max(int(forecast_lead or 0), 0)
        pin_key: str | None = None
        if forecast_run_at:
            picked = dt_util.parse_datetime(forecast_run_at)
            # The vintage in force at the picked moment: the newest plan made
            # at or before it (plans are recorded once per clock hour).
            pin_key = sn.nearest_run_at(picked) if picked else None
        elif lead > 0:
            pin_key = sn.nearest_run_at(now - timedelta(hours=lead))
        pin_requested = bool(forecast_run_at) or lead > 0
        pin_rec = sn.get(pin_key) if pin_key else None
        pin_start = (
            dt_util.parse_datetime(pin_rec.get("start") or "") if pin_rec else None
        )
        pin_n = (
            int(
                pin_rec.get("n")
                or pin_rec.get("horizon_hours")
                or len(pin_rec.get("soc") or [])
            )
            if pin_rec
            else 0
        )

        def _pin_idx(hour: datetime) -> int | None:
            if pin_start is None:
                return None
            idx = round((hour - pin_start).total_seconds() / 3600.0)
            return idx if 0 <= idx < pin_n else None

        def _pin_val(hour: datetime, key: str):
            idx = _pin_idx(hour)
            if idx is None:
                return None
            seq = pin_rec.get(key) or []
            return seq[idx] if idx < len(seq) else None

        def _fc(hour: datetime, key: str):
            if pin_requested:
                return _pin_val(hour, key)
            return sn.run0_at(hour, key)

        def _fc_origin(hour: datetime) -> str | None:
            """When the forecast shown for ``hour`` was made (vintage run time)."""
            if pin_requested:
                return pin_key if _pin_idx(hour) is not None else None
            return sn.origin_at(hour, 0)

        def _pin_mode(hour: datetime) -> str | None:
            """Planned inverter mode from the pinned plan (drives the mode band)."""
            code = _pin_val(hour, "mode")
            return MODE_CODE_INV.get(code) if code else None

        # Trips for the away-window shading + tooltip. Stale lead → only the
        # events the pinned plan knew about. Live view → current calendar trips
        # plus already-started history harvested from vintages, so past events
        # removed from the calendar stay visible where they shaped plans, while
        # edited future events do not leave ghost copies from older snapshots.
        series_end = (
            window_end
            or (
                self.data.forecast.slots[-1].start + timedelta(hours=1)
                if self.data and self.data.forecast.slots
                else past_end
            )
        )
        if pin_requested:
            trips = list((pin_rec or {}).get("trips") or [])
        else:
            live = self.ev.trips_payload()

            def _window(t: dict) -> tuple[datetime, datetime] | None:
                depart = dt_util.parse_datetime(t.get("depart") or "")
                ret = dt_util.parse_datetime(t.get("return_end") or "")
                return (depart, ret) if depart and ret else None

            live_windows = [w for w in map(_window, live) if w]

            def _clashes_with_live(t: dict) -> bool:
                w = _window(t)
                if w is None:
                    return True  # unparseable history row — cannot place it
                return any(w[0] < ret and w[1] > dep for dep, ret in live_windows)

            # The live calendar is the current truth wherever it says anything:
            # a harvested trip overlapping a live one is either the same event
            # (already in ``live``) or its stale pre-edit copy — drop it.
            trips = sorted(
                [
                    t
                    for t in sn.trips_overlapping(
                        past_start, series_end, started_at_or_before=real_now
                    )
                    if not _clashes_with_live(t)
                ]
                + live,
                key=lambda t: t.get("depart") or "",
            )

        # EV wired BEFORE the meters (its own tap): the house sensors never see
        # the charging, so the realized grid import and hour costs get the
        # car's session energy added back (grid side = added ÷ charging
        # efficiency). Hours inside a trip window are skipped — away charging
        # is not home import.
        ev_behind_meter = bool(self.config.get(CONF_EV_BEHIND_METER, True))
        ev_charge_eff = max(
            float(self.config.get(CONF_EV_CHARGE_EFFICIENCY, 1.0) or 1.0), 0.01
        )
        away_windows: list[tuple[datetime, datetime]] = []
        for t in trips:
            dep = dt_util.parse_datetime(t.get("depart") or "")
            ret = dt_util.parse_datetime(t.get("return_end") or "")
            if dep and ret:
                away_windows.append((dep, ret))

        def _unmetered_ev_grid(hour: datetime, added_kwh: float | None) -> float:
            if ev_behind_meter or not added_kwh:
                return 0.0
            hour_end = hour + timedelta(hours=1)
            if any(dep < hour_end and ret > hour for dep, ret in away_windows):
                return 0.0
            return added_kwh / ev_charge_eff

        # A charger told to run to the end of an hour stops a little late, so a
        # few minutes of the PREVIOUS hour's planned session land in this one.
        # That much charging after a planned hour is a session finishing, not an
        # unplanned one — flagging it would cry wolf on ordinary days.
        ev_spill_kwh = (
            float(self.config.get(CONF_EV_CHARGER_KW, 0) or 0)
            * max(int(self.config.get(CONF_EV_CHARGER_PHASES, 1) or 1), 1)
            * _EV_SPILL_MINUTES
            / 60.0
        )

        def _planned_ev(hour: datetime) -> float:
            """Most EV energy any plan of ``hour`` asked for — its vintage or any
            mid-hour revision of it."""
            values = [sn.run0_at(hour, "ev")] + [
                r.get("ev") for r in sn.revisions_at(hour)
            ]
            return max((value or 0.0) for value in values)

        def _ev_off_plan(hour: datetime, realized_ev: float | None) -> bool:
            """Did the car charge in an hour no plan ever asked it to?

            Judged against the hour's OWN plans — its vintage plus every recorded
            mid-hour revision — never the pinned view: "was this charging planned"
            must not change its answer when the forecast-lead picker moves. This
            is what tells a deliberate re-plan (the cable went in mid-hour, a
            revision records it) apart from charging nothing in PowerPilot asked
            for. Hours with no vintage at all are not flagged — there was no plan
            to contradict — and neither are trip hours, where the charging
            happened away from home. Neither are hours older than revision
            tracking itself: their re-plans went unrecorded, so their silence
            means "nobody was looking", not "nothing was planned".
            """
            if realized_ev is None:
                return False
            if sn.origin_at(hour, 0) is None:
                return False
            if not sn.records_revisions_at(hour):
                return False
            hour_end = hour + timedelta(hours=1)
            if any(dep < hour_end and ret > hour for dep, ret in away_windows):
                return False
            if _planned_ev(hour) > _REVISION_EV_EPS:
                return False
            floor = _REVISION_EV_EPS
            if _planned_ev(hour - timedelta(hours=1)) > _REVISION_EV_EPS:
                floor = max(floor, ev_spill_kwh)
            return realized_ev > floor

        # ----- Past hours -----
        h = past_start
        while h < past_end:
            wd, hr = h.weekday(), h.hour
            base_fc = self.consumption.base_value(wd, hr) if learned else None
            dev_forecast = {
                eid: self._device_forecast_kwh(eid, h) for eid in device_ids
            }
            dev_real_h = {
                eid: round(device_real[eid][h], 3) if h in device_real.get(eid, {}) else None
                for eid in device_ids
            }
            forecast_c = (
                round(
                    (base_fc or 0.0)
                    + sum(v for v in dev_forecast.values() if v is not None),
                    3,
                )
                if learned
                else None
            )
            buy_price = self.prices.price_at(h)
            dist_price = self.tariff.snapshot_for(h)
            # Per-kWh price excludes the fixed monthly charge (kept separately).
            total_price = (
                buy_price + dist_price
                if buy_price is not None and dist_price is not None
                else None
            )
            g_real, d_real = grid_import_real.get(h), bat_discharge_real.get(h)
            c_real, m_real = bat_charge_real.get(h), main_real.get(h)
            if g_real is not None:
                # Pre-meter EV tap: fold the unmetered charging back into the
                # realized import (0.0 when the charger is behind the meters).
                g_real += _unmetered_ev_grid(h, ev_charge_real.get(h))
            # Realized hourly costs (PLN/h) reconstructed from the measured flows:
            # grid spend = imported kWh × the hour's gross price; battery-served
            # cost = discharged kWh × the modelled in-battery cost at that hour.
            bcost = self.snapshots.value_at(h, "bcost")
            real_hour_cost = (
                round(g_real * total_price, 4)
                if g_real is not None and total_price is not None
                else None
            )
            real_energy_cost = (
                round(g_real * buy_price, 4)
                if g_real is not None and buy_price is not None
                else None
            )
            real_dist_cost = (
                round(g_real * dist_price, 4)
                if g_real is not None and dist_price is not None
                else None
            )
            real_bat_use_cost = (
                round(d_real * bcost, 4)
                if d_real is not None and bcost is not None
                else None
            )
            dev_real_sum = sum(v for v in dev_real_h.values() if v is not None)
            base_real = (
                max(0.0, m_real - dev_real_sum) if m_real is not None else None
            )
            realized_side = _side(
                grid=g_real,
                discharge=d_real,
                base=base_real,
                ev=ev_charge_real.get(h),
                charge=c_real,
                devices=dev_real_h,
                soc_start=prev_soc,
                soc_end=soc_real.get(h),
                ev_soc_start=prev_ev_soc,
                ev_soc_end=ev_soc_real.get(h),
            )
            # Forecast side = the single plan made AT this hour (vintage run_at==h,
            # index 0), so soc/charge/grid all belong to one coherent trajectory.
            # soc_start is the real SoC the battery entered the hour with — the
            # value that plan seeded from — so the forecast SoC delta matches its
            # own charge instead of stitching two re-seeded vintages together.
            # Battery flows are captured only from now on → blank for old vintages.
            # Only surface a forecast side when a plan/vintage actually backs this
            # hour: the learned consumption profile is keyed by weekday+hour and is
            # always available, so without this gate it would masquerade as a
            # "prognoza" for hours no plan was ever made for (dates predating the
            # addon, or older than snapshot retention).
            fc_origin = _fc_origin(h)
            if fc_origin is not None:
                fc_soc_end = _fc(h, "soc")
                fc_ev_soc_end = _fc(h, "ev_soc")
                forecast_side = _side(
                    grid=_fc(h, "grid"),
                    discharge=_fc(h, "dischg"),
                    base=base_fc,
                    ev=_fc(h, "ev"),
                    charge=_fc(h, "charge"),
                    devices=dev_forecast,
                    soc_start=prev_soc if fc_soc_end is not None else None,
                    soc_end=fc_soc_end,
                    # Planned EV SoC (end-of-hour) for the dashed forecast line;
                    # blank for vintages predating EV-SoC capture.
                    ev_soc_start=prev_ev_soc if fc_ev_soc_end is not None else None,
                    ev_soc_end=fc_ev_soc_end,
                )
            else:
                forecast_side = None
            # Mid-hour re-plans of this hour + the "nobody planned this" flag, so
            # the tooltip can say whether a divergence was a decision or a
            # surprise. Always the hour's own record, independent of the pin.
            revisions = sn.revisions_at(h)
            hours.append(
                {
                    "start": h.isoformat(),
                    "is_past": True,
                    "realized": realized_side,
                    "forecast": forecast_side,
                    "forecast_origin": fc_origin if forecast_side else None,
                    "revisions": revisions or None,
                    "revisions_dropped": sn.revisions_dropped_at(h) or None,
                    "ev_off_plan": _ev_off_plan(h, ev_charge_real.get(h)) or None,
                    "buy_price": buy_price,
                    "distribution_price_kwh": dist_price,
                    "total_price_kwh": total_price,
                    "price_confirmed": self.prices.is_confirmed(h),
                    "price_type": self.prices.price_type_at(h),
                    "consumption_real": round(main_real[h], 3) if h in main_real else None,
                    # Consumption forecast (learned profile) is only meaningful for
                    # hours a plan actually backs — otherwise it would show a
                    # "prognoza" for dates no plan was ever made for.
                    "consumption_forecast": forecast_c if fc_origin is not None else None,
                    "base_consumption_forecast": (
                        round(base_fc, 3)
                        if base_fc is not None and fc_origin is not None
                        else None
                    ),
                    "soc": round(soc_real[h], 1) if h in soc_real else None,
                    "ev_soc": round(ev_soc_real[h], 1) if h in ev_soc_real else None,
                    "battery_soc_start": round(prev_soc, 1) if prev_soc is not None else None,
                    # At a pinned forecast the mode band follows that plan's
                    # schedule for the hours it covered; outside its coverage
                    # (and without a pin) it shows the realized mode.
                    "inverter_mode": (
                        _pin_mode(h)
                        if pin_requested and _pin_idx(h) is not None
                        else _real_mode(bat_charge_real.get(h), bat_discharge_real.get(h))
                    ),
                    # The mode THIS hour's own plan chose as it began (vintage
                    # index 0) — the "plan" side of the plan-vs-real comparison.
                    "planned_mode": MODE_CODE_INV.get(sn.run0_at(h, "mode") or ""),
                    "battery_charge_kwh": round(bat_charge_real[h], 3) if h in bat_charge_real else None,
                    "battery_discharge_kwh": round(bat_discharge_real[h], 3) if h in bat_discharge_real else None,
                    # Planned grid-side charge setpoint for this hour, from the
                    # plan at the requested lead (blank for hours predating capture).
                    "charge_power_kw": _fc(h, "charge_pw"),
                    # Realized battery energy cost from the vintage recorded at h
                    # (no live sensor exists for it). Blank for hours predating
                    # snapshot capture of this field.
                    "battery_energy_cost": bcost,
                    "grid_buy_kwh": round(grid_import_real[h], 3) if h in grid_import_real else None,
                    # Realized EV charge from the energy-added meter; when that
                    # sensor isn't configured, fall back to the planned EV charge
                    # recorded for this hour so charging stays visible.
                    "ev_charge_kwh": (
                        round(ev_charge_real[h], 3)
                        if h in ev_charge_real
                        else sn.run0_at(h, "ev")
                    ),
                    # Planned charging minutes from the plan made at this hour
                    # (tooltip context; blank for vintages predating capture).
                    "ev_charge_minutes": _fc(h, "ev_min"),
                    "hour_cost": real_hour_cost,
                    "energy_cost": real_energy_cost,
                    "distribution_cost": real_dist_cost,
                    "battery_use_cost": real_bat_use_cost,
                    "fixed_cost": self.tariff.fixed_hourly_for(h),
                    "devices_real": dev_real_h,
                    "devices_forecast": dev_forecast if fc_origin is not None else None,
                }
            )
            if h in soc_real:
                prev_soc = soc_real[h]
            if h in ev_soc_real:
                prev_ev_soc = ev_soc_real[h]
            h += timedelta(hours=1)

        # ----- Current (in-progress) hour: realized-so-far + whole-hour forecast -----
        # The current clock hour is part realized (elapsed, from 5-min stats) and
        # part forecast. We draw the realized-so-far bar (so SoC and bars agree)
        # and the tooltip shows both sides: realized up to ``real_now`` and the
        # plan's forecast for the whole hour.
        # Only when the live hour actually falls inside the requested window —
        # a past-day window ends at/before `now`, a future-day window starts
        # after it; in both cases the in-progress hour is off-screen.
        emitted_current = False
        if (
            real_now > now
            and now >= past_start
            and (window_end is None or window_end > now)
        ):

            async def _partial(conf_key: str) -> float | None:
                sensor = self.config.get(conf_key)
                if not sensor:
                    return None
                return await self.consumption.async_partial_kwh(sensor, now, real_now)

            cur_charge = await _partial(CONF_BATTERY_CHARGE_SENSOR)
            cur_discharge = await _partial(CONF_BATTERY_DISCHARGE_SENSOR)
            cur_grid = await _partial(CONF_GRID_IMPORT_SENSOR)
            cur_ev = await _partial(CONF_EV_ENERGY_ADDED_SENSOR)
            if cur_grid is not None:
                # Pre-meter EV tap (see _unmetered_ev_grid): the running hour's
                # realized import and costs must include the ongoing charging.
                cur_grid += _unmetered_ev_grid(now, cur_ev)
            cur_main = (
                await self.consumption.async_partial_kwh(main_sensor, now, real_now)
                if main_sensor
                else None
            )
            cur_devices: dict[str, float | None] = {}
            for eid in device_ids:
                cur_devices[eid] = await self.consumption.async_partial_kwh(
                    eid, now, real_now
                )
            live_soc = self._read_soc()
            wd, hr = now.weekday(), now.hour
            dev_forecast = {
                eid: self._device_forecast_kwh(eid, now) for eid in device_ids
            }
            buy_price = self.prices.price_at(now)
            dist_price = self.tariff.distribution_for(now)
            # Per-kWh price excludes the fixed monthly charge (kept separately).
            total_price = (
                buy_price + dist_price
                if buy_price is not None and dist_price is not None
                else None
            )

            # Realized-so-far per-component breakdown.
            cur_dev_sum = sum(v for v in cur_devices.values() if v is not None)
            cur_base = max(0.0, cur_main - cur_dev_sum) if cur_main is not None else None
            cur_ev_soc = ev_soc_real.get(now)
            if cur_ev_soc is None:
                cur_ev_soc = self.ev.soc
            realized_side = _side(
                grid=cur_grid,
                discharge=cur_discharge,
                base=cur_base,
                ev=cur_ev,
                charge=cur_charge,
                devices=cur_devices,
                soc_start=prev_soc,
                soc_end=live_soc,
                ev_soc_start=prev_ev_soc,
                ev_soc_end=cur_ev_soc,
            )
            # Whole-hour forecast for the current hour, from today's plan's
            # current-hour decision.
            cur_dec = cur_slot = None
            forecast_side = None
            cur_fc_origin: str | None = None
            if self.data and self.data.decisions:
                for _sl, _dc in zip(self.data.forecast.slots, self.data.decisions):
                    if _sl.start == now:
                        cur_slot, cur_dec = _sl, _dc
                        break
            if pin_requested:
                # Keep the current hour on the SAME pinned plan as the rest of
                # the pinned prognoza, so the dashed line has no one-hour blip
                # at "now". Blank when the pinned plan didn't reach this hour.
                if _fc_origin(now) is not None:
                    fc_soc_end = _pin_val(now, "soc")
                    fc_ev_soc_end = _pin_val(now, "ev_soc")
                    forecast_side = _side(
                        grid=_pin_val(now, "grid"),
                        discharge=_pin_val(now, "dischg"),
                        base=self.consumption.base_value(wd, hr) if learned else None,
                        ev=_pin_val(now, "ev"),
                        charge=_pin_val(now, "charge"),
                        devices=dev_forecast,
                        soc_start=prev_soc if fc_soc_end is not None else None,
                        soc_end=fc_soc_end,
                        ev_soc_start=prev_ev_soc if fc_ev_soc_end is not None else None,
                        ev_soc_end=fc_ev_soc_end,
                    )
                    cur_fc_origin = _fc_origin(now)
            elif cur_dec is not None:
                forecast_side = _side(
                    grid=cur_dec.grid_buy_kwh,
                    discharge=cur_dec.battery_discharge_kwh,
                    base=cur_slot.base_consumption_kwh,
                    ev=cur_dec.ev_charge_kwh,
                    charge=cur_dec.battery_charge_kwh,
                    devices=dev_forecast,
                    soc_start=prev_soc,
                    soc_end=cur_dec.battery_soc,
                    ev_soc_start=prev_ev_soc,
                    ev_soc_end=cur_dec.ev_soc,
                )
                cur_fc_origin = (
                    self.data.created_at.isoformat()
                    if self.data and self.data.created_at
                    else sn.origin_at(now, 0)
                )
            cur_revisions = sn.revisions_at(now)
            hours.append(
                {
                    "start": now.isoformat(),
                    "is_past": True,
                    "partial": True,
                    "partial_until": real_now.isoformat(),
                    "realized": realized_side,
                    "forecast": forecast_side,
                    "forecast_origin": cur_fc_origin if forecast_side else None,
                    "revisions": cur_revisions or None,
                    "revisions_dropped": sn.revisions_dropped_at(now) or None,
                    "ev_off_plan": _ev_off_plan(now, cur_ev) or None,
                    "buy_price": buy_price,
                    "distribution_price_kwh": dist_price,
                    "total_price_kwh": total_price,
                    "price_confirmed": self.prices.is_confirmed(now),
                    "price_type": self.prices.price_type_at(now),
                    "consumption_real": round(cur_main, 3) if cur_main is not None else None,
                    "consumption_forecast": None,
                    "base_consumption_forecast": None,
                    "soc": round(live_soc, 1) if live_soc else None,
                    "ev_soc": (
                        round(ev_soc_real[now], 1)
                        if now in ev_soc_real
                        else (round(cur_ev_soc, 1) if cur_ev_soc is not None else None)
                    ),
                    "battery_soc_start": round(prev_soc, 1) if prev_soc is not None else None,
                    # The in-progress hour shows the COMMITTED decision's mode
                    # (what the inverter is actually steered to — the same value
                    # the inverter-mode sensor reports). The measured flows lag
                    # early in the hour: a few minutes of 5-minute statistics
                    # sit under the noise threshold and read as "passthrough"
                    # even while the battery is discharging.
                    "inverter_mode": (
                        _pin_mode(now)
                        if pin_requested and _pin_idx(now) is not None
                        else (
                            cur_dec.inverter_mode
                            if cur_dec is not None
                            else _real_mode(cur_charge, cur_discharge)
                        )
                    ),
                    # Plan-vs-real baseline for the in-progress hour: the plan
                    # the hour STARTED with (its own vintage, full-hour values).
                    # The live decision above may already be a mid-hour re-plan
                    # covering only the remainder — comparing realized flows
                    # against that would always read "on plan".
                    "planned_mode": MODE_CODE_INV.get(sn.run0_at(now, "mode") or ""),
                    "hour_plan": (
                        {
                            "charge": sn.run0_at(now, "charge"),
                            "discharge": sn.run0_at(now, "dischg"),
                            "ev": sn.run0_at(now, "ev"),
                            "ev_minutes": sn.run0_at(now, "ev_min"),
                            "grid": sn.run0_at(now, "grid"),
                            "consumption": sn.run0_at(now, "cons_fc"),
                            "soc_end": sn.run0_at(now, "soc"),
                            "charge_power_kw": sn.run0_at(now, "charge_pw"),
                        }
                        if sn.origin_at(now, 0) is not None
                        else None
                    ),
                    "battery_charge_kwh": round(cur_charge, 3) if cur_charge is not None else None,
                    "battery_discharge_kwh": round(cur_discharge, 3) if cur_discharge is not None else None,
                    # Plan's grid-side charge setpoint for this hour (forecast side).
                    "charge_power_kw": (
                        round(cur_dec.charge_power_kw, 3) if cur_dec is not None else None
                    ),
                    "battery_energy_cost": round(self._battery_energy_cost, 4),
                    "grid_buy_kwh": round(cur_grid, 3) if cur_grid is not None else None,
                    # Realized so far, else the plan's EV charge for this hour.
                    "ev_charge_kwh": (
                        round(cur_ev, 3)
                        if cur_ev is not None
                        else (round(cur_dec.ev_charge_kwh, 3) if cur_dec is not None else None)
                    ),
                    "ev_charge_minutes": (
                        cur_dec.ev_charge_minutes if cur_dec is not None else None
                    ),
                    # Realized-so-far costs for the in-progress hour (partial flows).
                    "hour_cost": (
                        round(cur_grid * total_price, 4)
                        if cur_grid is not None and total_price is not None
                        else None
                    ),
                    "energy_cost": (
                        round(cur_grid * buy_price, 4)
                        if cur_grid is not None and buy_price is not None
                        else None
                    ),
                    "distribution_cost": (
                        round(cur_grid * dist_price, 4)
                        if cur_grid is not None and dist_price is not None
                        else None
                    ),
                    "battery_use_cost": (
                        round(cur_discharge * self._battery_energy_cost, 4)
                        if cur_discharge is not None
                        else None
                    ),
                    "fixed_cost": self.tariff.fixed_hourly_for(now),
                    "devices_real": {
                        eid: round(v, 3) if v is not None else None
                        for eid, v in cur_devices.items()
                    },
                    "devices_forecast": dev_forecast,
                }
            )
            emitted_current = True
            # Seed the FUTURE forecast rows from the current decision's planned
            # end-of-hour state, not the live reading: energy planned for the
            # rest of this hour (e.g. EV charging that hasn't started yet) is
            # in the decision's end state, and seeding from the live value made
            # the whole delta appear as a phantom jump on the NEXT hour's
            # tooltip ("21 → 28 %" with no charging in that hour).
            if cur_dec is not None and not pin_requested:
                prev_soc = cur_dec.battery_soc
                if cur_dec.ev_soc is not None:
                    prev_ev_soc = cur_dec.ev_soc
                elif cur_ev_soc is not None:
                    prev_ev_soc = cur_ev_soc
            else:
                if live_soc:
                    prev_soc = live_soc
                if cur_ev_soc is not None:
                    prev_ev_soc = cur_ev_soc

        # ----- Future hours from plan -----
        plan = self.data
        # If there was no past window to seed from, start the future SoC line at
        # the live SoC the optimizer began planning from.
        if prev_soc is None:
            live_soc = self._read_soc()
            prev_soc = live_soc if live_soc else None
        # When the current hour is shown as a realized partial, the plan's own
        # current-hour slot is a duplicate → forecast starts at the next hour.
        forecast_cutoff = (now + timedelta(hours=1)) if emitted_current else past_end

        # With a pinned forecast (lead N or an exact run_at) the future side
        # continues the SAME pinned plan used for the past (`_pin_val`), so the
        # dashed prognoza is one coherent line across the whole horizon and can
        # be read against the live plan (the solid line). Without a pin the
        # future side is just the live plan.
        # Seed the pinned forecast SoC from the vintage's own state entering the
        # first shown future hour, so its dashed trajectory chains coherently.
        fc_prev_soc = _pin_val(forecast_cutoff - timedelta(hours=1), "soc")
        if fc_prev_soc is None:
            fc_prev_soc = prev_soc
        fc_prev_ev_soc = _pin_val(forecast_cutoff - timedelta(hours=1), "ev_soc")
        if fc_prev_ev_soc is None:
            fc_prev_ev_soc = prev_ev_soc

        if plan:
            for slot, decision in zip(plan.forecast.slots, plan.decisions):
                if window_end and slot.start >= window_end:
                    break
                if slot.start < forecast_cutoff:
                    # Plan slot already covered by past / current-partial → skip.
                    continue
                dev_forecast = {
                    eid: self._device_forecast_kwh(eid, slot.start)
                    for eid in device_ids
                }
                if pin_requested:
                    # Dashed forecast continues the pinned plan's trajectory.
                    fc_soc_end = _pin_val(slot.start, "soc")
                    fc_ev_soc_end = _pin_val(slot.start, "ev_soc")
                    forecast_side = _side(
                        grid=_pin_val(slot.start, "grid"),
                        discharge=_pin_val(slot.start, "dischg"),
                        base=slot.base_consumption_kwh,
                        ev=_pin_val(slot.start, "ev"),
                        charge=_pin_val(slot.start, "charge"),
                        devices=dev_forecast,
                        soc_start=fc_prev_soc if fc_soc_end is not None else None,
                        soc_end=fc_soc_end,
                        ev_soc_start=fc_prev_ev_soc if fc_ev_soc_end is not None else None,
                        ev_soc_end=fc_ev_soc_end,
                    )
                    forecast_origin = pin_key if _pin_idx(slot.start) is not None else None
                    if fc_soc_end is not None:
                        fc_prev_soc = fc_soc_end
                    if fc_ev_soc_end is not None:
                        fc_prev_ev_soc = fc_ev_soc_end
                else:
                    forecast_side = _side(
                        grid=decision.grid_buy_kwh,
                        discharge=decision.battery_discharge_kwh,
                        base=slot.base_consumption_kwh,
                        ev=decision.ev_charge_kwh,
                        charge=decision.battery_charge_kwh,
                        devices=dev_forecast,
                        soc_start=prev_soc,
                        soc_end=decision.battery_soc,
                        ev_soc_start=prev_ev_soc,
                        ev_soc_end=decision.ev_soc,
                    )
                    # Future side comes from the current live plan.
                    forecast_origin = (
                        plan.created_at.isoformat() if plan.created_at else None
                    )
                hours.append(
                    {
                        "start": slot.start.isoformat(),
                        "is_past": False,
                        "realized": None,
                        "forecast": forecast_side,
                        "forecast_origin": forecast_origin,
                        "buy_price": slot.buy_price,
                        "distribution_price_kwh": slot.distribution_price_kwh,
                        "total_price_kwh": slot.total_price_kwh,
                        "price_confirmed": slot.price_confirmed,
                        "price_type": self._slot_ptype(slot),
                        "consumption_real": None,
                        "consumption_forecast": round(slot.total_consumption_kwh, 3),
                        "base_consumption_forecast": round(slot.base_consumption_kwh, 3),
                        "soc": round(decision.battery_soc, 1),
                        "ev_soc": (
                            round(decision.ev_soc, 1)
                            if decision.ev_soc is not None
                            else None
                        ),
                        "battery_soc_start": round(prev_soc, 1) if prev_soc is not None else None,
                        "inverter_mode": (
                            _pin_mode(slot.start)
                            if pin_requested and _pin_idx(slot.start) is not None
                            else decision.inverter_mode
                        ),
                        "battery_charge_kwh": round(decision.battery_charge_kwh, 3),
                        "battery_discharge_kwh": round(decision.battery_discharge_kwh, 3),
                        "charge_power_kw": round(decision.charge_power_kw, 3),
                        "battery_energy_cost": round(decision.battery_energy_cost, 4),
                        "grid_buy_kwh": round(decision.grid_buy_kwh, 3),
                        "ev_charge_kwh": round(decision.ev_charge_kwh, 3),
                        "ev_charge_minutes": (
                            _pin_val(slot.start, "ev_min")
                            if pin_requested
                            else decision.ev_charge_minutes
                        ),
                        "hour_cost": round(decision.hour_cost, 4),
                        "energy_cost": round(decision.energy_cost, 4),
                        "distribution_cost": round(decision.distribution_cost, 4),
                        "battery_use_cost": round(decision.battery_use_cost, 4),
                        "fixed_cost": round(decision.fixed_cost, 4),
                        "devices_real": {eid: None for eid in device_ids},
                        "devices_forecast": dev_forecast,
                    }
                )
                prev_soc = decision.battery_soc
                if decision.ev_soc is not None:
                    prev_ev_soc = decision.ev_soc

        # At a pinned forecast the hours the vintage covered are pinned to it:
        # bars, costs and prices come from THAT plan, not from realized data —
        # otherwise switching the pin only moved the tooltip / dashed lines and
        # the view mixed "reality" bars with a past plan's context. Hours the
        # plan never covered (before it was made / past its horizon) KEEP their
        # realized / live values — blanking them wiped all history left of the
        # pin marker off the chart. The solid realized SoC lines and the
        # tooltip's "realne" column stay untouched as the comparison anchor.
        if pin_requested:
            for hour_dict in hours:
                hstart = dt_util.parse_datetime(hour_dict["start"])
                if hstart is None or _pin_idx(hstart) is None:
                    continue

                def p(key: str, _h=hstart):
                    return _pin_val(_h, key)

                buy, dist = p("buy"), p("dist")
                grid, dischg, bcost = p("grid"), p("dischg"), p("bcost")
                hour_dict.update(
                    {
                        "buy_price": buy,
                        "distribution_price_kwh": dist,
                        "total_price_kwh": (
                            buy + dist if buy is not None and dist is not None else None
                        ),
                        "price_confirmed": p("ptype") == "c",
                        "price_type": PTYPE_CODE_INV.get(p("ptype")),
                        "grid_buy_kwh": grid,
                        "battery_discharge_kwh": dischg,
                        "battery_charge_kwh": p("charge"),
                        "charge_power_kw": p("charge_pw"),
                        "ev_charge_kwh": p("ev"),
                        "ev_charge_minutes": p("ev_min"),
                        # Bars fall back to the pinned plan's consumption
                        # forecast (the panel prefers real when present).
                        "consumption_real": None,
                        "consumption_forecast": p("cons_fc"),
                        "base_consumption_forecast": p("base_fc"),
                        "devices_real": {eid: None for eid in device_ids},
                        "hour_cost": p("cost"),
                        "energy_cost": (
                            round(grid * buy, 4)
                            if grid is not None and buy is not None
                            else None
                        ),
                        "distribution_cost": (
                            round(grid * dist, 4)
                            if grid is not None and dist is not None
                            else None
                        ),
                        "battery_use_cost": (
                            round(dischg * bcost, 4)
                            if dischg is not None and bcost is not None
                            else None
                        ),
                        "battery_energy_cost": bcost,
                    }
                )

        # Pinned-vintage metadata: when the prognoza is pinned (lead N or an
        # exact run_at) the panel marks the span that plan actually covered —
        # inside it the hours carry both realne and prognoza, outside only
        # realne, which is how forecast evolution is audited hour by hour.
        forecast_pin: dict | None = None
        if pin_requested and pin_rec is not None and pin_start is not None:
            forecast_pin = {
                "run_at": pin_key,
                "start": pin_start.isoformat(),
                "end": (pin_start + timedelta(hours=pin_n)).isoformat(),
            }

        return {
            # Exact present instant — the panel draws the "teraz" line here.
            "now": real_now.isoformat(),
            "past_hours": past_hours,
            "start": past_start.isoformat(),
            "end": series_end.isoformat(),
            "device_ids": device_ids,
            "hours": hours,
            "trips": trips,
            "forecast_pin": forecast_pin,
        }

    async def async_charging_efficiency(self, days: int = 30) -> dict:
        """Measured charging efficiencies from sensors, next to the configured ones.

        Informational only — the configured values stay authoritative for
        planning. EV: the grid-side charge-meter energy vs the car's
        energy-added counter, overall and bucketed by average charging power
        (so the measured points can be read against the configured efficiency
        curve). Battery: round-trip Σ discharge / Σ charge over the window vs
        the configured η_ch × η_dis (the SoC drift over a 30-day window is
        noise next to the cycled energy).
        """
        from .const import (
            CONF_BATTERY_CHARGE_SENSOR,
            CONF_BATTERY_DISCHARGE_SENSOR,
            CONF_CHARGE_EFFICIENCY,
            CONF_CHARGE_EFFICIENCY_CURVE,
            CONF_DISCHARGE_EFFICIENCY,
            CONF_EV_CHARGE_EFFICIENCY,
            CONF_EV_CHARGE_METER_SENSOR,
            CONF_EV_ENERGY_ADDED_SENSOR,
            DEFAULTS,
        )

        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        start = now - timedelta(days=max(days, 1))
        _MIN_HOUR_KWH = 0.1  # ignore metering noise / idle hours

        async def _series(conf_key: str) -> tuple[str | None, dict]:
            eid = self.config.get(conf_key)
            if not eid:
                return None, {}
            return eid, await self.consumption.async_range_kwh(eid, start, now)

        def _r(value: float | None, nd: int = 3) -> float | None:
            return round(value, nd) if value is not None else None

        # ---- EV: charge meter (grid side) vs energy added (pack side) ----
        grid_eid, grid_series = await _series(CONF_EV_CHARGE_METER_SENSOR)
        added_eid, added_series = await _series(CONF_EV_ENERGY_ADDED_SENSOR)
        # Flat configured efficiency; 1.0 = unset (charging planned lossless).
        ev_eff = float(self.config.get(CONF_EV_CHARGE_EFFICIENCY, 1.0) or 1.0)
        ev_configured = _r(ev_eff, 4) if ev_eff < 1.0 - 1e-9 else None
        ev: dict = {
            "available": bool(grid_eid and added_eid),
            "grid_sensor": grid_eid,
            "added_sensor": added_eid,
            "hours": 0,
            "grid_kwh": None,
            "added_kwh": None,
            "measured_eff": None,
            "configured_eff": ev_configured,
            "buckets": [],
        }
        if grid_series and added_series:
            # Hourly kWh at 1-h slots == average kW → bucket by power (0.5 kW).
            buckets: dict[float, list[float]] = {}
            total_grid = total_added = 0.0
            hours = 0
            for hour, grid_kwh in grid_series.items():
                if grid_kwh < _MIN_HOUR_KWH:
                    continue
                added_kwh = added_series.get(hour, 0.0)
                hours += 1
                total_grid += grid_kwh
                total_added += added_kwh
                key = round(grid_kwh * 2) / 2.0
                agg = buckets.setdefault(key, [0.0, 0.0, 0])
                agg[0] += grid_kwh
                agg[1] += added_kwh
                agg[2] += 1
            ev.update(
                {
                    "hours": hours,
                    "grid_kwh": _r(total_grid),
                    "added_kwh": _r(total_added),
                    "measured_eff": (
                        _r(total_added / total_grid, 4) if total_grid > 0 else None
                    ),
                    "buckets": [
                        {
                            "power_kw": key,
                            "hours": int(n),
                            "grid_kwh": _r(g),
                            "added_kwh": _r(a),
                            "measured_eff": _r(a / g, 4) if g > 0 else None,
                            "configured_eff": ev_configured,
                        }
                        for key, (g, a, n) in sorted(buckets.items())
                    ],
                }
            )

        # ---- Battery: round-trip from the charge/discharge meters ----
        charge_eid, charge_series = await _series(CONF_BATTERY_CHARGE_SENSOR)
        dis_eid, dis_series = await _series(CONF_BATTERY_DISCHARGE_SENSOR)
        eta_c = float(self.config.get(CONF_CHARGE_EFFICIENCY, DEFAULTS[CONF_CHARGE_EFFICIENCY]))
        eta_d = float(self.config.get(CONF_DISCHARGE_EFFICIENCY, DEFAULTS[CONF_DISCHARGE_EFFICIENCY]))
        total_charge = sum(charge_series.values()) if charge_series else 0.0
        total_dis = sum(dis_series.values()) if dis_series else 0.0
        battery = {
            "available": bool(charge_eid and dis_eid),
            "charge_sensor": charge_eid,
            "discharge_sensor": dis_eid,
            "charge_kwh": _r(total_charge),
            "discharge_kwh": _r(total_dis),
            "measured_roundtrip": (
                _r(total_dis / total_charge, 4) if total_charge > 1.0 else None
            ),
            "configured_charge_eff": _r(eta_c, 4),
            "configured_discharge_eff": _r(eta_d, 4),
            "configured_roundtrip": _r(eta_c * eta_d, 4),
            "charge_curve_points": len(
                self.config.get(CONF_CHARGE_EFFICIENCY_CURVE) or []
            ),
        }

        return {
            "generated_at": dt_util.now().isoformat(),
            "window_days": days,
            "ev": ev,
            "battery": battery,
        }

    def ev_control(self) -> dict:
        """Advisory EV control surface for automations.

        PowerPilot decides *when* and *to what* the car should charge; an HA
        automation does the actual steering off these values.
        """
        ev = self.ev
        if not ev.enabled:
            return {
                "enabled": False,
                "connect_charger": False,
                "charging_now": False,
                "charge_start": None,
                "soc_limit": None,
                "soc": None,
                "charge_minutes": None,
            }

        now = dt_util.now().replace(minute=0, second=0, microsecond=0)
        horizon_24h = now + timedelta(hours=24)
        plan = self.data
        charge_start: datetime | None = None
        charge_start_minutes: int | None = None
        connect = False
        charging_now = False
        current = None
        if plan:
            for decision in plan.decisions:
                if decision.ev_charge_kwh <= 0 or decision.start < now:
                    continue
                if charge_start is None or decision.start < charge_start:
                    charge_start = decision.start
                    charge_start_minutes = decision.ev_charge_minutes
                if decision.start < horizon_24h:
                    connect = True
            current = self.current_decision(plan)
            charging_now = bool(current and current.ev_charge)
        if charging_now:
            connect = True

        # Planned charging duration (minutes within the hour, full power) for
        # the automation to time the charger: the active hour's while charging,
        # else the next planned charge hour's.
        charge_minutes = (
            current.ev_charge_minutes
            if charging_now and current
            else charge_start_minutes
        )

        return {
            "enabled": True,
            "connect_charger": connect,
            "charging_now": charging_now,
            "charge_start": charge_start.isoformat() if charge_start else None,
            "soc_limit": ev.soc_limit_now(),
            "soc": ev.soc,
            "charge_minutes": charge_minutes,
        }

    def get_flow(self) -> dict:
        """Live snapshot of the computation pipeline for the "Przepływ" tab.

        Answers "where does the in-battery price come from": which sensors
        feed the model, how the gross price is assembled, where the charge /
        discharge losses and wear cost enter, and what the optimizer decided
        for the current hour. Values are read live so the diagram doubles as a
        sanity check of the inputs.
        """
        from .const import (
            CONF_BATTERY_CAPACITY_KWH,
            CONF_BATTERY_CHARGE_SENSOR,
            CONF_BATTERY_DISCHARGE_SENSOR,
            CONF_BATTERY_WEAR_COST,
            CONF_BUY_PRICE_SENSOR,
            CONF_CHARGE_EFFICIENCY,
            CONF_CHARGE_EFFICIENCY_CURVE,
            CONF_CONSUMPTION_SENSOR,
            CONF_DEVICE_SENSORS,
            CONF_DISCHARGE_EFFICIENCY,
            CONF_EV_ENERGY_ADDED_SENSOR,
            CONF_EV_SOC_SENSOR,
            CONF_EXCISE_KWH,
            CONF_GRID_IMPORT_SENSOR,
            CONF_PRICE_MARKUP,
            CONF_PRICE_SOURCE,
            CONF_PRICE_VAT,
            CONF_SOC_SENSOR,
            CONF_WEATHER_ENTITY,
            DEFAULTS,
        )

        now = dt_util.now().replace(minute=0, second=0, microsecond=0)

        def ent(conf_key: str) -> dict | None:
            eid = self.config.get(conf_key)
            if not eid:
                return None
            state = self.hass.states.get(eid)
            return {
                "entity_id": eid,
                "value": state.state if state else None,
                "unit": (
                    state.attributes.get("unit_of_measurement") if state else None
                ),
                "available": state is not None
                and state.state not in ("unknown", "unavailable"),
            }

        def cfg(key: str, ndigits: int = 4) -> float:
            value = self.config.get(key, DEFAULTS.get(key))
            try:
                return round(float(value), ndigits)
            except (TypeError, ValueError):
                return float(DEFAULTS.get(key) or 0.0)

        buy = self.prices.price_at(now)
        dist = self.tariff.snapshot_for(now)
        eta_c = cfg(CONF_CHARGE_EFFICIENCY)
        eta_d = cfg(CONF_DISCHARGE_EFFICIENCY)
        wear = cfg(CONF_BATTERY_WEAR_COST)
        reservoir_cost = round(self._battery_energy_cost, 4)
        live_soc = self._read_soc()

        current = self.current_decision(self.data) if self.data else None

        return {
            "now": now.isoformat(),
            "inputs": {
                "consumption": ent(CONF_CONSUMPTION_SENSOR),
                "device_sensors": [
                    {"entity_id": eid} for eid in (self.config.get(CONF_DEVICE_SENSORS) or [])
                ],
                "battery_soc": ent(CONF_SOC_SENSOR),
                "battery_charge": ent(CONF_BATTERY_CHARGE_SENSOR),
                "battery_discharge": ent(CONF_BATTERY_DISCHARGE_SENSOR),
                "grid_import": ent(CONF_GRID_IMPORT_SENSOR),
                "buy_price_sensor": ent(CONF_BUY_PRICE_SENSOR),
                "weather": ent(CONF_WEATHER_ENTITY),
                "ev_soc": ent(CONF_EV_SOC_SENSOR),
                "ev_energy_added": ent(CONF_EV_ENERGY_ADDED_SENSOR),
                "calendars": list(self.config.get(CONF_CALENDARS) or []),
                "price_source": self.config.get(CONF_PRICE_SOURCE),
            },
            "pricing": {
                "markup": cfg(CONF_PRICE_MARKUP),
                "vat": cfg(CONF_PRICE_VAT),
                "excise_kwh": cfg(CONF_EXCISE_KWH),
                "buy_price_now": buy,
                "distribution_now": dist,
                "fixed_hourly": self.tariff.fixed_hourly_for(now),
                "total_now": (
                    round(buy + dist, 4) if buy is not None and dist is not None else None
                ),
                "confirmed": self.prices.is_confirmed(now),
            },
            "consumption_model": {
                "observed_days": self.consumption.base.observed_days,
                "base_now_kwh": self.consumption.base_value(now.weekday(), now.hour),
                "device_profiles": len(self.consumption.devices),
            },
            "battery": {
                "capacity_kwh": cfg(CONF_BATTERY_CAPACITY_KWH, 2),
                "soc": round(live_soc, 1) if live_soc else None,
                "charge_efficiency": eta_c,
                "discharge_efficiency": eta_d,
                "wear_cost": wear,
                # PLN per kWh *stored* in the pack (blended over charges).
                "reservoir_cost": reservoir_cost,
                # PLN per kWh *delivered* to the house: losses + wear on the
                # way out — this is the chart's "Cena w baterii".
                "delivered_cost": (
                    round(reservoir_cost / eta_d + wear, 4) if eta_d > 0 else None
                ),
                # What storing 1 kWh bought now would cost after charge losses
                # (before blending into the reservoir average).
                "store_cost_now": (
                    round(((buy + dist) / eta_c) + wear, 4)
                    if buy is not None and dist is not None and eta_c > 0
                    else None
                ),
                # Power-dependent efficiency curve points; when present the
                # optimizer picks charge powers by marginal efficiency and the
                # flat η above is only the fallback/average.
                "efficiency_curve_points": len(
                    self.config.get(CONF_CHARGE_EFFICIENCY_CURVE) or []
                ),
            },
            "ev": {
                "enabled": self.ev.enabled,
                "soc": self.ev.soc,
                "capacity_kwh": self.ev._capacity,
                "charger_power_kw": self.ev.charger_power_kw,
                "targets": len(self.ev._targets) + len(self.ev._trip_targets),
                "trips": len(self.calendar.trips),
            },
            "optimizer": {
                "created_at": (
                    self.data.created_at.isoformat()
                    if self.data and self.data.created_at
                    else None
                ),
                "horizon_hours": len(self.data.decisions) if self.data else 0,
                "total_cost": (
                    round(self.data.total_cost, 2) if self.data else None
                ),
                "current": (
                    {
                        "inverter_mode": current.inverter_mode,
                        "battery_charge_kwh": round(current.battery_charge_kwh, 3),
                        "battery_discharge_kwh": round(
                            current.battery_discharge_kwh, 3
                        ),
                        "grid_buy_kwh": round(current.grid_buy_kwh, 3),
                        "ev_charge_kwh": round(current.ev_charge_kwh, 3),
                        "battery_soc_end": round(current.battery_soc, 1),
                        "hour_cost": round(current.hour_cost, 4),
                        "battery_use_cost": round(current.battery_use_cost, 4),
                    }
                    if current
                    else None
                ),
            },
        }

    def get_status(self) -> dict:
        """Feature/module status for the panel: what works, what's missing."""
        from .const import (
            CONF_BUY_PRICE_SENSOR,
            CONF_CONSUMPTION_SENSOR,
            CONF_PRADCAST_API_KEY,
            CONF_PRICE_SOURCE,
            CONF_SOC_SENSOR,
            INTEGRATION_VERSION,
            PRICE_SOURCE_PRADCAST,
        )

        plan = self.data
        price_source = self.config.get(CONF_PRICE_SOURCE)
        price_ok = bool(
            self.config.get(CONF_PRADCAST_API_KEY)
            if price_source == PRICE_SOURCE_PRADCAST
            else self.config.get(CONF_BUY_PRICE_SENSOR)
        )

        modules = []
        for module in self.registry.modules:
            modules.append(
                {
                    "domain": module.domain,
                    "error": module.last_error,
                }
            )

        checks = [
            {
                "key": "battery_soc",
                "label": "🔋 Sensor SoC ESS",
                "ok": bool(self.config.get(CONF_SOC_SENSOR)),
            },
            {
                "key": "prices",
                "label": f"Źródło cen ({price_source})",
                "ok": price_ok,
            },
            {
                "key": "consumption",
                "label": "Sensor zużycia",
                "ok": bool(self.config.get(CONF_CONSUMPTION_SENSOR)),
            },
            {
                "key": "tariff",
                "label": "Taryfa dystrybucyjna",
                "ok": bool(
                    self.tariff.tariffs
                    and tariff_for_day(self.tariff.tariffs, dt_util.now().date())
                ),
            },
        ]

        ev_summary = self.ev.plan_summary()
        ev_summary["control"] = self.ev_control()
        ev_summary["planned_hours"] = (
            [
                {"start": d.start.isoformat(), "kwh": round(d.ev_charge_kwh, 3)}
                for d in plan.decisions
                if d.ev_charge_kwh > 0
            ]
            if plan
            else []
        )

        return {
            "version": INTEGRATION_VERSION,
            "last_update": self.events[0]["time"] if self.events else None,
            "horizon_hours": len(plan.decisions) if plan else 0,
            "price_archive_hours": len(self.prices.archive),
            "consumption_days": self.consumption.base.observed_days,
            "consumption_devices": list(self.consumption.devices.keys()),
            "ev_enabled": self.ev.enabled,
            "ev": ev_summary,
            "modules": modules,
            "checks": checks,
        }
