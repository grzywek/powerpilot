"""Sensor platform for PowerPilot."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityCategory
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    SENSOR_BATTERY_ENERGY_COST,
    SENSOR_CHARGE_POWER,
    SENSOR_ESS_CHARGE_START,
    SENSOR_EV_CHARGE_START,
    SENSOR_EV_SOC_LIMIT,
    SENSOR_INVERTER_MODE,
    SENSOR_NEXT_ACTION,
    SENSOR_PLAN,
    InverterMode,
)
from .coordinator import PowerPilotCoordinator
from .models import Plan


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PowerPilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            InverterModeSensor(coordinator, entry),
            ChargePowerSensor(coordinator, entry),
            BatteryEnergyCostSensor(coordinator, entry),
            PlanSensor(coordinator, entry),
            NextActionSensor(coordinator, entry),
            EssChargeStartSensor(coordinator, entry),
            EVChargeStartSensor(coordinator, entry),
            EVSocLimitSensor(coordinator, entry),
        ]
    )


class PowerPilotEntity(CoordinatorEntity[PowerPilotCoordinator]):
    """Shared base wiring the device + unique id."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: PowerPilotCoordinator, entry: ConfigEntry, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name="PowerPilot",
            manufacturer="PowerPilot",
            model="Energy Optimizer",
        )

    @property
    def plan(self) -> Plan | None:
        return self.coordinator.data

    @property
    def current_decision(self):
        return self.coordinator.current_decision()


class InverterModeSensor(PowerPilotEntity, SensorEntity):
    _attr_translation_key = SENSOR_INVERTER_MODE
    _attr_icon = "mdi:transmission-tower"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, SENSOR_INVERTER_MODE)

    @property
    def native_value(self) -> str | None:
        current = self.current_decision
        return current.inverter_mode if current else None


class ChargePowerSensor(PowerPilotEntity, SensorEntity):
    """Grid-side charge power planned for the current hour (kW).

    This is the "force charge X kW" setpoint to push to the inverter: the grid
    draw, equal to the hour's stored kWh divided by charge efficiency (slots are
    1 h so kWh == average kW). 0 when the current hour is not charging.
    """

    _attr_translation_key = SENSOR_CHARGE_POWER
    _attr_native_unit_of_measurement = "kW"
    _attr_device_class = SensorDeviceClass.POWER
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:speedometer"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, SENSOR_CHARGE_POWER)

    @property
    def native_value(self) -> float | None:
        current = self.current_decision
        if current:
            return round(current.charge_power_kw, 3)
        return None


class BatteryEnergyCostSensor(PowerPilotEntity, SensorEntity):
    _attr_translation_key = SENSOR_BATTERY_ENERGY_COST
    _attr_native_unit_of_measurement = "PLN/kWh"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:cash-multiple"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, SENSOR_BATTERY_ENERGY_COST)

    @property
    def native_value(self) -> float | None:
        current = self.current_decision
        if current:
            return round(current.battery_energy_cost, 4)
        return None


class PlanSensor(PowerPilotEntity, SensorEntity):
    """Carries the full hourly plan as attributes for the dashboard charts."""

    _attr_translation_key = SENSOR_PLAN
    _attr_icon = "mdi:chart-timeline-variant"
    _attr_native_unit_of_measurement = "PLN"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, SENSOR_PLAN)

    @property
    def native_value(self) -> float | None:
        return round(self.plan.total_cost, 2) if self.plan else None

    @property
    def extra_state_attributes(self) -> dict:
        if not self.plan:
            return {}
        consumption = self.coordinator.consumption
        return {
            "created_at": self.plan.created_at.isoformat() if self.plan.created_at else None,
            "horizon_hours": len(self.plan.decisions),
            "price_archive_hours": len(self.coordinator.prices.archive),
            "consumption_observed_days": consumption.base.observed_days,
            "consumption_base_profile": consumption.base.as_matrix(),
            "consumption_devices": list(consumption.devices.keys()),
            "hours": [d.as_dict() for d in self.plan.decisions],
            "forecast": [
                {
                    "start": s.start.isoformat(),
                    "buy_price": s.buy_price,
                    "price_confirmed": s.price_confirmed,
                    "consumption_kwh": round(s.total_consumption_kwh, 3),
                    "temperature": s.temperature,
                }
                for s in self.plan.forecast.slots
            ],
        }


class NextActionSensor(PowerPilotEntity, SensorEntity):
    """Human-readable summary of the imminent action."""

    _attr_translation_key = SENSOR_NEXT_ACTION
    _attr_icon = "mdi:lightbulb-on"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, SENSOR_NEXT_ACTION)

    @property
    def native_value(self) -> str | None:
        current = self.current_decision
        if not current:
            return None
        action = current.inverter_mode
        if current.ev_charge:
            action += " + EV"
        return action

    @property
    def extra_state_attributes(self) -> dict:
        current = self.current_decision
        if not current:
            return {}
        return {"reminders": current.reminders}


class EssChargeStartSensor(PowerPilotEntity, SensorEntity):
    """When the ongoing-or-next planned charge/passthrough block starts.

    The moment the ESS needs the grid: exposed as a timestamp so a user
    automation can connect the grid ahead of it (and keep it disconnected
    through pure battery-discharge stretches). While the current hour already
    charges or passes through, the value is that hour's start — a value in the
    past means "the grid should be connected right now".
    """

    _attr_translation_key = SENSOR_ESS_CHARGE_START
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, SENSOR_ESS_CHARGE_START)

    @property
    def native_value(self):
        plan = self.plan
        if not plan:
            return None
        now = dt_util.now()
        for decision in plan.decisions:
            if decision.start + timedelta(hours=1) <= now:
                continue  # hour already over
            if decision.inverter_mode in (
                InverterMode.CHARGE,
                InverterMode.PASSTHROUGH,
            ):
                return decision.start
        return None


class EVChargeStartSensor(PowerPilotEntity, SensorEntity):
    """When the next planned EV charging hour starts (None if nothing planned).

    Exposed as a timestamp so an automation (or the UI) gets a live "in X"
    countdown without the value going stale between re-plans.
    """

    _attr_translation_key = SENSOR_EV_CHARGE_START
    _attr_device_class = SensorDeviceClass.TIMESTAMP
    _attr_icon = "mdi:clock-start"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, SENSOR_EV_CHARGE_START)

    @property
    def native_value(self):
        start = self.coordinator.ev_control().get("charge_start")
        return dt_util.parse_datetime(start) if start else None


class EVSocLimitSensor(PowerPilotEntity, SensorEntity):
    """Target SoC (%) the car should charge to right now."""

    _attr_translation_key = SENSOR_EV_SOC_LIMIT
    _attr_native_unit_of_measurement = "%"
    _attr_device_class = SensorDeviceClass.BATTERY
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:battery-charging-90"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, SENSOR_EV_SOC_LIMIT)

    @property
    def native_value(self) -> float | None:
        return self.coordinator.ev_control().get("soc_limit")
