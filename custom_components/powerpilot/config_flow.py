"""Config and options flow for PowerPilot."""

from __future__ import annotations

from typing import Any
from uuid import uuid4

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
    CONF_BATTERY_CHARGE_SENSOR,
    CONF_BATTERY_DISCHARGE_SENSOR,
    CONF_BATTERY_WEAR_COST,
    CONF_BUY_PRICE_SENSOR,
    CONF_CHARGE_CURVE,
    CONF_CHARGE_EFFICIENCY,
    CONF_CONSUMPTION_LEARN_DAYS,
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    CONF_DISCHARGE_EFFICIENCY,
    CONF_EV_BATTERY_KWH,
    CONF_EV_CALENDAR,
    CONF_EV_CALENDAR_KEYWORD,
    CONF_EV_CHARGER_CONNECTED_SENSOR,
    CONF_EV_CHARGER_KW,
    CONF_EV_CHARGER_PHASES,
    CONF_EV_CHARGING_SENSOR,
    CONF_EV_ENABLED,
    CONF_EV_ENERGY_ADDED_SENSOR,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_RANGE_KM,
    CONF_EV_SOC_SENSOR,
    CONF_EV_TARGET_SOC_SENSOR,
    CONF_EV_WEEKLY_KM,
    CONF_GRID_DISCONNECT_SOC,
    CONF_GRID_IMPORT_SENSOR,
    CONF_INVERTER_MAX_CHARGE_KW,
    CONF_INVERTER_MAX_DISCHARGE_KW,
    CONF_MAIN_FUSE_A,
    CONF_MAX_SOC,
    CONF_MIN_SOC,
    CONF_PHASES,
    CONF_EXCISE_KWH,
    CONF_PRADCAST_API_KEY,
    CONF_PRICE_MARKUP,
    CONF_PRICE_REFRESH_HOURS,
    CONF_PRICE_ROUNDING,
    CONF_PRICE_SOURCE,
    CONF_PRICE_VAT,
    CONF_SENSOR_PARENTS,
    CONF_SOC_SENSOR,
    CONF_TARIFFS,
    CONF_WEATHER_ENTITY,
    CHARGE_CURVE_BANDS,
    DEFAULTS,
    DOMAIN,
    PRICE_ROUNDING_OPTIONS,
    PRICE_SOURCE_PRADCAST,
    PRICE_SOURCE_SENSOR,
    charge_curve_band_key,
)
from .hierarchy import PARENT_ROOT
from .models import Tariff, TariffPeriod, ValidityRange

_NUMBER = selector.NumberSelector
_NUM = selector.NumberSelectorConfig

# Transient per-device parent field prefix; reassembled into CONF_SENSOR_PARENTS
# on submit so the field keys never leak into the saved options.
_PARENT_FIELD_PREFIX = "parent__"


def _parent_field_key(entity_id: str) -> str:
    return f"{_PARENT_FIELD_PREFIX}{entity_id}"


def _normalize_core_input(user_input: dict[str, Any]) -> dict[str, Any]:
    """Coerce core-form values back to their stored types.

    ``CONF_PHASES`` is rendered as a string-valued select but stored as an int,
    so the select's string output is coerced here before it reaches the options.
    """
    data = dict(user_input)
    if CONF_PHASES in data:
        data[CONF_PHASES] = int(data[CONF_PHASES])
    return data


def _entity(domain: str | list[str]) -> selector.EntitySelector:
    return selector.EntitySelector(selector.EntitySelectorConfig(domain=domain))


def _core_schema(data: dict[str, Any]) -> vol.Schema:
    def d(key):
        return data.get(key, DEFAULTS.get(key))

    return vol.Schema(
        {
            # Grid connection.
            # Rendered as a radio list of string options; the stored value is an
            # int, so the default is stringified to pre-select on re-open and the
            # submitted value is coerced back to int (see _normalize_core_input).
            vol.Required(CONF_PHASES, default=str(d(CONF_PHASES))): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value="1", label="1"),
                        selector.SelectOptionDict(value="3", label="3"),
                    ],
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
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
            ): _NUMBER(_NUM(min=0, max=2, step=0.01, unit_of_measurement="PLN/kWh netto", mode="box")),
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
                CONF_BATTERY_CHARGE_SENSOR, default=d(CONF_BATTERY_CHARGE_SENSOR) or vol.UNDEFINED
            ): _entity("sensor"),
            vol.Optional(
                CONF_BATTERY_DISCHARGE_SENSOR, default=d(CONF_BATTERY_DISCHARGE_SENSOR) or vol.UNDEFINED
            ): _entity("sensor"),
            vol.Optional(
                CONF_GRID_IMPORT_SENSOR, default=d(CONF_GRID_IMPORT_SENSOR) or vol.UNDEFINED
            ): _entity("sensor"),
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
            # Energy price feed for the "Home Assistant sensor" price source.
            vol.Optional(
                CONF_BUY_PRICE_SENSOR, default=d(CONF_BUY_PRICE_SENSOR) or vol.UNDEFINED
            ): _entity("sensor"),
            vol.Optional(
                CONF_PRADCAST_API_KEY, default=d(CONF_PRADCAST_API_KEY) or vol.UNDEFINED
            ): selector.TextSelector(
                selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD)
            ),
            vol.Optional(CONF_PRICE_MARKUP, default=d(CONF_PRICE_MARKUP)): _NUMBER(
                _NUM(min=0, max=2, step=0.01, unit_of_measurement="PLN/kWh netto", mode="box")
            ),
            vol.Optional(CONF_PRICE_VAT, default=d(CONF_PRICE_VAT)): _NUMBER(
                _NUM(min=1, max=2, step=0.01, mode="box")
            ),
            vol.Optional(CONF_EXCISE_KWH, default=d(CONF_EXCISE_KWH)): _NUMBER(
                _NUM(min=0, max=0.5, step=0.001, unit_of_measurement="PLN/kWh netto", mode="box")
            ),
            vol.Optional(
                CONF_PRICE_ROUNDING, default=d(CONF_PRICE_ROUNDING)
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=list(PRICE_ROUNDING_OPTIONS),
                    translation_key="price_rounding",
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(
                CONF_PRICE_REFRESH_HOURS, default=d(CONF_PRICE_REFRESH_HOURS)
            ): _NUMBER(_NUM(min=1, max=24, step=1, unit_of_measurement="h", mode="box")),
        }
    )


def _ev_schema(data: dict[str, Any]) -> vol.Schema:
    def d(key):
        return data.get(key, DEFAULTS.get(key))

    return vol.Schema(
        {
            vol.Required(CONF_EV_ENABLED, default=d(CONF_EV_ENABLED)): selector.BooleanSelector(),
            vol.Optional(CONF_EV_SOC_SENSOR, default=d(CONF_EV_SOC_SENSOR) or vol.UNDEFINED): _entity(["sensor", "input_number", "number"]),
            vol.Optional(
                CONF_EV_TARGET_SOC_SENSOR, default=d(CONF_EV_TARGET_SOC_SENSOR) or vol.UNDEFINED
            ): _entity(["sensor", "input_number", "number"]),
            vol.Optional(
                CONF_EV_CHARGER_CONNECTED_SENSOR,
                default=d(CONF_EV_CHARGER_CONNECTED_SENSOR) or vol.UNDEFINED,
            ): _entity(["binary_sensor", "switch", "input_boolean"]),
            vol.Optional(
                CONF_EV_CHARGING_SENSOR, default=d(CONF_EV_CHARGING_SENSOR) or vol.UNDEFINED
            ): _entity(["binary_sensor", "switch", "sensor"]),
            vol.Optional(
                CONF_EV_ENERGY_ADDED_SENSOR, default=d(CONF_EV_ENERGY_ADDED_SENSOR) or vol.UNDEFINED
            ): _entity("sensor"),
            vol.Optional(
                CONF_EV_LOCATION_SENSOR, default=d(CONF_EV_LOCATION_SENSOR) or vol.UNDEFINED
            ): _entity(["device_tracker", "binary_sensor", "person"]),
            vol.Optional(
                CONF_EV_CALENDAR, default=d(CONF_EV_CALENDAR) or vol.UNDEFINED
            ): _entity("calendar"),
            vol.Optional(
                CONF_EV_CALENDAR_KEYWORD, default=d(CONF_EV_CALENDAR_KEYWORD)
            ): selector.TextSelector(),
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
            vol.Optional(
                CONF_EV_CHARGER_PHASES, default=str(d(CONF_EV_CHARGER_PHASES))
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=["1", "3"],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _charge_curve_schema(data: dict[str, Any]) -> vol.Schema:
    """One max-charge-power field per SoC band.

    Each band defaults to its stored value (matched by ``soc_from``) or, if the
    curve is not configured yet, to the flat inverter charge limit — so leaving
    every field at its default reproduces today's flat behaviour.
    """
    stored = {
        int(seg["soc_from"]): float(seg["max_kw"])
        for seg in (data.get(CONF_CHARGE_CURVE) or [])
    }
    flat = float(
        data.get(CONF_INVERTER_MAX_CHARGE_KW, DEFAULTS[CONF_INVERTER_MAX_CHARGE_KW])
    )
    fields: dict[Any, Any] = {}
    for band in CHARGE_CURVE_BANDS:
        lo, _hi = band
        fields[
            vol.Required(charge_curve_band_key(band), default=stored.get(lo, flat))
        ] = _NUMBER(_NUM(min=0, max=50, step=0.1, unit_of_measurement="kW", mode="box"))
    return vol.Schema(fields)


def _charge_curve_segments(user_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Assemble the per-band form fields into ``CONF_CHARGE_CURVE`` segments."""
    return [
        {
            "soc_from": lo,
            "soc_to": hi,
            "max_kw": float(user_input[charge_curve_band_key((lo, hi))]),
        }
        for (lo, hi) in CHARGE_CURVE_BANDS
    ]


class PowerPilotConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the initial configuration."""

    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if self._async_current_entries():
            return self.async_abort(reason="single_instance_allowed")
        if user_input is not None:
            self._data.update(_normalize_core_input(user_input))
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
    """Menu-based options flow.

    The init step shows a menu; each sub-section (core, prices, EV, tariffs)
    saves into ``self._data`` and returns to the menu. The user finalises by
    picking *Save & exit* from the menu.

    Tariff editing is multi-level:

        tariff_list ──► tariff_form ──► period_list ──► period_form
                ▲                          │   │
                │                          │   └► range_list ──► range_form
                └──────────────────────────┘
    """

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._loaded = False
        # Edit context for the tariff sub-flow.
        self._editing_tariff_id: str | None = None
        self._editing_period_id: str | None = None
        self._editing_range_id: str | None = None

    # ------------------------------------------------------------------ utils

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._data = {**self.config_entry.data, **self.config_entry.options}
        self._loaded = True

    def _tariffs(self) -> list[Tariff]:
        raw = self._data.get(CONF_TARIFFS) or []
        return [Tariff.from_dict(t) for t in raw]

    def _save_tariffs(self, tariffs: list[Tariff]) -> None:
        self._data[CONF_TARIFFS] = [t.to_dict() for t in tariffs]

    def _current_tariff(self) -> Tariff | None:
        if not self._editing_tariff_id:
            return None
        for t in self._tariffs():
            if t.id == self._editing_tariff_id:
                return t
        return None

    # ------------------------------------------------------------ menu (init)

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        return self.async_show_menu(
            step_id="init",
            menu_options=[
                "core",
                "hierarchy",
                "charge_curve",
                "prices",
                "ev",
                "tariff_list",
                "reset_data",
                "finish",
            ],
        )

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        return self.async_create_entry(title="", data=self._data)

    # --------------------------------------------------------- clear data/cache

    async def async_step_reset_data(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Wipe persisted data/cache (learned profiles, archives, snapshots).

        Configuration is preserved — only the stored data is removed. The entry
        is reloaded afterwards so every module re-initialises from empty stores.
        """
        self._ensure_loaded()
        if user_input is not None:
            if user_input.get("confirm"):
                coordinator = self.hass.data.get(DOMAIN, {}).get(
                    self.config_entry.entry_id
                )
                if coordinator is not None:
                    await coordinator.async_clear_data()
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.config_entry.entry_id)
                )
            return await self.async_step_init()
        return self.async_show_form(
            step_id="reset_data",
            data_schema=vol.Schema(
                {vol.Required("confirm", default=False): selector.BooleanSelector()}
            ),
        )

    # ----------------------------------------------------- existing sub-forms

    async def async_step_core(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        if user_input is not None:
            self._data.update(_normalize_core_input(user_input))
            return await self.async_step_init()
        return self.async_show_form(step_id="core", data_schema=_core_schema(self._data))

    # ----------------------------------------------------------- meter hierarchy

    def _friendly(self, entity_id: str | None) -> str:
        """Human label for a sensor: ``Friendly name (entity_id)``."""
        if not entity_id:
            return "—"
        state = self.hass.states.get(entity_id)
        if state and (name := state.name):
            return f"{name} ({entity_id})"
        return entity_id

    def _hierarchy_schema(self, devices: list[str]) -> vol.Schema:
        """One parent dropdown per device sensor (root or another device)."""
        root_label = self._friendly(self._data.get(CONF_CONSUMPTION_SENSOR))
        stored = dict(self._data.get(CONF_SENSOR_PARENTS) or {})
        fields: dict[Any, Any] = {}
        for eid in devices:
            options = [
                selector.SelectOptionDict(
                    value=PARENT_ROOT, label=f"⬆ Główny: {root_label}"
                )
            ]
            options.extend(
                selector.SelectOptionDict(value=other, label=self._friendly(other))
                for other in devices
                if other != eid
            )
            valid = {PARENT_ROOT, *(o for o in devices if o != eid)}
            default = stored.get(eid, PARENT_ROOT)
            if default not in valid:
                default = PARENT_ROOT
            fields[
                vol.Required(_parent_field_key(eid), default=default)
            ] = selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options, mode=selector.SelectSelectorMode.DROPDOWN
                )
            )
        return vol.Schema(fields)

    async def async_step_hierarchy(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._ensure_loaded()
        devices = list(self._data.get(CONF_DEVICE_SENSORS) or [])

        if user_input is not None:
            parents: dict[str, str] = {}
            for eid in devices:
                value = user_input.get(_parent_field_key(eid))
                if value and value != PARENT_ROOT:
                    parents[eid] = value
            self._data[CONF_SENSOR_PARENTS] = parents
            return await self.async_step_init()

        if not devices:
            return self.async_show_form(
                step_id="hierarchy",
                data_schema=vol.Schema({}),
                description_placeholders={
                    "info": "⚠️ Brak czujników urządzeń — dodaj je najpierw w sekcji Core."
                },
            )
        return self.async_show_form(
            step_id="hierarchy",
            data_schema=self._hierarchy_schema(devices),
            description_placeholders={"info": ""},
        )

    async def async_step_charge_curve(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._ensure_loaded()
        if user_input is not None:
            # Store only the canonical segment list; the per-band field keys are
            # transient and must not leak into the saved options.
            self._data[CONF_CHARGE_CURVE] = _charge_curve_segments(user_input)
            return await self.async_step_init()
        return self.async_show_form(
            step_id="charge_curve", data_schema=_charge_curve_schema(self._data)
        )

    async def async_step_prices(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_init()
        return self.async_show_form(step_id="prices", data_schema=_price_schema(self._data))

    async def async_step_ev(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_init()
        return self.async_show_form(step_id="ev", data_schema=_ev_schema(self._data))

    # ------------------------------------------------------------- tariff list

    async def async_step_tariff_list(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        tariffs = self._tariffs()

        if user_input is not None:
            action = user_input["action"]
            if action == "__back__":
                return await self.async_step_init()
            if action == "__add__":
                self._editing_tariff_id = None
                return await self.async_step_tariff_form()
            kind, _, tid = action.partition(":")
            if kind == "edit":
                self._editing_tariff_id = tid
                return await self.async_step_tariff_form()
            if kind == "del":
                remaining = [t for t in tariffs if t.id != tid]
                self._save_tariffs(remaining)
                return await self.async_step_tariff_list()
            return await self.async_step_tariff_list()

        options: list[selector.SelectOptionDict] = [
            selector.SelectOptionDict(value="__add__", label="➕ Dodaj nową taryfę"),
        ]
        for t in tariffs:
            range_count = len(t.validity_ranges)
            range_label = f" ({range_count} zakr.)" if range_count else " (zawsze)"
            options.append(
                selector.SelectOptionDict(
                    value=f"edit:{t.id}",
                    label=f"✎ {t.name}{range_label} — {len(t.periods)} okr.",
                )
            )
            options.append(
                selector.SelectOptionDict(
                    value=f"del:{t.id}",
                    label=f"🗑 Usuń: {t.name}",
                )
            )
        options.append(selector.SelectOptionDict(value="__back__", label="← Powrót do menu"))

        return self.async_show_form(
            step_id="tariff_list",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={"count": str(len(tariffs))},
        )

    # -------------------------------------------------------------- tariff form

    async def async_step_tariff_form(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        existing = self._current_tariff()

        if user_input is not None:
            tariffs = self._tariffs()
            new_tariff = Tariff(
                id=existing.id if existing else uuid4().hex,
                name=str(user_input["name"]).strip(),
                base_component_kwh=float(user_input["base_component_kwh"]),
                vat_rate=float(user_input["vat_rate"]),
                fixed_monthly_cost=float(user_input["fixed_monthly_cost"]),
                validity_ranges=existing.validity_ranges if existing else [],
                periods=existing.periods if existing else [],
            )
            if existing:
                tariffs = [new_tariff if t.id == existing.id else t for t in tariffs]
            else:
                tariffs.append(new_tariff)
                self._editing_tariff_id = new_tariff.id
            self._save_tariffs(tariffs)
            return await self.async_step_period_list()

        defaults = {
            "name": existing.name if existing else "",
            "base_component_kwh": existing.base_component_kwh if existing else 0.0435,
            # New tariffs default to 23 % VAT (standard PL rate); legacy
            # tariffs loaded without a vat_rate get 0.0 from the model and we
            # surface that as-is so the user sees what's stored.
            "vat_rate": existing.vat_rate if existing else 0.23,
            "fixed_monthly_cost": existing.fixed_monthly_cost if existing else 0.0,
        }
        schema = vol.Schema(
            {
                vol.Required("name", default=defaults["name"]): selector.TextSelector(),
                vol.Required("base_component_kwh", default=defaults["base_component_kwh"]): _NUMBER(
                    _NUM(min=0, max=5, step="any", unit_of_measurement="PLN/kWh netto", mode="box")
                ),
                vol.Required("vat_rate", default=defaults["vat_rate"]): _NUMBER(
                    _NUM(min=0, max=1, step=0.01, unit_of_measurement="× netto", mode="box")
                ),
                vol.Required(
                    "fixed_monthly_cost", default=defaults["fixed_monthly_cost"]
                ): _NUMBER(
                    _NUM(min=0, max=1000, step="any", unit_of_measurement="PLN/mies. netto", mode="box")
                ),
            }
        )
        return self.async_show_form(
            step_id="tariff_form",
            data_schema=schema,
            description_placeholders={
                "mode": "edycja" if existing else "nowa",
                "name": existing.name if existing else "—",
            },
        )

    # -------------------------------------------------------------- period list

    async def async_step_period_list(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        tariff = self._current_tariff()
        if tariff is None:
            return await self.async_step_tariff_list()

        if user_input is not None:
            action = user_input["action"]
            if action == "__back__":
                self._editing_tariff_id = None
                return await self.async_step_tariff_list()
            if action == "__add__":
                self._editing_period_id = None
                return await self.async_step_period_form()
            if action == "__ranges__":
                return await self.async_step_range_list()
            kind, _, pid = action.partition(":")
            if kind == "edit":
                self._editing_period_id = pid
                return await self.async_step_period_form()
            if kind == "del":
                tariffs = self._tariffs()
                for t in tariffs:
                    if t.id == tariff.id:
                        t.periods = [p for p in t.periods if p.id != pid]
                self._save_tariffs(tariffs)
                return await self.async_step_period_list()
            return await self.async_step_period_list()

        options: list[selector.SelectOptionDict] = [
            selector.SelectOptionDict(value="__add__", label="➕ Dodaj okres"),
            selector.SelectOptionDict(
                value="__ranges__",
                label=f"📅 Daty obowiązywania ({len(tariff.validity_ranges)})",
            ),
        ]
        for p in tariff.periods:
            options.append(
                selector.SelectOptionDict(
                    value=f"edit:{p.id}",
                    label=f"✎ {p.name} — {p.hour_from:02d}-{p.hour_to:02d}, {p.price_kwh:.4f} PLN/kWh",
                )
            )
            options.append(
                selector.SelectOptionDict(value=f"del:{p.id}", label=f"🗑 Usuń: {p.name}")
            )
        options.append(selector.SelectOptionDict(value="__back__", label="← Powrót do listy taryf"))

        return self.async_show_form(
            step_id="period_list",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={
                "tariff_name": tariff.name,
                "base_component": f"{tariff.base_component_kwh:.4f}",
                "count": str(len(tariff.periods)),
            },
        )

    # -------------------------------------------------------------- period form

    async def async_step_period_form(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        tariff = self._current_tariff()
        if tariff is None:
            return await self.async_step_tariff_list()
        existing = next((p for p in tariff.periods if p.id == self._editing_period_id), None)

        if user_input is not None:
            hour_from = int(user_input["hour_from"])
            hour_to = int(user_input["hour_to"])
            new_period = TariffPeriod(
                id=existing.id if existing else uuid4().hex,
                name=str(user_input["name"]).strip(),
                hour_from=hour_from,
                hour_to=hour_to,
                price_kwh=float(user_input["price_kwh"]),
                day_sensor=user_input.get("day_sensor") or None,
            )
            tariffs = self._tariffs()
            for t in tariffs:
                if t.id == tariff.id:
                    if existing:
                        t.periods = [new_period if p.id == existing.id else p for p in t.periods]
                    else:
                        t.periods.append(new_period)
            self._save_tariffs(tariffs)
            self._editing_period_id = None
            return await self.async_step_period_list()

        defaults = {
            "name": existing.name if existing else "",
            "hour_from": existing.hour_from if existing else 0,
            "hour_to": existing.hour_to if existing else 24,
            "price_kwh": existing.price_kwh if existing else 0.0,
            "day_sensor": existing.day_sensor if existing and existing.day_sensor else vol.UNDEFINED,
        }
        schema = vol.Schema(
            {
                vol.Required("name", default=defaults["name"]): selector.TextSelector(),
                vol.Required("hour_from", default=defaults["hour_from"]): _NUMBER(
                    _NUM(min=0, max=23, step=1, unit_of_measurement="h", mode="box")
                ),
                vol.Required("hour_to", default=defaults["hour_to"]): _NUMBER(
                    _NUM(min=1, max=24, step=1, unit_of_measurement="h", mode="box")
                ),
                vol.Required("price_kwh", default=defaults["price_kwh"]): _NUMBER(
                    _NUM(min=0, max=5, step="any", unit_of_measurement="PLN/kWh netto", mode="box")
                ),
                vol.Optional("day_sensor", default=defaults["day_sensor"]): _entity("binary_sensor"),
            }
        )
        return self.async_show_form(
            step_id="period_form",
            data_schema=schema,
            description_placeholders={
                "mode": "edycja" if existing else "nowy",
                "tariff_name": tariff.name,
            },
        )

    # ---------------------------------------------------------- validity ranges

    async def async_step_range_list(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        tariff = self._current_tariff()
        if tariff is None:
            return await self.async_step_tariff_list()

        if user_input is not None:
            action = user_input["action"]
            if action == "__back__":
                return await self.async_step_period_list()
            if action == "__add__":
                self._editing_range_id = None
                return await self.async_step_range_form()
            kind, _, rid = action.partition(":")
            if kind == "edit":
                self._editing_range_id = rid
                return await self.async_step_range_form()
            if kind == "del":
                tariffs = self._tariffs()
                for t in tariffs:
                    if t.id == tariff.id:
                        t.validity_ranges = [r for r in t.validity_ranges if r.id != rid]
                self._save_tariffs(tariffs)
                return await self.async_step_range_list()
            return await self.async_step_range_list()

        options: list[selector.SelectOptionDict] = [
            selector.SelectOptionDict(value="__add__", label="➕ Dodaj zakres dat"),
        ]
        if not tariff.validity_ranges:
            options.append(
                selector.SelectOptionDict(
                    value="__noop__",
                    label="ℹ Brak zakresów — taryfa obowiązuje zawsze",
                )
            )
        for r in tariff.validity_ranges:
            label_from = r.valid_from.isoformat() if r.valid_from else "…"
            label_to = r.valid_to.isoformat() if r.valid_to else "…"
            options.append(
                selector.SelectOptionDict(
                    value=f"edit:{r.id}",
                    label=f"✎ {label_from} → {label_to}",
                )
            )
            options.append(
                selector.SelectOptionDict(value=f"del:{r.id}", label=f"🗑 Usuń: {label_from} → {label_to}")
            )
        options.append(selector.SelectOptionDict(value="__back__", label="← Powrót do okresów"))

        return self.async_show_form(
            step_id="range_list",
            data_schema=vol.Schema(
                {
                    vol.Required("action"): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=options,
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            description_placeholders={
                "tariff_name": tariff.name,
                "count": str(len(tariff.validity_ranges)),
            },
        )

    async def async_step_range_form(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._ensure_loaded()
        tariff = self._current_tariff()
        if tariff is None:
            return await self.async_step_tariff_list()
        existing = next(
            (r for r in tariff.validity_ranges if r.id == self._editing_range_id), None
        )

        if user_input is not None:
            from datetime import date as _date

            def _to_date(value: Any) -> _date | None:
                if not value:
                    return None
                if isinstance(value, _date):
                    return value
                return _date.fromisoformat(str(value))

            new_range = ValidityRange(
                id=existing.id if existing else uuid4().hex,
                valid_from=_to_date(user_input.get("valid_from")),
                valid_to=_to_date(user_input.get("valid_to")),
            )
            tariffs = self._tariffs()
            for t in tariffs:
                if t.id == tariff.id:
                    if existing:
                        t.validity_ranges = [
                            new_range if r.id == existing.id else r for r in t.validity_ranges
                        ]
                    else:
                        t.validity_ranges.append(new_range)
            self._save_tariffs(tariffs)
            self._editing_range_id = None
            return await self.async_step_range_list()

        defaults = {
            "valid_from": existing.valid_from.isoformat()
            if existing and existing.valid_from
            else vol.UNDEFINED,
            "valid_to": existing.valid_to.isoformat()
            if existing and existing.valid_to
            else vol.UNDEFINED,
        }
        schema = vol.Schema(
            {
                vol.Optional("valid_from", default=defaults["valid_from"]): selector.DateSelector(),
                vol.Optional("valid_to", default=defaults["valid_to"]): selector.DateSelector(),
            }
        )
        return self.async_show_form(
            step_id="range_form",
            data_schema=schema,
            description_placeholders={
                "mode": "edycja" if existing else "nowy",
                "tariff_name": tariff.name,
            },
        )

