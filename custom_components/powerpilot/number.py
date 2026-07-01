"""Number platform for PowerPilot.

Hosts the EV target-SoC entity: a writable helper the integration owns and
persists itself, instead of pointing the EV module at a car-provided sensor
(most cars don't expose one in Home Assistant, and a dashboard-editable
helper is quicker to adjust than the options flow). Calendar deadline
targets and forced windows still override it when present.
"""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EV_TARGET_SOC_DEFAULT, NUMBER_EV_TARGET_SOC
from .coordinator import PowerPilotCoordinator
from .sensor import PowerPilotEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PowerPilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EVTargetSocNumber(coordinator, entry)])


class EVTargetSocNumber(PowerPilotEntity, RestoreNumber):
    """The SoC (%) the EV should charge to when no calendar plan overrides it."""

    _attr_translation_key = NUMBER_EV_TARGET_SOC
    _attr_icon = "mdi:battery-charging-80"
    _attr_native_min_value = 20
    _attr_native_max_value = 100
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, NUMBER_EV_TARGET_SOC)
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None:
            last_data = await self.async_get_last_number_data()
            self._attr_native_value = (
                last_data.native_value if last_data is not None else None
            )
        if self._attr_native_value is None:
            self._attr_native_value = EV_TARGET_SOC_DEFAULT
        self.coordinator.ev.target_soc_entity = self

    async def async_will_remove_from_hass(self) -> None:
        if self.coordinator.ev.target_soc_entity is self:
            self.coordinator.ev.target_soc_entity = None
        await super().async_will_remove_from_hass()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
