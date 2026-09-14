"""Plan execution watchdog: planned mode vs the battery's measured flows."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    BINARY_PLAN_NOT_EXECUTED,
    CONF_BATTERY_CHARGE_SENSOR,
    CONF_BATTERY_DISCHARGE_SENSOR,
    CONF_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
    InverterMode,
)
from custom_components.powerpilot.execution import (
    EXECUTION_WINDOW,
    ExecutionWatch,
    assess_execution,
)
from custom_components.powerpilot.models import Decision

C, P, D = InverterMode.CHARGE, InverterMode.PASSTHROUGH, InverterMode.DISCHARGE


def test_draining_while_the_plan_needs_the_grid_is_an_alarm() -> None:
    for mode in (C, P):
        verdict = assess_execution(mode, 2.5, discharged_kwh=0.2, charged_kwh=0.0)
        assert verdict is not None and not verdict.ok
        assert "ESS oddał 0.20 kWh" in verdict.reason


def test_planned_discharge_is_never_an_alarm() -> None:
    assert assess_execution(D, 0.0, discharged_kwh=1.0, charged_kwh=0.0).ok


def test_sensor_noise_in_passthrough_is_fine() -> None:
    assert assess_execution(P, 0.0, discharged_kwh=0.02, charged_kwh=0.0).ok


def test_missing_charge_is_an_alarm_real_charge_is_not() -> None:
    # 3 kW over the ~15 measured minutes ≈ 0.75 kWh expected.
    missing = assess_execution(C, 3.0, discharged_kwh=0.0, charged_kwh=0.05)
    assert missing is not None and not missing.ok
    assert "ładowanie nie działa" in missing.reason
    assert assess_execution(C, 3.0, discharged_kwh=0.0, charged_kwh=0.6).ok


def test_unmeasurable_discharge_is_unknown_not_ok() -> None:
    assert assess_execution(P, 0.0, discharged_kwh=None, charged_kwh=None) is None


def test_without_charge_counter_draining_is_still_caught() -> None:
    assert assess_execution(C, 3.0, discharged_kwh=0.0, charged_kwh=None).ok
    assert not assess_execution(C, 3.0, discharged_kwh=0.3, charged_kwh=None).ok


def test_watch_waits_a_full_window_and_restarts_on_mode_change() -> None:
    watch = ExecutionWatch()
    t0 = dt_util.now()
    assert watch.observe(t0, C) is None
    assert watch.observe(t0 + EXECUTION_WINDOW - timedelta(minutes=1), C) is None
    at = t0 + EXECUTION_WINDOW
    assert watch.observe(at, C) == at - EXECUTION_WINDOW
    # The plan switched: the automation gets a fresh window for the new mode.
    assert watch.observe(at + timedelta(minutes=5), P) is None
    assert watch.observe(at + timedelta(minutes=10), D) is None


async def test_alarm_entity_turns_on_when_pack_drains_against_plan(
    hass: HomeAssistant, monkeypatch
) -> None:
    hass.states.async_set("sensor.soc", "40", {"unit_of_measurement": "%"})
    for entity_id in ("sensor.bat_in", "sensor.bat_out"):
        hass.states.async_set(entity_id, "100", {"unit_of_measurement": "kWh"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **DEFAULTS,
            CONF_SOC_SENSOR: "sensor.soc",
            CONF_BATTERY_CHARGE_SENSOR: "sensor.bat_in",
            CONF_BATTERY_DISCHARGE_SENSOR: "sensor.bat_out",
        },
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]

    now = dt_util.now()
    decision = Decision(start=now.replace(minute=0, second=0, microsecond=0))
    decision.inverter_mode = C
    decision.charge_power_kw = 3.0
    monkeypatch.setattr(coordinator, "current_decision", lambda *a, **k: decision)
    flows = {"sensor.bat_out": 0.4, "sensor.bat_in": 0.0}

    async def _partial(entity_id, start, end):
        return flows[entity_id]

    monkeypatch.setattr(coordinator.consumption, "async_partial_kwh", _partial)
    # The charge mode has already held for a whole window.
    coordinator._execution_watch.observe(now - EXECUTION_WINDOW - timedelta(minutes=1), C)

    entity_id = er.async_get(hass).async_get_entity_id(
        "binary_sensor", DOMAIN, f"{entry.entry_id}_{BINARY_PLAN_NOT_EXECUTED}"
    )
    assert entity_id is not None

    await coordinator._async_check_execution()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id)
    assert state.state == "on"
    assert "ESS oddał 0.40 kWh" in state.attributes["reason"]

    # The automation catches up: the pack charges as planned → alarm clears.
    flows.update({"sensor.bat_out": 0.0, "sensor.bat_in": 0.7})
    await coordinator._async_check_execution()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "off"
