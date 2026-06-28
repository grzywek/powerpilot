"""Unit tests for consumption demand contribution (double-count exclusion)."""

from __future__ import annotations

from datetime import datetime

from custom_components.powerpilot.const import (
    CONF_EV_CHARGE_METER_SENSOR,
)
from custom_components.powerpilot.modules.consumption import ConsumptionModule
from custom_components.powerpilot.profiles import WeeklyAccumulator

WASHER = "sensor.washer"
EV_METER = "sensor.ev_charger_energy"
HOUR = datetime(2026, 6, 15, 8)  # a Monday, 08:00


def _module(config: dict) -> ConsumptionModule:
    module = object.__new__(ConsumptionModule)
    module.config = config
    module.devices = {}
    for eid in (WASHER, EV_METER):
        acc = WeeklyAccumulator()
        acc.observe(HOUR, 1.0)  # 1 kWh at Mon 08:00 for both
        module.devices[eid] = acc
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
