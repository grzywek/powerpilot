"""Constants for the PowerPilot integration."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Final

DOMAIN: Final = "powerpilot"
PLATFORMS: Final = ["sensor", "binary_sensor", "number"]
INTEGRATION_VERSION: Final[str] = json.loads(
    Path(__file__).with_name("manifest.json").read_text(encoding="utf-8")
)["version"]

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
# Below this grid-side charge power the optimizer won't force-charge at all
# (0 disables the floor). Stops trivial sub-kW charge dribbles like 0.20 kW.
CONF_MIN_CHARGE_POWER_KW: Final = "min_charge_power_kw"
CONF_INVERTER_MAX_DISCHARGE_KW: Final = "inverter_max_discharge_kw"
CONF_CHARGE_EFFICIENCY: Final = "charge_efficiency"  # 0..1
CONF_DISCHARGE_EFFICIENCY: Final = "discharge_efficiency"  # 0..1
CONF_BATTERY_WEAR_COST: Final = "battery_wear_cost"  # PLN per kWh throughput
CONF_MIN_SOC: Final = "min_soc"  # %
CONF_MAX_SOC: Final = "max_soc"  # %
# Charge curve: list of {"soc_from", "soc_to", "max_kw"} segments.
CONF_CHARGE_CURVE: Final = "charge_curve"
# Power-dependent charge efficiency: list of {"kw", "eff"} points (eff is a
# 0..1 fraction) sampled from the inverter's efficiency chart. The optimizer
# builds a piecewise-linear stored-energy function from them and picks charge
# powers accordingly (charging slower in the efficiency sweet spot beats one
# full-power hour). Empty → the flat CONF_CHARGE_EFFICIENCY applies.
CONF_CHARGE_EFFICIENCY_CURVE: Final = "charge_efficiency_curve"

# --- Linked entities ---
CONF_SOC_SENSOR: Final = "soc_sensor"  # current battery SoC %
CONF_BATTERY_CHARGE_SENSOR: Final = "battery_charge_sensor"  # kW or kWh (total_increasing)
CONF_BATTERY_DISCHARGE_SENSOR: Final = "battery_discharge_sensor"
CONF_GRID_IMPORT_SENSOR: Final = "grid_import_sensor"
CONF_CONSUMPTION_SENSOR: Final = "consumption_sensor"  # household power/energy (tree root)
CONF_DEVICE_SENSORS: Final = "device_sensors"  # separately-metered loads to break out
# Meter nesting: ``{device_entity_id: parent_entity_id}``. Parent is another
# device sensor; devices absent from the map (or pointing at PARENT_ROOT) hang
# directly under the main consumption sensor. Lets nested sub-meters (washer ⊂
# apartment ⊂ Victron output) be counted exactly once. See ``hierarchy.py``.
CONF_SENSOR_PARENTS: Final = "sensor_parents"
CONF_CONSUMPTION_LEARN_DAYS: Final = "consumption_learn_days"  # history window
CONF_BUY_PRICE_SENSOR: Final = "buy_price_sensor"
CONF_WEATHER_ENTITY: Final = "weather_entity"
# Which consumption sensors' loads depend on the weather (e.g. AC meters, a
# heat-pump meter). Each device's hourly profile is then learned as a function
# of the outside temperature (climate module) instead of the weekday+hour
# weekly average.
CONF_CLIMATE_SENSORS: Final = "climate_sensors"
# Legacy single-sensor key. Superseded by CONF_CLIMATE_SENSORS (a list); kept
# only so ``async_setup_entry`` can seed the new list once on upgrade —
# nothing reads it at runtime anymore.
CONF_CLIMATE_SENSOR: Final = "climate_sensor"
# Presence entities (person.*, device_tracker.*, binary_sensor.*) gating the
# temperature-profile learning: an hour is folded into a profile only when
# someone was home during it ("anyone home" — any entity reading home/on).
# Hours whose presence is unknown still learn; empty list → every hour learns.
CONF_CLIMATE_PRESENCE_SENSORS: Final = "climate_presence_sensors"

# Storage version for the per-entry climate (temperature-profile) store.
STORAGE_VERSION_CLIMATE: Final = 1

# --- Price source ---
CONF_PRICE_SOURCE: Final = "price_source"  # "sensor" | "pradcast"
CONF_PRADCAST_API_KEY: Final = "pradcast_api_key"
# Net seller markup added to wholesale RDN price before VAT.
CONF_PRICE_MARKUP: Final = "price_markup"  # additive PLN/kWh (seller's net fee)
CONF_PRICE_VAT: Final = "price_vat"  # multiplier applied after markup (e.g. 1.23)
# Net excise duty (akcyza) added per kWh on the energy side, inside the VAT base.
CONF_EXCISE_KWH: Final = "excise_kwh"  # additive PLN/kWh netto (akcyza)
# How the displayed full price is rounded so it can match a specific seller's
# bill to the grosz. See ``pricing.assemble`` / ``PRICE_ROUNDING_*``.
CONF_PRICE_ROUNDING: Final = "price_rounding"
# How often the price source is actually re-fetched (forecasts change). The
# optimizer itself runs on exact clock-hour boundaries off cached prices.
CONF_PRICE_REFRESH_HOURS: Final = "price_refresh_hours"

# Rounding schemes for the displayed gross per-kWh price.
PRICE_ROUNDING_PER_BUCKET: Final = "per_bucket"  # round prąd/dystrybucja/podatki separately (seller-style)
PRICE_ROUNDING_TOTAL: Final = "total"  # one gross sum, rounded once
PRICE_ROUNDING_NONE: Final = "none"  # raw, full precision
PRICE_ROUNDING_OPTIONS: Final = (
    PRICE_ROUNDING_PER_BUCKET,
    PRICE_ROUNDING_TOTAL,
    PRICE_ROUNDING_NONE,
)

PRICE_SOURCE_SENSOR: Final = "sensor"
PRICE_SOURCE_PRADCAST: Final = "pradcast"

# Price provenance for the archive / "Ceny" tab.
PRICE_TYPE_CERTAIN: Final = "certain"  # binding RDN — final, never overwritten
PRICE_TYPE_FORECAST: Final = "forecast"  # published forecast — refreshed each fetch
PRICE_TYPE_ESTIMATED: Final = "estimated"  # weighted weekday+hour average (derived)

# Storage version for the per-entry energy-price archive store.
STORAGE_VERSION_PRICE_ARCHIVE: Final = 1

# Weights for the estimated price: same weekday+hour, 1 / 2 / 3 weeks ago.
# More recent weeks weigh more; renormalised over whatever samples exist.
ESTIMATE_WEEKLY_WEIGHTS: Final = (0.5, 0.3, 0.2)

# --- EV ---
CONF_EV_ENABLED: Final = "ev_enabled"
CONF_EV_SOC_SENSOR: Final = "ev_soc_sensor"
# Live "is the car home" tracker — feeds the plug-in reminder and the
# plan-vs-reality "charging away from home" note. It no longer gates whether
# the optimizer is allowed to plan charging (see CONF_EV_CALENDAR: calendar
# events with a non-home ``location`` are the only source of *unavailability*).
CONF_EV_LOCATION_SENSOR: Final = "ev_location_sensor"
# Extra presence entities (phones, person.*) combined with the car tracker —
# car trackers often poll rarely, so a fresh "not home" from any of these
# beats a stale "home" from the car. Home only when every known reading agrees.
CONF_EV_PRESENCE_ENTITIES: Final = "ev_presence_entities"
CONF_EV_RANGE_KM: Final = "ev_range_km"  # km on a full charge
CONF_EV_BATTERY_KWH: Final = "ev_battery_kwh"
CONF_EV_WEEKLY_KM: Final = "ev_weekly_km"  # off-calendar weekly km
CONF_EV_CHARGER_KW: Final = "ev_charger_kw"  # per-phase draw (e.g. 3.5)
CONF_EV_CHARGER_PHASE: Final = "ev_charger_phase"  # shared phase index 1..3
CONF_EV_CHARGER_PHASES: Final = "ev_charger_phases"  # number of phases the charger uses (1 or 3)
# --- EV charging efficiency & in-hour dosing ---
# Flat AC charging efficiency (0..1) at full charger power (e.g. 0.91 for a
# Tesla at 3×16 A). Converts between grid-side and pack-side energy in the
# allocator and the plan. 1.0 → charging treated as lossless.
# The charger always runs at full power; hours that need less than a full
# hour's worth of energy get a planned charging DURATION instead
# (``Decision.ev_charge_minutes``, exposed as a sensor) — the user's
# automation starts/stops the charger off it.
CONF_EV_CHARGE_EFFICIENCY: Final = "ev_charge_efficiency"
# Charger / charging telemetry. All optional; each has a concrete planning use.
CONF_EV_CHARGING_SENSOR: Final = "ev_charging_sensor"  # actively drawing (bool) → plan-vs-reality reminders
CONF_EV_ENERGY_ADDED_SENSOR: Final = "ev_energy_added_sensor"  # session kWh (total_increasing) → delivered so far
CONF_EV_ODOMETER_SENSOR: Final = "ev_odometer_sensor"  # total km (increasing) → learn kWh/km + drain profile
# Grid-side EV charging energy meter — when the charger draws through the house
# meter, its historical charging is in the learned consumption profile. PowerPilot
# plans EV charging explicitly, so this meter is *subtracted* from the demand
# forecast (no double counting); realized history is unaffected. Set only when the
# charger sits inside the main consumption meter.
CONF_EV_CHARGE_METER_SENSOR: Final = "ev_charge_meter_sensor"
# Legacy single-calendar key. Superseded by the integration-wide CONF_CALENDARS
# list (below); kept only so ``async_setup_entry`` can seed the new list once on
# upgrade — nothing reads it at runtime anymore.
CONF_EV_CALENDAR: Final = "ev_calendar"
CONF_EV_CALENDAR_KEYWORD: Final = "ev_calendar_keyword"  # event-summary trigger word (e.g. "Kotek")
# --- EV charging-hour placement preferences ---
# The allocator's baseline is pure cost: cheapest hours win even when that
# scatters charging (2 h on, 1 h gap, 2 h on). These options trade a bounded
# amount of money for nicer placement:
# * prefer contiguous — keep the charging hours in one unbroken block as long
#   as the block costs at most ``contiguous_max_extra_pct`` % more than the
#   scattered optimum; a bigger difference re-allows the gap.
# * prefer early — among placements within ``early_max_extra_pct`` % of the
#   cheapest, charge as much as possible as early as possible: first maximise
#   the energy taken on the earliest day, then on the next, and only then
#   finish the day as soon as possible. Earliness outranks contiguity —
#   anything that can be charged today should be, because today's cheap hours
#   are known while tomorrow's are still a forecast that can evaporate, and an
#   unused cheap hour never comes back.
CONF_EV_PREFER_CONTIGUOUS: Final = "ev_prefer_contiguous"
CONF_EV_CONTIGUOUS_MAX_EXTRA_PCT: Final = "ev_contiguous_max_extra_pct"
CONF_EV_PREFER_EARLY: Final = "ev_prefer_early"
CONF_EV_EARLY_MAX_EXTRA_PCT: Final = "ev_early_max_extra_pct"

# --- Calendars (integration-wide, not EV-specific) ---
# HA ``calendar.*`` entities read for planning (works with Google, CalDAV/
# iCloud, Local Calendar, …). Events feed two consumers:
# * EV keyword events ("Kotek 100%" = deadline target, bare "Kotek" = forced
#   window) — parsed by the EV module from *every* configured calendar.
# * Events with a ``location`` become trips: the car is away for the event plus
#   travel time (Google Maps) plus the margins below. Those hours are not
#   chargeable and the round trip drains the EV pack (learned kWh/km).
CONF_CALENDARS: Final = "calendars"
# Google Maps API key (Distance Matrix API). Without it trips carry no
# distance/travel-time model — unavailability covers only the event span (plus
# margins) and no drive energy is planned; a warning is logged instead of
# guessing. Resolved distances are cached per location string (see below).
CONF_GMAPS_API_KEY: Final = "gmaps_api_key"
# Extra unavailability margin (minutes) before departure and after the return —
# packing the car, parking, cable staying unplugged etc.
CONF_TRAVEL_MARGIN_BEFORE_MIN: Final = "travel_margin_before_min"
CONF_TRAVEL_MARGIN_AFTER_MIN: Final = "travel_margin_after_min"

# Storage version + lifetime of the per-entry travel (distance/duration) cache —
# one Google Maps call per unique location per TRAVEL_CACHE_DAYS.
STORAGE_VERSION_TRAVEL: Final = 1
TRAVEL_CACHE_DAYS: Final = 30
# Event locations (lower-cased, exact match) treated as "at home" — no trip.
HOME_LOCATION_MARKERS: Final = ("dom", "home")

# --- EV target SoC (own writable entity, not a config field) ---
# PowerPilot exposes its own ``number.*`` entity for the charge target instead
# of pointing at a car-provided sensor — most cars don't expose one in HA, and
# a writable helper is easier to adjust from the dashboard than the options
# flow. Calendar deadline targets / forced windows still override it.
NUMBER_EV_TARGET_SOC: Final = "ev_target_soc"
EV_TARGET_SOC_DEFAULT: Final = 80.0

# --- EV minimum SoC (own writable entity, not a config field) ---
# Safety reserve (%) the car should never be planned below: trip targets charge
# to at least ``min SoC + round-trip energy`` before departure, and routine
# top-ups keep this floor. Writable from the dashboard like the target SoC.
NUMBER_EV_MIN_SOC: Final = "ev_min_soc"
EV_MIN_SOC_DEFAULT: Final = 20.0

# --- EV battery capacity (learned, not configured) ---
# Capacity is derived from charging sessions: kWh added ÷ SoC gained × 100. The
# legacy CONF_EV_BATTERY_KWH (above) is kept only to seed the learned value on
# upgrade — there is no capacity field in the config flow anymore.
STORAGE_VERSION_EV: Final = 1
CAPACITY_LEARN_DAYS: Final = 30  # history window scanned for charging sessions
MIN_CAPACITY_SAMPLES: Final = 3  # sessions needed before a learned value is used
MAX_CAPACITY_SAMPLES: Final = 20  # rolling window of session samples kept
MIN_SESSION_KWH: Final = 1.0  # ignore tiny top-ups (noise)
MIN_SESSION_SOC: Final = 15.0  # need a meaningful SoC swing for a clean estimate

# --- EV driving consumption (learned) ---
# kWh/km from odometer deltas + SoC drops × capacity; a 7×24 drain profile (kWh
# out of the pack per hour) predicts routine driving so charging anticipates it.
DRAIN_LEARN_DAYS: Final = 30
MIN_TRIP_KM: Final = 2.0  # ignore sub-2 km noise when learning kWh/km
DRAIN_HORIZON_HOURS: Final = 24  # look-ahead window for predicted driving drain

# --- Distribution tariffs ---
# Stored in ``entry.options`` as a list of dicts (see ``models.Tariff.to_dict``).
# Snapshots of resolved per-hour distribution prices live in their own
# ``Store(version=STORAGE_VERSION_TARIFF_SNAPSHOTS, key=f"{DOMAIN}_{entry_id}_tariff_snapshots")``.
CONF_TARIFFS: Final = "tariffs"

# Storage version for the per-entry tariff snapshot store.
STORAGE_VERSION_TARIFF_SNAPSHOTS: Final = 1

# --- Optimizer snapshots ("Symulacje" tab) ---
# One columnar snapshot of the optimizer's inputs+plan is persisted per clock
# hour (a "vintage"), so past plans can be compared against each other and
# against realized actuals. Stored in its own
# ``Store(version=STORAGE_VERSION_SNAPSHOTS, key=f"{DOMAIN}_{entry_id}_snapshots")``.
STORAGE_VERSION_SNAPSHOTS: Final = 1

# Single-char codes keep the columnar snapshot records compact on disk.
MODE_CODE: Final = {"charge": "c", "discharge": "d", "passthrough": "p"}
MODE_CODE_INV: Final = {v: k for k, v in MODE_CODE.items()}
PTYPE_CODE: Final = {
    PRICE_TYPE_CERTAIN: "c",
    PRICE_TYPE_FORECAST: "f",
    PRICE_TYPE_ESTIMATED: "e",
}
PTYPE_CODE_INV: Final = {v: k for k, v in PTYPE_CODE.items()}

DEFAULTS: Final = {
    CONF_PHASES: 3,
    CONF_MAIN_FUSE_A: 32,
    CONF_GRID_VOLTAGE: 230,
    CONF_BATTERY_CAPACITY_KWH: 10.0,
    CONF_INVERTER_MAX_CHARGE_KW: 3.0,
    CONF_MIN_CHARGE_POWER_KW: 0.0,
    CONF_INVERTER_MAX_DISCHARGE_KW: 3.0,
    CONF_CHARGE_EFFICIENCY: 0.95,
    CONF_DISCHARGE_EFFICIENCY: 0.95,
    CONF_BATTERY_WEAR_COST: 0.10,
    CONF_MIN_SOC: 10,
    CONF_MAX_SOC: 100,
    CONF_PRICE_SOURCE: PRICE_SOURCE_SENSOR,
    CONF_PRICE_MARKUP: 0.0,
    CONF_PRICE_VAT: 1.0,
    CONF_EXCISE_KWH: 0.0,
    CONF_PRICE_ROUNDING: PRICE_ROUNDING_PER_BUCKET,
    CONF_PRICE_REFRESH_HOURS: 3,
    CONF_CONSUMPTION_LEARN_DAYS: 21,
    CONF_EV_ENABLED: False,
    CONF_EV_CHARGER_KW: 3.5,
    CONF_EV_CHARGER_PHASE: 1,
    CONF_EV_CHARGER_PHASES: 1,
    CONF_EV_CHARGE_EFFICIENCY: 1.0,
    CONF_EV_CALENDAR_KEYWORD: "Kotek",
    CONF_EV_PREFER_CONTIGUOUS: False,
    CONF_EV_CONTIGUOUS_MAX_EXTRA_PCT: 15.0,
    CONF_EV_PREFER_EARLY: False,
    CONF_EV_EARLY_MAX_EXTRA_PCT: 10.0,
    CONF_TRAVEL_MARGIN_BEFORE_MIN: 30,
    CONF_TRAVEL_MARGIN_AFTER_MIN: 30,
}

# ---------------------------------------------------------------------------
# Charge-curve SoC bands
# ---------------------------------------------------------------------------
# Each band is a half-open SoC interval ``[soc_from, soc_to)``. The exclusive
# upper edge is the user-facing inclusive top + 1 (e.g. the "11–30 %" band is
# stored as ``[11, 31)``) so the bands are contiguous and cover 0–100 % with no
# gaps for a continuous SoC reading. The config flow asks for one max charge
# power per band and assembles them into ``CONF_CHARGE_CURVE`` segments.
CHARGE_CURVE_BANDS: Final = (
    (0, 11),
    (11, 31),
    (31, 51),
    (51, 71),
    (71, 91),
    (91, 101),
)


def charge_curve_band_key(band: tuple[int, int]) -> str:
    """Transient config-flow field key for a band, e.g. ``charge_curve_kw_11_30``.

    The key embeds the inclusive label (``soc_to - 1``) so it matches what the
    user sees in the form. These keys are not persisted — they are assembled
    into the canonical ``CONF_CHARGE_CURVE`` segment list on save.
    """
    lo, hi = band
    return f"charge_curve_kw_{lo}_{hi - 1}"


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
# When the ongoing-or-next planned charge / passthrough block starts — the
# moment the ESS needs the grid (feed for a connect/disconnect automation).
SENSOR_ESS_CHARGE_START: Final = "ess_charge_start"
BINARY_EV_CHARGE: Final = "ev_charge"
# EV control surface (the integration advises; an automation does the steering).
SENSOR_EV_CHARGE_START: Final = "ev_charge_start"  # next planned charge start (timestamp)
SENSOR_EV_SOC_LIMIT: Final = "ev_soc_limit"  # target SoC the car should charge to (%)
SENSOR_EV_CHARGE_MINUTES: Final = "ev_charge_minutes"  # planned charging minutes within the active/next charge hour
BINARY_EV_CONNECT_CHARGER: Final = "ev_connect_charger"  # charging planned within 24 h
