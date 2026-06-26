"""The PowerPilot integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import CONF_TARIFFS, DOMAIN, PLATFORMS
from .coordinator import PowerPilotCoordinator
from .panel import async_register_panel, async_unregister_panel

_LOGGER = logging.getLogger(__name__)

_WORKDAY_DOMAIN = "workday"
_CHECK_DATE_SERVICE = "check_date"


def _uses_day_sensor(entry: ConfigEntry) -> bool:
    """Whether any configured tariff period keys off a workday day-sensor."""
    tariffs = {**entry.data, **entry.options}.get(CONF_TARIFFS) or []
    return any(
        period.get("day_sensor")
        for tariff in tariffs
        for period in (tariff.get("periods") or [])
    )


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up PowerPilot from a config entry."""
    # Day-of-week tariff periods classify future days (weekends/holidays) via
    # ``workday.check_date``. That service is a hard requirement — we never
    # second-guess it. If it isn't registered yet (workday still loading, or the
    # integration removed/broken), defer setup and let HA retry; PowerPilot does
    # not start until the service is available.
    if _uses_day_sensor(entry) and not hass.services.has_service(
        _WORKDAY_DOMAIN, _CHECK_DATE_SERVICE
    ):
        raise ConfigEntryNotReady(
            f"Serwis {_WORKDAY_DOMAIN}.{_CHECK_DATE_SERVICE} niedostępny — "
            "integracja workday nie jest gotowa, a taryfy używają czujnika dnia. "
            "Ponowię konfigurację, gdy serwis się pojawi."
        )

    coordinator = PowerPilotCoordinator(hass, entry)
    await coordinator.async_setup_modules()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id, None)
        if not hass.data.get(DOMAIN):
            async_unregister_panel(hass)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
