"""Binary sensor platform for PowerPilot."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import BINARY_EV_CHARGE, BINARY_GRID_CONNECTED, DOMAIN
from .coordinator import PowerPilotCoordinator
from .sensor import PowerPilotEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PowerPilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            GridConnectedBinarySensor(coordinator, entry),
            EVChargeBinarySensor(coordinator, entry),
        ]
    )


class GridConnectedBinarySensor(PowerPilotEntity, BinarySensorEntity):
    _attr_translation_key = BINARY_GRID_CONNECTED
    _attr_icon = "mdi:transmission-tower-import"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, BINARY_GRID_CONNECTED)

    @property
    def is_on(self) -> bool | None:
        if self.plan and self.plan.current:
            return self.plan.current.grid_connected
        return None


class EVChargeBinarySensor(PowerPilotEntity, BinarySensorEntity):
    _attr_translation_key = BINARY_EV_CHARGE
    _attr_icon = "mdi:ev-station"

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, BINARY_EV_CHARGE)

    @property
    def is_on(self) -> bool | None:
        if self.plan and self.plan.current:
            return self.plan.current.ev_charge
        return None
