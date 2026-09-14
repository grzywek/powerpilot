"""Binary sensor platform for PowerPilot."""

from __future__ import annotations

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    BINARY_EV_CHARGE,
    BINARY_EV_CONNECT_CHARGER,
    BINARY_PLAN_NOT_EXECUTED,
    BINARY_TARCZA_PPE1_CAP_BINDS,
    DOMAIN,
)
from .coordinator import PowerPilotCoordinator
from .sensor import PowerPilotEntity, TarczaEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PowerPilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            EVChargeBinarySensor(coordinator, entry),
            EVConnectChargerBinarySensor(coordinator, entry),
            TarczaCapBindsBinarySensor(coordinator, entry),
            PlanNotExecutedBinarySensor(coordinator, entry),
        ]
    )


class PlanNotExecutedBinarySensor(PowerPilotEntity, BinarySensorEntity):
    """ON when the inverter demonstrably does not follow the plan.

    The plan needs the grid (charge / passthrough) but the pack drains, or a
    planned charge is not reaching it — the automation that executes the plan
    has failed. ``unknown`` until a full window was measured, and permanently
    without a battery discharge counter configured.
    """

    _attr_translation_key = BINARY_PLAN_NOT_EXECUTED
    _attr_device_class = BinarySensorDeviceClass.PROBLEM
    _attr_icon = "mdi:alert-octagon"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, BINARY_PLAN_NOT_EXECUTED)

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.execution_alarm.get("on")

    @property
    def extra_state_attributes(self) -> dict:
        alarm = self.coordinator.execution_alarm
        since = alarm.get("since")
        return {
            "reason": alarm.get("reason"),
            "planned_mode": alarm.get("mode"),
            "since": since.isoformat() if since else None,
        }


class EVChargeBinarySensor(PowerPilotEntity, BinarySensorEntity):
    _attr_translation_key = BINARY_EV_CHARGE
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, BINARY_EV_CHARGE)

    @property
    def is_on(self) -> bool | None:
        current = self.current_decision
        if current:
            return current.ev_charge
        return None


class EVConnectChargerBinarySensor(PowerPilotEntity, BinarySensorEntity):
    """ON when EV charging is planned within the next 24 h.

    The cue for an automation to make sure the car is plugged in ahead of time.
    """

    _attr_translation_key = BINARY_EV_CONNECT_CHARGER
    _attr_device_class = BinarySensorDeviceClass.PLUG
    _attr_icon = "mdi:power-plug"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, BINARY_EV_CONNECT_CHARGER)

    @property
    def is_on(self) -> bool | None:
        return self.coordinator.ev_control().get("connect_charger")


class TarczaCapBindsBinarySensor(TarczaEntity, BinarySensorEntity):
    """ON when the plan expects PPE 1's monthly average to hit the price cap.

    While it is ON every further kWh this month costs the limit whatever the
    hour's price, so the battery is planned on distribution differences only.
    ``unknown`` while the cap cannot be modelled (see the attributes).
    """

    _attr_translation_key = BINARY_TARCZA_PPE1_CAP_BINDS
    _attr_icon = "mdi:shield-check"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, BINARY_TARCZA_PPE1_CAP_BINDS)

    @property
    def is_on(self) -> bool | None:
        outcome = self.outcome
        if outcome is None:
            return None
        return outcome.cap_binds

    @property
    def extra_state_attributes(self) -> dict:
        return self._forecast_attributes()
