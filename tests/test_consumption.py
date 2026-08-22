"""Unit tests for consumption demand contribution (double-count exclusion)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    CONF_CLIMATE_SENSORS,
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    CONF_EV_CHARGE_METER_SENSOR,
    CONF_SENSOR_PARENTS,
    CONF_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
)
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.consumption import ConsumptionModule
from custom_components.powerpilot.profiles import WeeklyAccumulator

WASHER = "sensor.washer"
EV_METER = "sensor.ev_charger_energy"
BOILER = "sensor.boiler"
MAIN = "sensor.main"
AC = "sensor.salon_klimka_energy"
HOUR = datetime(2026, 6, 15, 8)  # a Monday, 08:00


def _acc(value: float, observed: bool = False) -> WeeklyAccumulator:
    acc = WeeklyAccumulator()
    acc.observe(HOUR, value)
    if observed:
        acc.mark_date_observed(HOUR.date())
    return acc


class _StubClimate:
    """Climate stand-in: owns (at most) one device's demand forecast."""

    def __init__(self, handled: str | None = None) -> None:
        self._handled = handled

    def handles(self, entity_id: str) -> bool:
        return entity_id == self._handled


def _module(
    config: dict,
    devices: dict[str, float] | None = None,
    climate_handled: str | None = None,
) -> ConsumptionModule:
    module = object.__new__(ConsumptionModule)
    module.config = config
    module.coordinator = SimpleNamespace(climate=_StubClimate(climate_handled))
    module.devices = {}
    for eid, value in (devices or {WASHER: 1.0, EV_METER: 1.0}).items():
        module.devices[eid] = _acc(value)
    return module


def test_device_value_excludes_ev_charge_meter() -> None:
    wd, hr = HOUR.weekday(), HOUR.hour
    # Without designating an EV meter, both devices contribute → 2.0 kWh.
    plain = _module({})
    assert plain.device_value(wd, hr) == 2.0

    # With the EV meter designated, its energy is dropped from demand → 1.0 kWh
    # (the washer only). The EV meter is still learned (in self.devices) so it
    # can be subtracted from the base, but it is not added back to demand.
    excluded = _module({CONF_EV_CHARGE_METER_SENSOR: EV_METER})
    assert excluded.device_value(wd, hr) == 1.0
    assert EV_METER in excluded.devices  # still learned, just not contributed


def test_device_value_excludes_climate_handled_device() -> None:
    wd, hr = HOUR.weekday(), HOUR.hour
    # Once the climate module's temperature model owns the AC meter, its weekly
    # profile stops contributing (the climate module forecasts it instead).
    module = _module({}, {WASHER: 1.0, BOILER: 0.5}, climate_handled=BOILER)
    assert module.device_value(wd, hr) == 1.0


def test_contribute_keeps_base_exclusive_and_devices_in_total_demand() -> None:
    module = _module({}, {WASHER: 0.5, BOILER: 0.25})
    module.base = _acc(1.1, observed=True)
    forecast = Forecast(slots=[HourSlot(start=HOUR)])

    module.contribute(forecast)

    slot = forecast.slots[0]
    assert slot.base_consumption_kwh == 1.1
    assert slot.extra_load_kwh == 0.75
    assert slot.total_consumption_kwh == 1.85


def test_topology_fingerprint_detects_hierarchy_changes() -> None:
    base_cfg = {CONF_CONSUMPTION_SENSOR: "sensor.main"}
    # Device order does not matter — same tree, same fingerprint.
    a = _module({**base_cfg, CONF_DEVICE_SENSORS: [WASHER, BOILER]})
    b = _module({**base_cfg, CONF_DEVICE_SENSORS: [BOILER, WASHER]})
    assert a._current_topology() == b._current_topology()

    # Adding a device or re-parenting one changes the fingerprint → the
    # learned profile is reset and relearned under the new tree.
    added = _module({**base_cfg, CONF_DEVICE_SENSORS: [WASHER, BOILER, EV_METER]})
    assert added._current_topology() != a._current_topology()
    reparented = _module(
        {
            **base_cfg,
            CONF_DEVICE_SENSORS: [WASHER, BOILER],
            CONF_SENSOR_PARENTS: {WASHER: BOILER},
        }
    )
    assert reparented._current_topology() != a._current_topology()

    # Designating the EV charge meter folds it into the learned tree too.
    ev = _module({**base_cfg, CONF_DEVICE_SENSORS: [WASHER, BOILER], CONF_EV_CHARGE_METER_SENSOR: EV_METER})
    assert ev._current_topology() == added._current_topology()


async def test_profiles_break_out_a_climate_only_meter(hass: HomeAssistant) -> None:
    """A meter picked only as weather-dependent still gets its own profile.

    Regression: the AC was configured under ``climate_sensors`` alone, so it was
    missing from the panel's profile list *and* its energy stayed inside the
    base load — which the climate module's temperature forecast would then add
    on top of once its model took over.
    """
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    for eid in (MAIN, AC):
        hass.states.async_set(
            eid, "1", {"unit_of_measurement": "kWh", "state_class": "total_increasing"}
        )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **DEFAULTS,
            CONF_SOC_SENSOR: "sensor.soc",
            CONF_CONSUMPTION_SENSOR: MAIN,
            CONF_CLIMATE_SENSORS: [AC],
        },
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hour = dt_util.start_of_local_day(dt_util.now()) - timedelta(hours=6)

    async def _range(entity_id: str, start, end) -> dict:
        return {hour: 1.5 if entity_id == AC else 4.0}

    coordinator.consumption.async_range_kwh = _range

    stats = await coordinator.async_consumption_stats(days=3)
    profiles = {p["key"]: p for p in stats["profiles"]}

    assert AC in profiles, "the weather-dependent meter has no profile of its own"
    assert profiles[AC]["icon"] == "mdi:air-conditioner"
    # Its energy is broken out of the base, exactly like a device sensor.
    assert profiles[AC]["daily"][-2]["kwh"] == 1.5
    assert profiles["__base__"]["daily"][-2]["kwh"] == 2.5
