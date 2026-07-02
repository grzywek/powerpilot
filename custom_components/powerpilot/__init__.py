"""The PowerPilot integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady

from .const import (
    CONF_BATTERY_CHARGE_SENSOR,
    CONF_BATTERY_DISCHARGE_SENSOR,
    CONF_BUY_PRICE_SENSOR,
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    CONF_EV_CALENDAR,
    CONF_EV_CHARGE_METER_SENSOR,
    CONF_EV_CHARGING_SENSOR,
    CONF_EV_ENERGY_ADDED_SENSOR,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_ODOMETER_SENSOR,
    CONF_EV_SOC_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_SENSOR_PARENTS,
    CONF_SOC_SENSOR,
    CONF_TARIFFS,
    CONF_WEATHER_ENTITY,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import PowerPilotCoordinator
from .hierarchy import PARENT_ROOT
from .panel import async_register_panel, async_unregister_panel

_LOGGER = logging.getLogger(__name__)

_WORKDAY_DOMAIN = "workday"
_CHECK_DATE_SERVICE = "check_date"

# Entity states that mean "configured but not usable yet".
_NOT_READY_STATES = {"unavailable", "unknown"}

# Every configured entity is a hard setup dependency. PowerPilot must not start
# from a partial configuration because missing inputs produce misleading plans
# and charts.
_ENTITY_CONFIG_KEYS = (
    CONF_SOC_SENSOR,
    CONF_CONSUMPTION_SENSOR,
    CONF_BATTERY_CHARGE_SENSOR,
    CONF_BATTERY_DISCHARGE_SENSOR,
    CONF_GRID_IMPORT_SENSOR,
    CONF_BUY_PRICE_SENSOR,
    CONF_WEATHER_ENTITY,
    CONF_EV_SOC_SENSOR,
    CONF_EV_ODOMETER_SENSOR,
    CONF_EV_CHARGING_SENSOR,
    CONF_EV_ENERGY_ADDED_SENSOR,
    CONF_EV_CHARGE_METER_SENSOR,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_CALENDAR,
)
_ENTITY_LIST_CONFIG_KEYS = (CONF_DEVICE_SENSORS,)


def _uses_day_sensor(entry: ConfigEntry) -> bool:
    """Whether any configured tariff period keys off a workday day-sensor."""
    tariffs = {**entry.data, **entry.options}.get(CONF_TARIFFS) or []
    return any(
        period.get("day_sensor")
        for tariff in tariffs
        for period in (tariff.get("periods") or [])
    )


def _append_entity_id(entity_ids: list[str], seen: set[str], value) -> None:
    if not isinstance(value, str) or not value or value == PARENT_ROOT:
        return
    if value in seen:
        return
    seen.add(value)
    entity_ids.append(value)


def _configured_entity_ids(entry: ConfigEntry) -> list[str]:
    """Entity IDs explicitly stored in the config entry, in stable order."""
    cfg = {**entry.data, **entry.options}
    entity_ids: list[str] = []
    seen: set[str] = set()

    for key in _ENTITY_CONFIG_KEYS:
        _append_entity_id(entity_ids, seen, cfg.get(key))

    for key in _ENTITY_LIST_CONFIG_KEYS:
        for entity_id in cfg.get(key) or []:
            _append_entity_id(entity_ids, seen, entity_id)

    for child, parent in (cfg.get(CONF_SENSOR_PARENTS) or {}).items():
        _append_entity_id(entity_ids, seen, child)
        _append_entity_id(entity_ids, seen, parent)

    for tariff in cfg.get(CONF_TARIFFS) or []:
        for period in tariff.get("periods") or []:
            _append_entity_id(entity_ids, seen, period.get("day_sensor"))

    return entity_ids


def _unready_inputs(hass: HomeAssistant, entry: ConfigEntry) -> list[str]:
    """Configured entities that are missing or currently unavailable."""
    unready: list[str] = []
    for entity_id in _configured_entity_ids(entry):
        state = hass.states.get(entity_id)
        if state is None or state.state in _NOT_READY_STATES:
            unready.append(entity_id)
    return unready


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

    # Don't start until configured entities exist and report a usable value.
    # Like other integrations, defer setup and let HA retry — this keeps the
    # entry in a clean "waiting for entities" state instead of coming up with a
    # half-broken plan when source integrations are still loading.
    unready = _unready_inputs(hass, entry)
    if unready:
        raise ConfigEntryNotReady(
            "Czujniki wejściowe niedostępne: "
            + ", ".join(unready)
            + ". Ponowię konfigurację, gdy będą gotowe."
        )

    coordinator = PowerPilotCoordinator(hass, entry)
    await coordinator.async_setup_modules()
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    await async_register_panel(hass)
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    coordinator.async_start_hour_boundary_updates()
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator = hass.data[DOMAIN].pop(entry.entry_id, None)
        if coordinator is not None:
            coordinator.async_stop_hour_boundary_updates()
        if not hass.data.get(DOMAIN):
            async_unregister_panel(hass)
    return unloaded


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the entry when options change."""
    await hass.config_entries.async_reload(entry.entry_id)
