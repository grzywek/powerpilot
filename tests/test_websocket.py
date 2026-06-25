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
    assert "consumption" in msg["result"]

    await client.send_json({"id": 5, "type": "powerpilot/forecasts"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "horizons" in msg["result"]
    assert "date" in msg["result"]

    await client.send_json({"id": 6, "type": "powerpilot/series", "past_hours": 12})
    msg = await client.receive_json()
    assert msg["success"]
    assert "hours" in msg["result"]
    assert "now" in msg["result"]
    # Should contain both past hours and the forecast horizon.
    assert any(h["is_past"] for h in msg["result"]["hours"])


async def test_ws_prices_archive(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "powerpilot/prices"})
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert "date" in result
    # A full day is always 24 hourly rows with the archive column shape.
    assert len(result["hours"]) == 24
    row = result["hours"][0]
    for key in (
        "start",
        "type",
        "source",
        "fetched_at",
        "energy_price_kwh",
        "distribution_price_kwh",
        "total_price_kwh",
        "estimate_breakdown",
    ):
        assert key in row

    # An explicit (future) date is accepted and echoed back.
    await client.send_json(
        {"id": 2, "type": "powerpilot/prices", "date": "2030-01-01"}
    )
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["date"] == "2030-01-01"
    assert len(msg["result"]["hours"]) == 24


async def test_ws_snapshots_and_accuracy(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    # The first optimization run records one vintage.
    await client.send_json({"id": 1, "type": "powerpilot/snapshots"})
    msg = await client.receive_json()
    assert msg["success"]
    assert "runs" in msg["result"]
    assert len(msg["result"]["runs"]) >= 1
    run_at = msg["result"]["runs"][0]["run_at"]

    await client.send_json(
        {"id": 2, "type": "powerpilot/snapshot", "run_at": run_at}
    )
    msg = await client.receive_json()
    assert msg["success"]
    assert msg["result"]["run_at"]
    assert "hours" in msg["result"]
    if msg["result"]["hours"]:
        for key in ("start", "buy_price", "consumption_forecast", "inverter_mode"):
            assert key in msg["result"]["hours"][0]

    await client.send_json(
        {"id": 3, "type": "powerpilot/accuracy", "lead_hours": 24, "days": 3}
    )
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert result["lead_hours"] == 24
    assert len(result["bias_by_hour"]) == 24
    assert "mae" in result
    assert "bias" in result
    assert "hours" in result


async def test_ws_diagnostics(hass: HomeAssistant, hass_ws_client) -> None:
    await _setup(hass)
    client = await hass_ws_client(hass)

    await client.send_json({"id": 1, "type": "powerpilot/diagnostics"})
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert "ready" in result
    assert set(result["summary"]) >= {"ok", "warn", "error", "skip"}
    assert result["groups"], "diagnostics must report at least one group"
    # Every item carries a status verdict and a human message.
    for group in result["groups"]:
        for item in group["items"]:
            assert item["status"] in ("ok", "warn", "error", "skip")
            assert item["message"]
