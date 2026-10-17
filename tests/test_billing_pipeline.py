"""The price cap wired end-to-end: config → coordinator → plan → entities."""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    BINARY_TARCZA_PPE1_CAP_BINDS,
    CONF_PPE1_IMPORT_SENSORS,
    CONF_SOC_SENSOR,
    CONF_TARCZA_TERMS,
    DEFAULTS,
    DOMAIN,
    SENSOR_TARCZA_PPE1_AVERAGE,
    SENSOR_TARCZA_PPE1_DISCOUNT,
    SENSOR_TARCZA_PPE1_ENERGY,
    SENSOR_TARCZA_PPE1_FORECAST_AVERAGE,
)
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.billing import month_bounds


TERMS = {
    "id": "t",
    "name": "Tarcza Pstryk 2026",
    "valid_from": "2025-01-01",
    "valid_to": "2099-12-31",
    "limit_kwh": 0.61,
    "limit_includes_vat": True,
    "handling_fee_included": True,
}


async def _setup(hass: HomeAssistant, **config):
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc", **config},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry, hass.data[DOMAIN][entry.entry_id]


def _state(hass: HomeAssistant, entry, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{key}"
    )
    assert entity_id, f"{platform}.{key} not registered"
    return hass.states.get(entity_id)


async def test_cap_disabled_by_default_leaves_the_plan_without_billing(
    hass: HomeAssistant,
) -> None:
    entry, coordinator = await _setup(hass)
    assert coordinator.data.billing == []
    for key in (
        SENSOR_TARCZA_PPE1_ENERGY,
        SENSOR_TARCZA_PPE1_AVERAGE,
        SENSOR_TARCZA_PPE1_FORECAST_AVERAGE,
        SENSOR_TARCZA_PPE1_DISCOUNT,
    ):
        assert _state(hass, entry, "sensor", key).state == "unknown"
    assert _state(hass, entry, "binary_sensor", BINARY_TARCZA_PPE1_CAP_BINDS).state == "unknown"


async def test_enabled_without_meters_reports_why_instead_of_guessing(
    hass: HomeAssistant,
) -> None:
    entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [TERMS]})
    outcomes = coordinator.data.billing
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert not outcome.available
    assert "liczników" in outcome.unavailable_reason
    assert outcome.energy_kwh == 0.0
    # The battery was planned without the cap and the entities say so.
    state = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_FORECAST_AVERAGE)
    assert state.state == "unknown"
    assert "liczników" in state.attributes["unavailable_reason"]
    binds = _state(hass, entry, "binary_sensor", BINARY_TARCZA_PPE1_CAP_BINDS)
    assert binds.state == "unknown"
    status = coordinator.billing_status()
    assert status["enabled"] is True
    assert status["outcomes"][0]["available"] is False


async def test_enabled_with_meters_but_unreadable_recorder_reports_the_error(
    hass: HomeAssistant,
) -> None:
    """No recorder in this test harness → the meters cannot be read at all.

    That must surface as the reason (not as a zero-import month): the module
    records it, the plan carries it and the entities show it.
    """
    hass.states.async_set(
        "sensor.acin",
        "12.5",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    entry, coordinator = await _setup(
        hass,
        **{CONF_TARCZA_TERMS: [TERMS], CONF_PPE1_IMPORT_SENSORS: ["sensor.acin"]},
    )
    outcome = coordinator.data.billing[0]
    assert not outcome.available
    assert "błąd odczytu liczników" in outcome.unavailable_reason
    assert coordinator.billing.periods == {}
    assert coordinator.billing.last_error is not None
    energy = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_ENERGY)
    assert energy.state == "unknown"
    assert any("błąd odczytu" in r for r in energy.attributes["reasons"])


async def test_cap_outside_its_validity_is_inactive(hass: HomeAssistant) -> None:
    ended = {**TERMS, "valid_from": "2019-01-01", "valid_to": "2019-12-31"}
    entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [ended]})
    assert coordinator.data.billing == []
    state = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_ENERGY)
    assert state.state == "unknown"
    assert any("nieaktywna" in r for r in state.attributes["reasons"])


async def test_unusable_terms_reach_the_entities_instead_of_a_silent_no_cap(
    hass: HomeAssistant,
) -> None:
    partial = {**TERMS, "valid_from": "2025-01-15"}
    entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [partial]})
    # No month can be settled under broken terms, so no outcome is invented…
    assert coordinator.data.billing == []
    # …and the reason is on the entities and in the status, not swallowed.
    state = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_FORECAST_AVERAGE)
    assert any("pełnych miesięcy" in r for r in state.attributes["reasons"])
    assert "pełnych miesięcy" in coordinator.billing_status()["config_error"]


async def test_a_cap_starting_next_month_needs_nothing_observed_this_month(
    hass: HomeAssistant,
) -> None:
    """Late December with only the 2027 edition: January is still planned.

    The running month has no cap, so there is no month-to-date position to
    read — that must not block settling the next, capped, month.
    """
    now = dt_util.now()
    _, next_month = month_bounds(now)
    _, month_after = month_bounds(next_month)
    starts_next_month = {
        **TERMS,
        "valid_from": dt_util.as_local(next_month).date().isoformat(),
        "valid_to": (dt_util.as_local(month_after).date() - timedelta(days=1)).isoformat(),
    }
    _entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [starts_next_month]})
    assert coordinator.billing.periods == {}  # nothing to observe this month

    # A horizon from now to the end of the capped month (no tail needed).
    hour = now.replace(minute=0, second=0, microsecond=0)
    slots = []
    while hour < month_after:
        slots.append(HourSlot(start=hour, buy_price=0.5, cap_price_kwh=0.5))
        hour += timedelta(hours=1)
    periods, tail, reason = await coordinator._billing_inputs(Forecast(slots=slots), None)
    assert reason is None
    assert [p.month_start for p in periods] == [next_month]
    assert periods[0].energy_kwh == 0.0
    assert tail.slots == []
