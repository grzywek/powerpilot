"""Unit tests for consumption demand contribution (double-count exclusion)."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from custom_components.powerpilot.const import (
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    CONF_EV_CHARGE_METER_SENSOR,
    CONF_SENSOR_PARENTS,
)
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.consumption import ConsumptionModule
from custom_components.powerpilot.profiles import WeeklyAccumulator

WASHER = "sensor.washer"
EV_METER = "sensor.ev_charger_energy"
BOILER = "sensor.boiler"
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
