"""Unit tests for consumption demand contribution (double-count exclusion)."""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    CONF_APARTMENT_METER,
    CONF_APARTMENT_POWER_SENSOR,
    CONF_CLIMATE_SENSORS,
    CONF_CONSUMPTION_LEARN_DAYS,
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    CONF_EV_CHARGE_METER_SENSOR,
    CONF_SENSOR_PARENTS,
    CONF_SOC_SENSOR,
    CONF_VICTRON_OTHER_POWER_SENSORS,
    CONF_VICTRON_OUTPUT_POWER_SENSOR,
    DEFAULTS,
    DOMAIN,
    SUPPLY_HOUR_MIXED,
    SUPPLY_HOUR_UNKNOWN,
    SUPPLY_METER_2,
    SUPPLY_SOURCE_LIVE,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.models import Forecast, HourSlot, SupplyTransition
from custom_components.powerpilot.modules.consumption import ConsumptionModule
from custom_components.powerpilot.modules.supply import SupplyDetector, SupplyModule
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
    module.coordinator = SimpleNamespace(
        climate=_StubClimate(climate_handled),
        supply=SupplyModule(None, SimpleNamespace(config=config)),
    )
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


# ---------------------------------------------------------------------------
# Apartment supply (stage 2, slice 3)
# ---------------------------------------------------------------------------

APARTMENT = "sensor.apartment_energy"
SUPPLY_CFG = {
    CONF_CONSUMPTION_SENSOR: MAIN,
    CONF_DEVICE_SENSORS: [APARTMENT, WASHER, BOILER],
    CONF_SENSOR_PARENTS: {WASHER: APARTMENT},
    CONF_APARTMENT_METER: APARTMENT,
    CONF_APARTMENT_POWER_SENSOR: "sensor.apartment_power",
    CONF_VICTRON_OUTPUT_POWER_SENSOR: "sensor.victron_power",
    CONF_VICTRON_OTHER_POWER_SENSORS: ["sensor.garage_power"],
}


def _confirm(module: ConsumptionModule, state: str | None) -> SupplyModule:
    supply = module.coordinator.supply
    supply.transitions = (
        []
        if state is None
        else [
            SupplyTransition(
                state=state,
                at=datetime(2026, 6, 1, tzinfo=dt_util.UTC),
                not_before=None,
                apartment_w=1000.0,
                victron_net_w=1050.0,
                source=SUPPLY_SOURCE_LIVE,
            )
        ]
    )
    return supply


def test_the_apartment_subtree_is_forecast_apart_in_every_state() -> None:
    """Stage 3: the apartment, its washer included, goes to ``apartment_kwh``.

    In every state — the optimizer places it on the Victron or on meter 2 per
    the hour's supply, so the Victron demand never carries it here.
    """
    for state in (SUPPLY_METER_2, SUPPLY_VICTRON, None):
        module = _module(SUPPLY_CFG, {APARTMENT: 1.0, WASHER: 0.5, BOILER: 0.25})
        module.base = _acc(1.1, observed=True)
        _confirm(module, state)
        forecast = Forecast(slots=[HourSlot(start=HOUR)])

        module.contribute(forecast)

        slot = forecast.slots[0]
        assert slot.base_consumption_kwh == 1.1, state
        assert slot.extra_load_kwh == 0.25, state  # the boiler only
        assert slot.apartment_kwh == 1.5, state
        assert module.device_value(HOUR.weekday(), HOUR.hour) == 0.25, state


def test_unknown_supply_adds_a_plan_reminder() -> None:
    module = _module(SUPPLY_CFG)
    supply = _confirm(module, None)
    assert len(supply.collect_reminders()) == 1
    assert "mieszkanie" in supply.collect_reminders()[0]
    _confirm(module, SUPPLY_VICTRON)
    assert supply.collect_reminders() == []
    # Not configured at all: nothing to remind about.
    assert _module({CONF_CONSUMPTION_SENSOR: MAIN}).coordinator.supply.collect_reminders() == []


def test_meter_2_without_an_apartment_meter_detaches_nothing() -> None:
    config = {k: v for k, v in SUPPLY_CFG.items() if k != CONF_APARTMENT_METER}
    module = _module(config, {APARTMENT: 1.0, BOILER: 0.25})
    _confirm(module, SUPPLY_METER_2)
    assert module.coordinator.supply.apartment_devices() == set()
    assert _forecast(module, APARTMENT, SUPPLY_METER_2) == 1.0
    assert module.device_value(HOUR.weekday(), HOUR.hour) == 1.25
    module.base = _acc(1.1, observed=True)
    forecast = Forecast(slots=[HourSlot(start=HOUR)])
    module.contribute(forecast)
    assert forecast.slots[0].apartment_kwh == 0.0
    assert forecast.slots[0].extra_load_kwh == 1.25


def test_topology_fingerprint_includes_the_supply_configuration() -> None:
    plain = {k: v for k, v in SUPPLY_CFG.items() if k != CONF_APARTMENT_METER}
    without_detection = {
        k: v for k, v in SUPPLY_CFG.items() if k != CONF_VICTRON_OUTPUT_POWER_SENSOR
    }
    fingerprints = [
        _module(cfg)._current_topology() for cfg in (SUPPLY_CFG, plain, without_detection)
    ]
    assert len({repr(f) for f in fingerprints}) == 3


def _learner(config: dict, series: dict, states: dict | None = None):
    """A consumption module learning yesterday from ``series`` (hour index → kWh)."""
    module = _module({**config, CONF_CONSUMPTION_LEARN_DAYS: 1}, {})
    module.base = WeeklyAccumulator()
    module._store = None
    module._last_learn_day = None
    module._topology = None
    logs: list[tuple[str, str]] = []
    module.coordinator.log_info = lambda domain, msg, extra=None: logs.append(("info", msg))
    module.coordinator.log_warning = lambda domain, msg, extra=None: logs.append(("warn", msg))
    supply = module.coordinator.supply
    supply.detector = SupplyDetector(300.0, 100.0, 3)
    day = dt_util.start_of_local_day(dt_util.now().date() - timedelta(days=1))
    hours = [day + timedelta(hours=h) for h in range(24)]
    fetched: list[str] = []

    async def fetch(entity_id, start, end):
        fetched.append(entity_id)
        return {hours[i]: v for i, v in series.get(entity_id, {}).items()}

    async def past_states(start, end):
        return {h: (states or {}).get(i, SUPPLY_VICTRON) for i, h in enumerate(hours)}

    module._fetch_hourly_kwh = fetch
    supply.async_past_hour_states = past_states
    return module, hours, logs, fetched


ON_PPE_2 = range(10, 16)


def _supply_series(ppe_2: range = ON_PPE_2) -> dict:
    """Victron 3.25 kWh/h = base 1.0 + apartment 2.0 + boiler 0.25; 1.25 on PPE 2."""
    return {
        MAIN: {h: (1.25 if h in ppe_2 else 3.25) for h in range(24)},
        APARTMENT: {h: 2.0 for h in range(24)},
        WASHER: {h: 0.5 for h in range(24)},
        BOILER: {h: 0.25 for h in range(24)},
    }


async def test_learning_on_meter_2_keeps_the_apartment_out_of_the_base() -> None:
    states = {h: SUPPLY_METER_2 for h in ON_PPE_2}
    states[9] = SUPPLY_HOUR_MIXED
    module, hours, logs, _ = _learner(SUPPLY_CFG, _supply_series(), states)

    await module._maybe_learn()

    wd = hours[0].weekday()
    assert module.base.observed_days == 1
    # Base = Victron − apartment − boiler on the Victron, Victron − boiler on PPE 2.
    for h in (0, 8, 10, 15, 16, 23):
        assert module.base.value(wd, h) == 1.0, h
    assert module.base.value(wd, 9) is None  # mixed: no base sample
    # The apartment and its washer learn in every hour, the mixed one too.
    for h in (9, 10, 20):
        assert module.devices[APARTMENT].value(wd, h) == 1.5
        assert module.devices[WASHER].value(wd, h) == 0.5
    assert any("1 godz." in msg and "zasilan" in msg for _, msg in logs)


async def test_a_device_hour_without_a_reading_is_not_learned_as_zero() -> None:
    series = _supply_series(range(0))
    del series[BOILER][5]
    module, hours, logs, _ = _learner(SUPPLY_CFG, series)

    await module._maybe_learn()

    wd = hours[0].weekday()
    assert module.devices[BOILER].value(wd, 5) is None
    assert module.base.value(wd, 5) is None  # its parent's hour is unknown
    assert module.base.observed_days == 1
    assert any(level == "warn" and MAIN.split(".")[-1] in msg for level, msg in logs)


async def test_learning_waits_for_the_supply_history() -> None:
    module, _, _, fetched = _learner(SUPPLY_CFG, _supply_series(range(0)))
    module.coordinator.supply.detector = None  # the replay is still running

    await module._maybe_learn()
    assert fetched == []
    assert module._last_learn_day is None

    module.coordinator.supply.detector = SupplyDetector(300.0, 100.0, 3)
    await module._maybe_learn()
    assert module.base.observed_days == 1


async def test_turning_supply_detection_on_relearns_the_profiles() -> None:
    plain = {k: v for k, v in SUPPLY_CFG.items() if k != CONF_APARTMENT_METER}
    module, _, logs, _ = _learner(plain, _supply_series(range(0)))
    await module._maybe_learn()
    learned_under = module._topology
    assert module.base.observed_days == 1

    module.config[CONF_APARTMENT_METER] = APARTMENT
    module._last_learn_day = dt_util.now().date()  # already learned today
    await module._maybe_learn()

    assert module._topology != learned_under
    assert any("od nowa" in msg for _, msg in logs)
    assert module.base.observed_days == 1  # relearned at once, not tomorrow


def _forecast(module: ConsumptionModule, entity_id: str, supply: str | None) -> float | None:
    """The chart's per-device forecast, as the coordinator computes it."""
    from custom_components.powerpilot.coordinator import PowerPilotCoordinator

    stub = SimpleNamespace(
        climate=module.coordinator.climate,
        consumption=module,
        supply=module.coordinator.supply,
    )
    return PowerPilotCoordinator._device_forecast_kwh(stub, entity_id, HOUR, supply)


def test_chart_forecast_leaves_out_the_subtree_in_a_meter_2_hour() -> None:
    """The hour's supply decides, not the actual state (stage 3, slice 4).

    A planned meter-2 hour while the apartment is still on the Victron leaves
    the subtree out, a planned Victron hour while it is on meter 2 keeps it —
    the device bars then match the Victron demand the plan carries. Mixed and
    unknown hours keep it, as ``HourSlot.victron_consumption_kwh`` does.
    """
    module = _module(SUPPLY_CFG, {APARTMENT: 1.0, WASHER: 0.5, BOILER: 0.25})
    for actual in (SUPPLY_VICTRON, SUPPLY_METER_2):
        _confirm(module, actual)
        # The apartment and the washer inside it are not on the Victron; the boiler is.
        assert _forecast(module, APARTMENT, SUPPLY_METER_2) is None, actual
        assert _forecast(module, WASHER, SUPPLY_METER_2) is None, actual
        assert _forecast(module, BOILER, SUPPLY_METER_2) == 0.25, actual
        for supply in (SUPPLY_VICTRON, SUPPLY_HOUR_MIXED, SUPPLY_HOUR_UNKNOWN, None):
            assert _forecast(module, APARTMENT, supply) == 1.0, (actual, supply)
            assert _forecast(module, WASHER, supply) == 0.5, (actual, supply)


def test_supply_status_lists_the_log_tail_and_the_configuration() -> None:
    module = _module({**DEFAULTS, **SUPPLY_CFG})
    supply = _confirm(module, SUPPLY_METER_2)
    status = supply.status()
    assert status["enabled"] and status["active"]
    assert status["state"] == SUPPLY_METER_2
    assert status["transitions_total"] == 1
    assert status["transitions"] == [supply.transitions[0].to_dict()]
    assert status["checkpoint"] is None
    assert status["apartment_meter"] == APARTMENT
    assert status["other_sensors"] == ["sensor.garage_power"]
    assert (status["min_apartment_w"], status["victron_share"]) == (300.0, 0.5)
    # Many transitions: only the last 50 are listed, the total is exact.
    first = supply.transitions[0]
    supply.transitions = [first] * 60
    status = supply.status()
    assert status["transitions_total"] == 60 and len(status["transitions"]) == 50


def test_learning_run_records_its_dropped_hour_counts() -> None:
    from custom_components.powerpilot.hierarchy import ExclusiveSeries

    module = _module(SUPPLY_CFG)
    module.log_info = lambda message, extra=None: None
    module.log_warning = lambda message, extra=None: None
    module.learning_dropped = None  # what __init__ sets before the first run
    module._log_dropped_hours(ExclusiveSeries(series={}), 30)
    assert module.learning_dropped == {
        "window_days": 30,
        "supply_dropped": 0,
        "missing_child": {},
        "negative": {},
    }
    module._log_dropped_hours(
        ExclusiveSeries(series={}, missing_child={WASHER: 2}, supply_dropped=5), 30
    )
    assert module.learning_dropped["supply_dropped"] == 5
    assert module.learning_dropped["missing_child"] == {WASHER: 2}
