"""PowerPilot DataUpdateCoordinator.

Runs the full pipeline on a fixed interval:

    modules.update → ForecastBuilder.build → Optimizer.optimize → Plan

and exposes the resulting :class:`Plan` to the entities.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import datetime, timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .battery import BatteryModel
from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_WEAR_COST,
    CONF_CHARGE_CURVE,
    CONF_CHARGE_EFFICIENCY,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_GRID_DISCONNECT_SOC,
    CONF_GRID_VOLTAGE,
    CONF_INVERTER_MAX_CHARGE_KW,
    CONF_INVERTER_MAX_DISCHARGE_KW,
    CONF_MAIN_FUSE_A,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PHASES,
    CONF_SOC_SENSOR,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
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
from .models import Plan, tariff_for_day
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


class PowerPilotCoordinator(DataUpdateCoordinator[Plan]):
    """Coordinates modules, forecast and optimizer."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.entry = entry
        self.config: dict = {**DEFAULTS, **entry.data, **entry.options}
        self._battery_energy_cost = 0.0
        self.events: deque = deque(maxlen=50)

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(minutes=DEFAULT_UPDATE_INTERVAL_MINUTES),
        )

        self.registry = ModuleRegistry()
        self.consumption = ConsumptionModule(hass, self)
        self.prices = PriceModule(hass, self)
        self.tariff = TariffModule(hass, self)
        self.loads = LoadsModule(hass, self)
        self.weather = WeatherModule(hass, self)
        self.climate = ClimateModule(hass, self)
        self.ev = EVModule(hass, self)
        self.calendar = CalendarModule(hass, self)

        # Order matters: prices/weather first, then derived loads, then EV.
        for module in (
            self.prices,
            self.tariff,
            self.consumption,
            self.weather,
            self.climate,
            self.loads,
            self.ev,
            self.calendar,
        ):
            self.registry.register(module)

        self.forecast_builder = ForecastBuilder(self.registry)

        # Optimizer snapshots ("Symulacje" tab): one vintage per clock hour.
        self.snapshots = SnapshotStore()
        self._snapshot_store: Store | None = None
        self._last_snapshot_hour: datetime | None = None

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

    async def _async_save_snapshots(self) -> None:
        if self._snapshot_store is None:
            return
        self._snapshot_store.async_delay_save(self.snapshots.to_dict, 30.0)

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

    def _build_battery(self) -> BatteryModel:
        soc = self._read_soc()
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

    def _read_soc(self) -> float:
        entity_id = self.config.get(CONF_SOC_SENSOR)
        if entity_id:
            state = self.hass.states.get(entity_id)
            if state is not None:
                try:
                    return float(state.state)
                except (TypeError, ValueError):
                    pass
        return 50.0

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
                grid_disconnect_soc=float(self.config[CONF_GRID_DISCONNECT_SOC]),
                charge_curve=curve,
                connection_power_kw=connection_power_kw,
            )
        )

    async def _async_update_data(self) -> Plan:
        await self.registry.async_update_all()

        forecast = await self.hass.async_add_executor_job(self.forecast_builder.build)
        ev_request = self.ev.get_request(forecast)
        reminders = self.registry.collect_reminders()

        battery = self._build_battery()
        optimizer = self._build_optimizer()
        plan = optimizer.optimize(forecast, battery, ev_request, reminders)

        if plan.current is not None:
            self._battery_energy_cost = plan.current.battery_energy_cost

        await self._maybe_record_snapshot(forecast, plan)
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

    async def _maybe_record_snapshot(self, forecast, plan) -> None:
        """Persist one columnar snapshot per clock hour (a 'vintage')."""
        now = dt_util.now()
        hour = now.replace(minute=0, second=0, microsecond=0)
        if self._last_snapshot_hour is not None and hour <= self._last_snapshot_hour:
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
            "grid": [_r(d.grid_buy_kwh, 3) for d in decisions],
            # Planned battery flows + EV, so the chart's forecast tooltip column
            # can be reconstructed for past hours. Captured from now on.
            "charge": [_r(d.battery_charge_kwh, 3) for d in decisions],
            "dischg": [_r(d.battery_discharge_kwh, 3) for d in decisions],
            "ev": [_r(d.ev_charge_kwh, 3) for d in decisions],
            "cost": [_r(d.hour_cost, 4) for d in decisions],
            # Realized battery energy cost (no sensor exists for it) so the chart
            # can show "Cena w baterii" for past hours by reading index 0 of the
            # vintage recorded at each hour. Captured from now on; older vintages
            # predate this field and stay blank.
            "bcost": [_r(d.battery_energy_cost, 4) for d in decisions],
        }
        self.snapshots.add(record)
        self.snapshots.prune()
        self._last_snapshot_hour = hour
        await self._async_save_snapshots()

    def get_snapshots(self) -> dict:
        """Available vintages for the picker."""
        return {"runs": self.snapshots.runs()}

    def get_snapshot(self, run_at: str | None) -> dict:
        """Decode one vintage into per-hour rows the chart can render."""
        rec = self.snapshots.get(run_at) if run_at else None
        if rec is None:
            runs = self.snapshots.runs()
            rec = self.snapshots.get(runs[0]["run_at"]) if runs else None
        if rec is None:
            return {"run_at": None, "hours": []}

        start = dt_util.parse_datetime(rec["start"])
        n = rec.get("n", 0)
        horizon = rec.get("horizon_hours", 0)

        def _at(key, idx, default=None):
            seq = rec.get(key) or []
            return seq[idx] if idx < len(seq) else default

        hours: list[dict] = []
        for i in range(n):
            hour_start = (start + timedelta(hours=i)) if start else None
            buy = _at("buy", i)
            dist = _at("dist", i)
            # Per-kWh price excludes the fixed monthly charge (billed separately).
            total = (
                buy + dist
                if (buy is not None and dist is not None)
                else None
            )
            hours.append(
                {
                    "start": hour_start.isoformat() if hour_start else None,
                    "buy_price": buy,
                    "distribution_price_kwh": dist,
                    "total_price_kwh": total,
                    "price_type": PTYPE_CODE_INV.get(_at("ptype", i)),
                    "consumption_forecast": _at("cons_fc", i),
                    "base_consumption_forecast": _at("base_fc", i),
                    "inverter_mode": MODE_CODE_INV.get(_at("mode", i)) if i < horizon else None,
                    "battery_soc": _at("soc", i) if i < horizon else None,
                    "soc": _at("soc", i) if i < horizon else None,
                    "grid_buy_kwh": _at("grid", i) if i < horizon else None,
                    "hour_cost": _at("cost", i) if i < horizon else None,
                }
            )
        return {
            "run_at": rec["run_at"],
            "start": rec.get("start"),
            "total_cost": rec.get("total_cost"),
            "hours": hours,
        }

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
        bias_sum = [0.0] * 24
        bias_cnt = [0] * 24
        h = window_start
        while h < now:
            pred = _pred_for(h)
            act_c = actual_cons.get(h)
            act_p = (self.prices.archive.get(h) or {}).get("energy")
            pred_c = pred["cons"] if pred else None
            err = (
                round(pred_c - act_c, 3)
                if (pred_c is not None and act_c is not None)
                else None
            )
            if err is not None:
                errors.append(err)
                bias_sum[h.hour] += err
                bias_cnt[h.hour] += 1
            hours.append(
                {
                    "start": h.isoformat(),
                    "predicted_cons": round(pred_c, 3) if pred_c is not None else None,
                    "actual_cons": round(act_c, 3) if act_c is not None else None,
                    "error": err,
                    "predicted_price": pred["buy"] if pred else None,
                    "actual_price": round(act_p, 4) if act_p is not None else None,
                }
            )
            h += timedelta(hours=1)

        bias_by_hour = [
            round(bias_sum[i] / bias_cnt[i], 3) if bias_cnt[i] else None
            for i in range(24)
        ]
        mae = round(sum(abs(e) for e in errors) / len(errors), 3) if errors else None
        bias = round(sum(errors) / len(errors), 3) if errors else None
        return {
            "lead_hours": lead_hours,
            "days": days,
            "samples": len(errors),
            "mae": mae,
            "bias": bias,
            "bias_by_hour": bias_by_hour,
            "hours": hours,
        }

    # ------------------------------------------------------------------
    # Frontend support: event log + feature status
    # ------------------------------------------------------------------
    def _record_event(self, plan: Plan) -> None:
        current = plan.current
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
        return {
            "consumption": self.consumption.base.as_matrix(),
            "consumption_days": self.consumption.base.observed_days,
            "devices": {
                eid: acc.as_matrix() for eid, acc in self.consumption.devices.items()
            },
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
                "soc", "SoC baterii", CONF_SOC_SENSOR, required=True, raw=True
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
                "Sensor ładowania baterii",
                CONF_BATTERY_CHARGE_SENSOR,
                required=False,
            ),
            await _sensor_item(
                "battery_discharge",
                "Sensor rozładowania baterii",
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
        optional_items.append(
            await _sensor_item(
                "weather", "Encja pogody", CONF_WEATHER_ENTITY, required=False, raw=True
            )
        )
        if self.config.get(CONF_EV_ENABLED):
            optional_items.append(
                await _sensor_item(
                    "ev_soc", "SoC samochodu (EV)", CONF_EV_SOC_SENSOR, required=False, raw=True
                )
            )

        groups = [
            {"title": "Wymagane do optymalizacji", "items": required_items},
            {"title": "Bateria i sieć (dane rzeczywiste)", "items": battery_items},
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

    async def get_debug(self) -> dict:
        """Full diagnostic snapshot for troubleshooting optimizer decisions.

        Bundles the (secret-redacted) config, the current plan with per-hour
        decision traces, feature status, learned profiles and the recent + full
        forecast series — everything needed to reason about why the optimizer
        made a given decision, in one copy/paste-able JSON blob.
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
        series = await self.get_series(past_hours=48)

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

        return {
            "generated_at": dt_util.now().isoformat(),
            "config": _redact(dict(self.config)),
            "plan": plan.as_dict() if plan else None,
            "status": self.get_status(),
            "profiles": self.get_profiles(),
            "series": series,
            "sensor_reads": sensor_reads,
            "log": self.get_log(),
        }

    async def get_forecasts(self, date_str: str | None) -> dict:
        """Horizon-indexed price forecasts (D+1..D+3) for a target date."""
        from datetime import date as _date, timedelta as _td

        if date_str:
            try:
                target = _date.fromisoformat(date_str)
            except ValueError:
                target = dt_util.now().date() + _td(days=1)
        else:
            target = dt_util.now().date() + _td(days=1)

        horizons = await self.prices.async_fetch_forecasts(target)
        return {"date": target.isoformat(), "horizons": horizons}

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
            if entry is not None:
                energy = entry["energy"]
                price_type = entry["type"]
                source = entry["source"]
                fetched_at = entry["fetched_at"]
                p10 = entry.get("p10")
                p90 = entry.get("p90")
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

            row = {
                "start": hour.isoformat(),
                "type": price_type,
                "source": source,
                "fetched_at": fetched_at,
                "p10": _r(p10),
                "p90": _r(p90),
                "estimate_breakdown": breakdown,
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

    async def _recent_soc(self, start: datetime, end: datetime) -> dict:
        """Hourly battery SoC (%) from the SoC sensor's statistics for a window."""
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import (
            statistics_during_period,
        )

        from .const import CONF_SOC_SENSOR

        entity_id = self.config.get(CONF_SOC_SENSOR)
        if not entity_id:
            return {}
        rows = await get_instance(self.hass).async_add_executor_job(
            statistics_during_period, self.hass, start, end, {entity_id}, "hour", None, {"mean"}
        )
        out: dict = {}
        for row in rows.get(entity_id, []):
            value = row.get("mean")
            if value is None:
                continue
            ts = row["start"]
            if isinstance(ts, (int, float)):
                ts = dt_util.utc_from_timestamp(ts)
            hour = dt_util.as_local(ts).replace(minute=0, second=0, microsecond=0)
            out[hour] = float(value)
        return out

    async def get_series(
        self,
        past_hours: int = 24,
        start: str | None = None,
        end: str | None = None,
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
        """
        from .const import (
            CONF_BATTERY_CHARGE_SENSOR,
            CONF_BATTERY_DISCHARGE_SENSOR,
            CONF_CONSUMPTION_SENSOR,
            CONF_DEVICE_SENSORS,
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
            }

        hours: list[dict] = []

        # SoC *entering* each hour (start-of-hour state). `decision.battery_soc`
        # and the recorder mean are END-of-hour values, so to draw the SoC line
        # rising/falling across the bar that caused the change, the chart needs
        # the value the battery enters each hour with. Track it as we walk
        # forward; seed from the hour just before the window (the recorder reads
        # one extra hour back exactly for this).
        prev_soc = soc_real.get(past_start - timedelta(hours=1))

        # ----- Past hours -----
        h = past_start
        while h < past_end:
            wd, hr = h.weekday(), h.hour
            base_fc = self.consumption.base_value(wd, hr) if learned else None
            dev_forecast = {
                eid: round(
                    self.consumption.devices[eid].value(wd, hr) or 0.0, 3
                )
                if eid in self.consumption.devices
                else None
                for eid in device_ids
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
            dev_real_sum = sum(v for v in dev_real_h.values() if v is not None)
            base_real = (
                max(0.0, m_real - dev_real_sum) if m_real is not None else None
            )
            realized_side = _side(
                grid=g_real,
                discharge=d_real,
                base=base_real,
                charge=c_real,
                devices=dev_real_h,
                soc_start=prev_soc,
                soc_end=soc_real.get(h),
            )
            # Forecast side reconstructed from the vintage recorded at this hour
            # (battery flows captured only from now on → blank for old vintages).
            sn = self.snapshots
            forecast_side = _side(
                grid=sn.value_at(h, "grid"),
                discharge=sn.value_at(h, "dischg"),
                base=base_fc,
                ev=sn.value_at(h, "ev"),
                charge=sn.value_at(h, "charge"),
                devices=dev_forecast,
                soc_start=sn.value_at(h - timedelta(hours=1), "soc"),
                soc_end=sn.value_at(h, "soc"),
            )
            hours.append(
                {
                    "start": h.isoformat(),
                    "is_past": True,
                    "realized": realized_side,
                    "forecast": forecast_side,
                    "buy_price": buy_price,
                    "distribution_price_kwh": dist_price,
                    "total_price_kwh": total_price,
                    "price_confirmed": self.prices.is_confirmed(h),
                    "consumption_real": round(main_real[h], 3) if h in main_real else None,
                    "consumption_forecast": forecast_c,
                    "base_consumption_forecast": round(base_fc, 3) if base_fc is not None else None,
                    "soc": round(soc_real[h], 1) if h in soc_real else None,
                    "battery_soc_start": round(prev_soc, 1) if prev_soc is not None else None,
                    "inverter_mode": _real_mode(
                        bat_charge_real.get(h), bat_discharge_real.get(h)
                    ),
                    "battery_charge_kwh": round(bat_charge_real[h], 3) if h in bat_charge_real else None,
                    "battery_discharge_kwh": round(bat_discharge_real[h], 3) if h in bat_discharge_real else None,
                    # Realized battery energy cost from the vintage recorded at h
                    # (no live sensor exists for it). Blank for hours predating
                    # snapshot capture of this field.
                    "battery_energy_cost": self.snapshots.value_at(h, "bcost"),
                    "grid_buy_kwh": round(grid_import_real[h], 3) if h in grid_import_real else None,
                    "ev_charge_kwh": None,
                    "hour_cost": None,
                    "energy_cost": None,
                    "distribution_cost": None,
                    "battery_use_cost": None,
                    "fixed_cost": self.tariff.fixed_hourly_for(h),
                    "devices_real": dev_real_h,
                    "devices_forecast": dev_forecast,
                }
            )
            if h in soc_real:
                prev_soc = soc_real[h]
            h += timedelta(hours=1)

        # ----- Current (in-progress) hour: realized-so-far + whole-hour forecast -----
        # The current clock hour is part realized (elapsed, from 5-min stats) and
        # part forecast. We draw the realized-so-far bar (so SoC and bars agree)
        # and the tooltip shows both sides: realized up to ``real_now`` and the
        # plan's forecast for the whole hour.
        emitted_current = False
        if real_now > now and (window_end is None or window_end > now):

            async def _partial(conf_key: str) -> float | None:
                sensor = self.config.get(conf_key)
                if not sensor:
                    return None
                return await self.consumption.async_partial_kwh(sensor, now, real_now)

            cur_charge = await _partial(CONF_BATTERY_CHARGE_SENSOR)
            cur_discharge = await _partial(CONF_BATTERY_DISCHARGE_SENSOR)
            cur_grid = await _partial(CONF_GRID_IMPORT_SENSOR)
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
                eid: round(self.consumption.devices[eid].value(wd, hr) or 0.0, 3)
                if eid in self.consumption.devices
                else None
                for eid in device_ids
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
            realized_side = _side(
                grid=cur_grid,
                discharge=cur_discharge,
                base=cur_base,
                charge=cur_charge,
                devices=cur_devices,
                soc_start=prev_soc,
                soc_end=live_soc,
            )
            # Whole-hour forecast from the plan's current-hour decision.
            cur_dec = cur_slot = None
            if self.data and self.data.decisions:
                for _sl, _dc in zip(self.data.forecast.slots, self.data.decisions):
                    if _sl.start == now:
                        cur_slot, cur_dec = _sl, _dc
                        break
            forecast_side = None
            if cur_dec is not None:
                forecast_side = _side(
                    grid=cur_dec.grid_buy_kwh,
                    discharge=cur_dec.battery_discharge_kwh,
                    base=cur_slot.base_consumption_kwh,
                    ev=cur_dec.ev_charge_kwh,
                    charge=cur_dec.battery_charge_kwh,
                    devices=dev_forecast,
                    soc_start=prev_soc,
                    soc_end=cur_dec.battery_soc,
                )
            hours.append(
                {
                    "start": now.isoformat(),
                    "is_past": True,
                    "partial": True,
                    "partial_until": real_now.isoformat(),
                    "realized": realized_side,
                    "forecast": forecast_side,
                    "buy_price": buy_price,
                    "distribution_price_kwh": dist_price,
                    "total_price_kwh": total_price,
                    "price_confirmed": self.prices.is_confirmed(now),
                    "consumption_real": round(cur_main, 3) if cur_main is not None else None,
                    "consumption_forecast": None,
                    "base_consumption_forecast": None,
                    "soc": round(live_soc, 1) if live_soc else None,
                    "battery_soc_start": round(prev_soc, 1) if prev_soc is not None else None,
                    "inverter_mode": _real_mode(cur_charge, cur_discharge),
                    "battery_charge_kwh": round(cur_charge, 3) if cur_charge is not None else None,
                    "battery_discharge_kwh": round(cur_discharge, 3) if cur_discharge is not None else None,
                    "battery_energy_cost": round(self._battery_energy_cost, 4),
                    "grid_buy_kwh": round(cur_grid, 3) if cur_grid is not None else None,
                    "ev_charge_kwh": None,
                    "hour_cost": None,
                    "energy_cost": None,
                    "distribution_cost": None,
                    "battery_use_cost": None,
                    "fixed_cost": self.tariff.fixed_hourly_for(now),
                    "devices_real": {
                        eid: round(v, 3) if v is not None else None
                        for eid, v in cur_devices.items()
                    },
                    "devices_forecast": dev_forecast,
                }
            )
            emitted_current = True
            if live_soc:
                prev_soc = live_soc

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
        if plan:
            for slot, decision in zip(plan.forecast.slots, plan.decisions):
                if window_end and slot.start >= window_end:
                    break
                if slot.start < forecast_cutoff:
                    # Plan slot already covered by past / current-partial → skip.
                    continue
                wd, hr = slot.start.weekday(), slot.start.hour
                dev_forecast = {
                    eid: round(
                        self.consumption.devices[eid].value(wd, hr) or 0.0, 3
                    )
                    if eid in self.consumption.devices
                    else None
                    for eid in device_ids
                }
                hours.append(
                    {
                        "start": slot.start.isoformat(),
                        "is_past": False,
                        "realized": None,
                        "forecast": _side(
                            grid=decision.grid_buy_kwh,
                            discharge=decision.battery_discharge_kwh,
                            base=slot.base_consumption_kwh,
                            ev=decision.ev_charge_kwh,
                            charge=decision.battery_charge_kwh,
                            devices=dev_forecast,
                            soc_start=prev_soc,
                            soc_end=decision.battery_soc,
                        ),
                        "buy_price": slot.buy_price,
                        "distribution_price_kwh": slot.distribution_price_kwh,
                        "total_price_kwh": slot.total_price_kwh,
                        "price_confirmed": slot.price_confirmed,
                        "consumption_real": None,
                        "consumption_forecast": round(slot.total_consumption_kwh, 3),
                        "base_consumption_forecast": round(slot.base_consumption_kwh, 3),
                        "soc": round(decision.battery_soc, 1),
                        "battery_soc_start": round(prev_soc, 1) if prev_soc is not None else None,
                        "inverter_mode": decision.inverter_mode,
                        "battery_charge_kwh": round(decision.battery_charge_kwh, 3),
                        "battery_discharge_kwh": round(decision.battery_discharge_kwh, 3),
                        "battery_energy_cost": round(decision.battery_energy_cost, 4),
                        "grid_buy_kwh": round(decision.grid_buy_kwh, 3),
                        "ev_charge_kwh": round(decision.ev_charge_kwh, 3),
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

        return {
            # Exact present instant — the panel draws the "teraz" line here.
            "now": real_now.isoformat(),
            "past_hours": past_hours,
            "start": past_start.isoformat(),
            "end": (window_end or (plan.forecast.slots[-1].start + timedelta(hours=1) if plan and plan.forecast.slots else past_end)).isoformat() if (window_end or plan) else past_end.isoformat(),
            "device_ids": device_ids,
            "hours": hours,
        }

    def get_status(self) -> dict:
        """Feature/module status for the panel: what works, what's missing."""
        from .const import (
            CONF_BUY_PRICE_SENSOR,
            CONF_CONSUMPTION_SENSOR,
            CONF_PRADCAST_API_KEY,
            CONF_PRICE_SOURCE,
            CONF_SOC_SENSOR,
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
                "label": "Sensor SoC baterii",
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

        return {
            "last_update": self.events[0]["time"] if self.events else None,
            "horizon_hours": len(plan.decisions) if plan else 0,
            "price_archive_hours": len(self.prices.archive),
            "consumption_days": self.consumption.base.observed_days,
            "consumption_devices": list(self.consumption.devices.keys()),
            "ev_enabled": self.ev.enabled,
            "modules": modules,
            "checks": checks,
        }
