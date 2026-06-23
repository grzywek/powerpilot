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
)
from .forecast import ForecastBuilder
from .models import Plan, tariff_for_day
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

    async def async_setup_modules(self) -> None:
        await self.registry.async_setup_all()

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

        self._record_event(plan)
        return plan

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
        """7×24 learned profiles for the panel heatmaps."""
        return {
            "price": self.prices.profile.as_matrix(),
            "price_days": self.prices.profile.observed_days,
            "consumption": self.consumption.base.as_matrix(),
            "consumption_days": self.consumption.base.observed_days,
            "devices": {
                eid: acc.as_matrix() for eid, acc in self.consumption.devices.items()
            },
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

        plan = self.data
        series = await self.get_series(past_hours=48)
        return {
            "generated_at": dt_util.now().isoformat(),
            "config": _redact(dict(self.config)),
            "plan": plan.as_dict() if plan else None,
            "status": self.get_status(),
            "profiles": self.get_profiles(),
            "series": series,
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
        )

        now = dt_util.now().replace(minute=0, second=0, microsecond=0)

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
            total_price = (
                buy_price + dist_price
                if buy_price is not None and dist_price is not None
                else None
            )
            hours.append(
                {
                    "start": h.isoformat(),
                    "is_past": True,
                    "buy_price": buy_price,
                    "distribution_price_kwh": dist_price,
                    "total_price_kwh": total_price,
                    "price_confirmed": self.prices.is_confirmed(h),
                    "consumption_real": round(main_real[h], 3) if h in main_real else None,
                    "consumption_forecast": forecast_c,
                    "base_consumption_forecast": round(base_fc, 3) if base_fc is not None else None,
                    "soc": round(soc_real[h], 1) if h in soc_real else None,
                    "battery_soc_start": round(prev_soc, 1) if prev_soc is not None else None,
                    "inverter_mode": None,
                    "battery_charge_kwh": round(bat_charge_real[h], 3) if h in bat_charge_real else None,
                    "battery_discharge_kwh": round(bat_discharge_real[h], 3) if h in bat_discharge_real else None,
                    "battery_energy_cost": None,
                    "grid_buy_kwh": round(grid_import_real[h], 3) if h in grid_import_real else None,
                    "ev_charge_kwh": None,
                    "hour_cost": None,
                    "energy_cost": None,
                    "distribution_cost": None,
                    "battery_use_cost": None,
                    "devices_real": dev_real_h,
                    "devices_forecast": dev_forecast,
                }
            )
            if h in soc_real:
                prev_soc = soc_real[h]
            h += timedelta(hours=1)

        # ----- Future hours from plan -----
        plan = self.data
        # If there was no past window to seed from, start the future SoC line at
        # the live SoC the optimizer began planning from.
        if prev_soc is None:
            live_soc = self._read_soc()
            prev_soc = live_soc if live_soc else None
        if plan:
            for slot, decision in zip(plan.forecast.slots, plan.decisions):
                if window_end and slot.start >= window_end:
                    break
                if slot.start < past_end:
                    # Plan slot already covered by past window — skip duplicate.
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
                        "devices_real": {eid: None for eid in device_ids},
                        "devices_forecast": dev_forecast,
                    }
                )
                prev_soc = decision.battery_soc

        return {
            "now": now.isoformat(),
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
            "price_profile_days": self.prices.profile.observed_days,
            "consumption_days": self.consumption.base.observed_days,
            "consumption_devices": list(self.consumption.devices.keys()),
            "ev_enabled": self.ev.enabled,
            "modules": modules,
            "checks": checks,
        }
