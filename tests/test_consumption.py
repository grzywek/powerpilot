"""Unit tests for consumption demand contribution (double-count exclusion)."""

from __future__ import annotations

from datetime import datetime

from custom_components.powerpilot.const import (
    CONF_EV_CHARGE_METER_SENSOR,
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


def _module(config: dict, devices: dict[str, float] | None = None) -> ConsumptionModule:
    module = object.__new__(ConsumptionModule)
    module.config = config
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


def test_contribute_keeps_base_exclusive_and_devices_in_total_demand() -> None:
    module = _module({}, {WASHER: 0.5, BOILER: 0.25})
    module.base = _acc(1.1, observed=True)
    forecast = Forecast(slots=[HourSlot(start=HOUR)])

    module.contribute(forecast)

    slot = forecast.slots[0]
    assert slot.base_consumption_kwh == 1.1
    assert slot.extra_load_kwh == 0.75
    assert slot.total_consumption_kwh == 1.85
