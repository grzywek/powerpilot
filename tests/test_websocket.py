"""WebSocket API tests — proves the panel's data endpoints work."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import DEFAULTS, DOMAIN


async def _setup(hass: HomeAssistant) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data=dict(DEFAULTS), title="PowerPilot")
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()


async def test_ws_plan_status_log(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "powerpilot/status"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "checks" in msg["result"]
    assert "modules" in msg["result"]

    await client.send_json({"id": 2, "type": "powerpilot/plan"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "hours" in msg["result"]
    assert "forecast" in msg["result"]

    await client.send_json({"id": 3, "type": "powerpilot/log"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "events" in msg["result"]
    # The first optimization run should have recorded an event.
    assert len(msg["result"]["events"]) >= 1

    await client.send_json({"id": 4, "type": "powerpilot/profiles"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "price" in msg["result"]
    assert "consumption" in msg["result"]

    await client.send_json({"id": 5, "type": "powerpilot/forecasts"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "horizons" in msg["result"]
    assert "date" in msg["result"]
