"""PowerPilot DataUpdateCoordinator.

Runs the full pipeline on a fixed interval:

    modules.update → ForecastBuilder.build → Optimizer.optimize → Plan

and exposes the resulting :class:`Plan` to the entities.
"""

from __future__ import annotations

import logging
from collections import deque
from datetime import timedelta

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
    CONF_INVERTER_MAX_CHARGE_KW,
    CONF_INVERTER_MAX_DISCHARGE_KW,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_SOC_SENSOR,
    DEFAULT_UPDATE_INTERVAL_MINUTES,
    DEFAULTS,
    DOMAIN,
)
from .forecast import ForecastBuilder
from .models import Plan
from .modules import (
    CalendarModule,
    ClimateModule,
    ConsumptionModule,
    EVModule,
    LoadsModule,
    ModuleRegistry,
    PriceModule,
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
        self.loads = LoadsModule(hass, self)
        self.weather = WeatherModule(hass, self)
        self.climate = ClimateModule(hass, self)
        self.ev = EVModule(hass, self)
        self.calendar = CalendarModule(hass, self)

        # Order matters: prices/weather first, then derived loads, then EV.
        for module in (
            self.prices,
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
        return Optimizer(
            OptimizerConfig(
                inverter_max_charge_kw=float(self.config[CONF_INVERTER_MAX_CHARGE_KW]),
                inverter_max_discharge_kw=float(self.config[CONF_INVERTER_MAX_DISCHARGE_KW]),
                grid_disconnect_soc=float(self.config[CONF_GRID_DISCONNECT_SOC]),
                charge_curve=curve,
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
                "horizon_hours": len(plan.decisions),
                "action": current.inverter_mode if current else None,
                "ev_charge": current.ev_charge if current else None,
                "battery_soc": round(current.battery_soc, 1) if current else None,
                "errors": errors,
            }
        )

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
