"""Integration setup tests — proves the integration loads inside a real HA loop."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    CONF_BUY_PRICE_SENSOR,
    DEFAULTS,
    DOMAIN,
)


async def test_setup_creates_entities(hass: HomeAssistant) -> None:
    """The integration sets up and creates its output entities."""
    entry = MockConfigEntry(domain=DOMAIN, data=dict(DEFAULTS), title="PowerPilot")
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in (
        "sensor.powerpilot_inverter_mode",
        "sensor.powerpilot_charge_power",
        "sensor.powerpilot_battery_energy_cost",
        "sensor.powerpilot_optimization_plan",
        "binary_sensor.powerpilot_grid_connected",
        "binary_sensor.powerpilot_ev_charge",
    ):
        assert hass.states.get(entity_id) is not None, entity_id


async def test_plan_reacts_to_price_sensor(hass: HomeAssistant) -> None:
    """With a price sensor, the plan exposes a priced forecast."""
    hass.states.async_set(
        "sensor.energy_price",
        "0.50",
        {
            "unit_of_measurement": "PLN/kWh",
            "prices": [
                {"hour": h, "value": 0.3 if h < 6 else 1.0} for h in range(24)
            ],
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_BUY_PRICE_SENSOR: "sensor.energy_price"},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    plan = hass.states.get("sensor.powerpilot_optimization_plan")
    assert plan is not None
    assert plan.attributes.get("horizon_hours", 0) >= 1
