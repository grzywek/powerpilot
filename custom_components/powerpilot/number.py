"""Number platform for PowerPilot.

Hosts the EV minimum-SoC entity: a writable helper the integration owns and
persists itself (a dashboard-editable helper is quicker to adjust than the
options flow). It is the safety reserve trip and routine planning charge on top
of — the car must always make the round trip and come home above this floor.
There is deliberately no "target SoC": the calendar says when the car needs
how much.
"""

from __future__ import annotations

from homeassistant.components.number import NumberMode, RestoreNumber
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import DOMAIN, EV_MIN_SOC_DEFAULT, NUMBER_EV_MIN_SOC
from .coordinator import PowerPilotCoordinator
from .sensor import PowerPilotEntity


async def async_setup_entry(
    hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback
) -> None:
    coordinator: PowerPilotCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([EVMinSocNumber(coordinator, entry)])


class EVMinSocNumber(PowerPilotEntity, RestoreNumber):
    """Safety reserve (%): the plan never lets the EV dip below this SoC."""

    _attr_translation_key = NUMBER_EV_MIN_SOC
    _attr_icon = "mdi:battery-alert"
    _attr_native_min_value = 0
    _attr_native_max_value = 60
    _attr_native_step = 5
    _attr_native_unit_of_measurement = "%"
    _attr_mode = NumberMode.SLIDER

    def __init__(self, coordinator, entry) -> None:
        super().__init__(coordinator, entry, NUMBER_EV_MIN_SOC)
        self._attr_native_value = None

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        if self._attr_native_value is None:
            last_data = await self.async_get_last_number_data()
            self._attr_native_value = (
                last_data.native_value if last_data is not None else None
            )
        if self._attr_native_value is None:
            self._attr_native_value = EV_MIN_SOC_DEFAULT
        self.coordinator.ev.min_soc_entity = self
        # The entry's first refresh runs BEFORE the number platform is set up,
        # so that plan (and its trip targets) used the built-in 20 % default
        # instead of the restored reserve — re-plan with the real value.
        await self.coordinator.async_request_refresh()

    async def async_will_remove_from_hass(self) -> None:
        if self.coordinator.ev.min_soc_entity is self:
            self.coordinator.ev.min_soc_entity = None
        await super().async_will_remove_from_hass()

    async def async_set_native_value(self, value: float) -> None:
        self._attr_native_value = value
        self.async_write_ha_state()
        # A changed reserve must reshape the plan (trip targets are built from
        # it) immediately, not at the next hourly boundary.
        await self.coordinator.async_request_refresh()
