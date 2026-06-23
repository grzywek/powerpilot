"""Constants for the PowerPilot integration."""

from __future__ import annotations

from typing import Final

DOMAIN: Final = "powerpilot"
PLATFORMS: Final = ["sensor", "binary_sensor"]

# How often the optimization pipeline runs.
DEFAULT_UPDATE_INTERVAL_MINUTES: Final = 5

# ---------------------------------------------------------------------------
# Config / options keys
# ---------------------------------------------------------------------------

# --- Grid connection ---
CONF_PHASES: Final = "phases"  # 1 or 3
CONF_MAIN_FUSE_A: Final = "main_fuse_a"  # pre-meter breaker rating, amps
CONF_GRID_VOLTAGE: Final = "grid_voltage"  # volts per phase

# --- Battery / inverter ---
CONF_BATTERY_CAPACITY_KWH: Final = "battery_capacity_kwh"
CONF_INVERTER_MAX_CHARGE_KW: Final = "inverter_max_charge_kw"
CONF_INVERTER_MAX_DISCHARGE_KW: Final = "inverter_max_discharge_kw"
CONF_CHARGE_EFFICIENCY: Final = "charge_efficiency"  # 0..1
CONF_DISCHARGE_EFFICIENCY: Final = "discharge_efficiency"  # 0..1
CONF_BATTERY_WEAR_COST: Final = "battery_wear_cost"  # PLN per kWh throughput
CONF_MIN_SOC: Final = "min_soc"  # %
CONF_MAX_SOC: Final = "max_soc"  # %
CONF_GRID_DISCONNECT_SOC: Final = "grid_disconnect_soc"  # %
# Charge curve: list of {"soc_from", "soc_to", "max_kw"} segments.
CONF_CHARGE_CURVE: Final = "charge_curve"

# --- Linked entities ---
CONF_SOC_SENSOR: Final = "soc_sensor"  # current battery SoC %
CONF_BATTERY_CHARGE_SENSOR: Final = "battery_charge_sensor"  # kW or kWh (total_increasing)
CONF_BATTERY_DISCHARGE_SENSOR: Final = "battery_discharge_sensor"
CONF_GRID_IMPORT_SENSOR: Final = "grid_import_sensor"
CONF_CONSUMPTION_SENSOR: Final = "consumption_sensor"  # household power/energy
CONF_DEVICE_SENSORS: Final = "device_sensors"  # separately-metered loads to break out
CONF_CONSUMPTION_LEARN_DAYS: Final = "consumption_learn_days"  # history window
CONF_BUY_PRICE_SENSOR: Final = "buy_price_sensor"
CONF_WEATHER_ENTITY: Final = "weather_entity"

# --- Price source ---
CONF_PRICE_SOURCE: Final = "price_source"  # "sensor" | "pradcast"
CONF_PRADCAST_API_KEY: Final = "pradcast_api_key"
# Net seller markup added to wholesale RDN price before VAT.
CONF_PRICE_MARKUP: Final = "price_markup"  # additive PLN/kWh (seller's net fee)
CONF_PRICE_VAT: Final = "price_vat"  # multiplier applied after markup (e.g. 1.23)

PRICE_SOURCE_SENSOR: Final = "sensor"
PRICE_SOURCE_PRADCAST: Final = "pradcast"

# --- EV ---
CONF_EV_ENABLED: Final = "ev_enabled"
CONF_EV_SOC_SENSOR: Final = "ev_soc_sensor"
CONF_EV_LOCATION_SENSOR: Final = "ev_location_sensor"
CONF_EV_RANGE_KM: Final = "ev_range_km"  # km on a full charge
CONF_EV_BATTERY_KWH: Final = "ev_battery_kwh"
CONF_EV_WEEKLY_KM: Final = "ev_weekly_km"  # off-calendar weekly km
CONF_EV_CHARGER_KW: Final = "ev_charger_kw"  # per-phase draw (e.g. 3.5)
CONF_EV_CHARGER_PHASE: Final = "ev_charger_phase"  # shared phase index 1..3

# --- Distribution tariffs ---
# Stored in ``entry.options`` as a list of dicts (see ``models.Tariff.to_dict``).
# Snapshots of resolved per-hour distribution prices live in their own
# ``Store(version=STORAGE_VERSION_TARIFF_SNAPSHOTS, key=f"{DOMAIN}_{entry_id}_tariff_snapshots")``.
CONF_TARIFFS: Final = "tariffs"

# Storage version for the per-entry tariff snapshot store.
STORAGE_VERSION_TARIFF_SNAPSHOTS: Final = 1

DEFAULTS: Final = {
    CONF_PHASES: 3,
    CONF_MAIN_FUSE_A: 32,
    CONF_GRID_VOLTAGE: 230,
    CONF_BATTERY_CAPACITY_KWH: 10.0,
    CONF_INVERTER_MAX_CHARGE_KW: 3.0,
    CONF_INVERTER_MAX_DISCHARGE_KW: 3.0,
    CONF_CHARGE_EFFICIENCY: 0.95,
    CONF_DISCHARGE_EFFICIENCY: 0.95,
    CONF_BATTERY_WEAR_COST: 0.10,
    CONF_MIN_SOC: 10,
    CONF_MAX_SOC: 100,
    CONF_GRID_DISCONNECT_SOC: 15,
    CONF_PRICE_SOURCE: PRICE_SOURCE_SENSOR,
    CONF_PRICE_MARKUP: 0.0,
    CONF_PRICE_VAT: 1.0,
    CONF_CONSUMPTION_LEARN_DAYS: 21,
    CONF_EV_ENABLED: False,
    CONF_EV_RANGE_KM: 400,
    CONF_EV_BATTERY_KWH: 60.0,
    CONF_EV_WEEKLY_KM: 200,
    CONF_EV_CHARGER_KW: 3.5,
    CONF_EV_CHARGER_PHASE: 1,
}

# ---------------------------------------------------------------------------
# Decision enums (kept as plain strings for HA state friendliness)
# ---------------------------------------------------------------------------


class InverterMode:
    """Inverter operating mode for an hour."""

    CHARGE: Final = "charge"
    DISCHARGE: Final = "discharge"
    PASSTHROUGH: Final = "passthrough"


class ChargePower:
    """Whether the inverter may charge at full power or must be limited."""

    FULL: Final = "full"
    LIMITED: Final = "limited"


# Sensor / binary_sensor entity keys
SENSOR_INVERTER_MODE: Final = "inverter_mode"
SENSOR_CHARGE_POWER: Final = "charge_power"
SENSOR_BATTERY_ENERGY_COST: Final = "battery_energy_cost"
SENSOR_PLAN: Final = "plan"
SENSOR_NEXT_ACTION: Final = "next_action"
BINARY_GRID_CONNECTED: Final = "grid_connected"
BINARY_EV_CHARGE: Final = "ev_charge"
