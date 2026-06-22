"""Config and options flow for PowerPilot."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_WEAR_COST,
    CONF_BUY_PRICE_SENSOR,
    CONF_CHARGE_EFFICIENCY,
    CONF_CONSUMPTION_LEARN_DAYS,
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_EV_BATTERY_KWH,
    CONF_EV_CHARGER_KW,
    CONF_EV_ENABLED,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_RANGE_KM,
    CONF_EV_SOC_SENSOR,
    CONF_EV_WEEKLY_KM,
    CONF_GRID_DISCONNECT_SOC,
    CONF_INVERTER_MAX_CHARGE_KW,
    CONF_INVERTER_MAX_DISCHARGE_KW,
    CONF_MAIN_FUSE_A,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PHASES,
    CONF_PRADCAST_API_KEY,
    CONF_PRICE_MARKUP,
    CONF_PRICE_SOURCE,
    CONF_PRICE_VAT,
    CONF_SELL_PRICE_SENSOR,
    CONF_SOC_SENSOR,
    CONF_WEATHER_ENTITY,
    DEFAULTS,
    DOMAIN,
    PRICE_SOURCE_PRADCAST,
    PRICE_SOURCE_SENSOR,
)

_NUMBER = selector.NumberSelector
_NUM = selector.NumberSelectorConfig


def _entity(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _core_schema(data: dict[str, Any]) -> vol.Schema:
    def d(key):
        return data.get(key, DEFAULTS.get(key))

    return vol.Schema(
        {
            # Grid connection
            vol.Required(CONF_PHASES, default=d(CONF_PHASES)): vol.In([1, 3]),
            vol.Required(CONF_MAIN_FUSE_A, default=d(CONF_MAIN_FUSE_A)): _NUMBER(
                _NUM(min=6, max=160, step=1, unit_of_measurement="A", mode="box")
            ),
            # Battery / inverter
            vol.Required(
                CONF_BATTERY_CAPACITY_KWH, default=d(CONF_BATTERY_CAPACITY_KWH)
            ): _NUMBER(_NUM(min=1, max=200, step=0.1, unit_of_measurement="kWh", mode="box")),
            vol.Required(
                CONF_INVERTER_MAX_CHARGE_KW, default=d(CONF_INVERTER_MAX_CHARGE_KW)
            ): _NUMBER(_NUM(min=0.5, max=50, step=0.1, unit_of_measurement="kW", mode="box")),
            vol.Required(
                CONF_INVERTER_MAX_DISCHARGE_KW, default=d(CONF_INVERTER_MAX_DISCHARGE_KW)
            ): _NUMBER(_NUM(min=0.5, max=50, step=0.1, unit_of_measurement="kW", mode="box")),
            vol.Required(
                CONF_CHARGE_EFFICIENCY, default=d(CONF_CHARGE_EFFICIENCY)
            ): _NUMBER(_NUM(min=0.5, max=1, step=0.01, mode="slider")),
            vol.Required(
                CONF_DISCHARGE_EFFICIENCY, default=d(CONF_DISCHARGE_EFFICIENCY)
            ): _NUMBER(_NUM(min=0.5, max=1, step=0.01, mode="slider")),
            vol.Required(
                CONF_BATTERY_WEAR_COST, default=d(CONF_BATTERY_WEAR_COST)
            ): _NUMBER(_NUM(min=0, max=2, step=0.01, unit_of_measurement="PLN/kWh", mode="box")),
            vol.Required(CONF_MIN_SOC, default=d(CONF_MIN_SOC)): _NUMBER(
                _NUM(min=0, max=100, step=1, unit_of_measurement="%", mode="slider")
            ),
            vol.Required(CONF_MAX_SOC, default=d(CONF_MAX_SOC)): _NUMBER(
                _NUM(min=0, max=100, step=1, unit_of_measurement="%", mode="slider")
            ),
            vol.Required(
                CONF_GRID_DISCONNECT_SOC, default=d(CONF_GRID_DISCONNECT_SOC)
            ): _NUMBER(_NUM(min=0, max=100, step=1, unit_of_measurement="%", mode="slider")),
            # Linked entities
            vol.Optional(CONF_SOC_SENSOR, default=d(CONF_SOC_SENSOR) or vol.UNDEFINED): _entity("sensor"),
            vol.Optional(
                CONF_CONSUMPTION_SENSOR, default=d(CONF_CONSUMPTION_SENSOR) or vol.UNDEFINED
            ): _entity("sensor"),
            vol.Optional(
                CONF_DEVICE_SENSORS, default=d(CONF_DEVICE_SENSORS) or vol.UNDEFINED
            ): selector.EntitySelector(
                selector.EntitySelectorConfig(domain="sensor", multiple=True)
            ),
            vol.Optional(
                CONF_CONSUMPTION_LEARN_DAYS, default=d(CONF_CONSUMPTION_LEARN_DAYS)
            ): _NUMBER(_NUM(min=7, max=90, step=1, unit_of_measurement="d", mode="box")),
            vol.Optional(
                CONF_BUY_PRICE_SENSOR, default=d(CONF_BUY_PRICE_SENSOR) or vol.UNDEFINED
            ): _entity("sensor"),
            vol.Optional(
                CONF_SELL_PRICE_SENSOR, default=d(CONF_SELL_PRICE_SENSOR) or vol.UNDEFINED
            ): _entity("sensor"),
            vol.Optional(
                CONF_WEATHER_ENTITY, default=d(CONF_WEATHER_ENTITY) or vol.UNDEFINED
            ): _entity("weather"),
        }
    )


def _price_schema(data: dict[str, Any]) -> vol.Schema:
    def d(key):
        return data.get(key, DEFAULTS.get(key))

    return vol.Schema(
        {
            vol.Required(CONF_PRICE_SOURCE, default=d(CONF_PRICE_SOURCE)): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[PRICE_SOURCE_SENSOR, PRICE_SOURCE_PRADCAST],
                    translation_key="price_source",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_PRADCAST_API_KEY, default=d(CONF_PRADCAST_API_KEY) or vol.UNDEFINED
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_PRICE_MARKUP, default=d(CONF_PRICE_MARKUP)): _NUMBER(
                _NUM(min=0, max=2, step=0.01, unit_of_measurement="PLN/kWh", mode="box")
            ),
            vol.Optional(CONF_PRICE_VAT, default=d(CONF_PRICE_VAT)): _NUMBER(
                _NUM(min=1, max=2, step=0.01, mode="box")
            ),
        }
    )


def _ev_schema(data: dict[str, Any]) -> vol.Schema:
    def d(key):
        return data.get(key, DEFAULTS.get(key))

    return vol.Schema(
        {
            vol.Required(CONF_EV_ENABLED, default=d(CONF_EV_ENABLED)): selector.BooleanSelector(),
            vol.Optional(CONF_EV_SOC_SENSOR, default=d(CONF_EV_SOC_SENSOR) or vol.UNDEFINED): _entity("sensor"),
            vol.Optional(
                CONF_EV_LOCATION_SENSOR, default=d(CONF_EV_LOCATION_SENSOR) or vol.UNDEFINED
            ): _entity(["device_tracker", "binary_sensor", "person"]),
            vol.Optional(CONF_EV_RANGE_KM, default=d(CONF_EV_RANGE_KM)): _NUMBER(
                _NUM(min=50, max=1000, step=10, unit_of_measurement="km", mode="box")
            ),
            vol.Optional(CONF_EV_BATTERY_KWH, default=d(CONF_EV_BATTERY_KWH)): _NUMBER(
                _NUM(min=10, max=200, step=1, unit_of_measurement="kWh", mode="box")
            ),
            vol.Optional(CONF_EV_WEEKLY_KM, default=d(CONF_EV_WEEKLY_KM)): _NUMBER(
                _NUM(min=0, max=3000, step=10, unit_of_measurement="km", mode="box")
            ),
            vol.Optional(CONF_EV_CHARGER_KW, default=d(CONF_EV_CHARGER_KW)): _NUMBER(
                _NUM(min=1, max=22, step=0.1, unit_of_measurement="kW", mode="box")
            ),
        }
    )


class PowerPilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_prices()
        return self.async_show_form(step_id="user", data_schema=_core_schema({}))

    async def async_step_prices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()
        return self.async_show_form(step_id="prices", data_schema=_price_schema({}))

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="PowerPilot", data=self._data)
        return self.async_show_form(step_id="ev", data_schema=_ev_schema({}))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> "PowerPilotOptionsFlow":
        return PowerPilotOptionsFlow()


class PowerPilotOptionsFlow(OptionsFlow):
    """Allow editing all parameters after setup."""

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        merged = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_prices()
        return self.async_show_form(step_id="init", data_schema=_core_schema(merged))

    async def async_step_prices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        merged = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_ev()
        return self.async_show_form(step_id="prices", data_schema=_price_schema(merged))

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        merged = {**self.config_entry.data, **self.config_entry.options}
        if user_input is not None:
            self._data.update(user_input)
            return self.async_create_entry(title="", data=self._data)
        return self.async_show_form(step_id="ev", data_schema=_ev_schema(merged))
