"""WebSocket API for the PowerPilot panel.

Exposes the full plan, feature status and a runtime event log to the custom
frontend panel, instead of overloading entity attributes.
"""

from __future__ import annotations

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN


def _coordinator(hass: HomeAssistant):
    from .coordinator import PowerPilotCoordinator

    for value in hass.data.get(DOMAIN, {}).values():
        if isinstance(value, PowerPilotCoordinator):
            return value
    return None


@websocket_api.websocket_command({vol.Required("type"): "powerpilot/plan"})
@callback
def ws_plan(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    plan = coordinator.data if coordinator else None
    connection.send_result(msg["id"], plan.as_dict() if plan else {})


@websocket_api.websocket_command({vol.Required("type"): "powerpilot/status"})
@callback
def ws_status(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    connection.send_result(msg["id"], coordinator.get_status() if coordinator else {})


@websocket_api.websocket_command({vol.Required("type"): "powerpilot/log"})
@callback
def ws_log(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    connection.send_result(
        msg["id"], {"events": coordinator.get_log() if coordinator else []}
    )


@websocket_api.websocket_command({vol.Required("type"): "powerpilot/profiles"})
@callback
def ws_profiles(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    connection.send_result(msg["id"], coordinator.get_profiles() if coordinator else {})


@websocket_api.websocket_command(
    {
        vol.Required("type"): "powerpilot/series",
        vol.Optional("past_hours", default=24): int,
        vol.Optional("start"): str,
        vol.Optional("end"): str,
        vol.Optional("forecast_lead", default=0): int,
        vol.Optional("forecast_run_at"): str,
    }
)
@websocket_api.async_response
async def ws_series(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    if not coordinator:
        connection.send_result(msg["id"], {"hours": []})
        return
    result = await coordinator.get_series(
        past_hours=int(msg.get("past_hours", 24)),
        start=msg.get("start"),
        end=msg.get("end"),
        forecast_lead=int(msg.get("forecast_lead", 0)),
        forecast_run_at=msg.get("forecast_run_at"),
    )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {vol.Required("type"): "powerpilot/prices", vol.Optional("date"): str}
)
@callback
def ws_prices(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    if not coordinator:
        connection.send_result(msg["id"], {"hours": []})
        return
    connection.send_result(msg["id"], coordinator.get_price_archive(msg.get("date")))


@websocket_api.websocket_command(
    {
        vol.Required("type"): "powerpilot/accuracy",
        vol.Optional("lead_hours", default=24): int,
        vol.Optional("days", default=7): int,
    }
)
@websocket_api.async_response
async def ws_accuracy(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    if not coordinator:
        connection.send_result(msg["id"], {"hours": []})
        return
    result = await coordinator.get_accuracy(
        lead_hours=int(msg.get("lead_hours", 24)),
        days=int(msg.get("days", 7)),
    )
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "powerpilot/flow"})
@callback
def ws_flow(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    connection.send_result(msg["id"], coordinator.get_flow() if coordinator else {})


@websocket_api.websocket_command(
    {vol.Required("type"): "powerpilot/efficiency", vol.Optional("days"): int}
)
@websocket_api.async_response
async def ws_efficiency(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    if not coordinator:
        connection.send_result(msg["id"], {})
        return
    result = await coordinator.async_charging_efficiency(days=int(msg.get("days", 30)))
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "powerpilot/debug",
        # Bound the dump to the next N hours (token-lean paste); omit for full.
        vol.Optional("hours"): vol.All(int, vol.Range(min=1, max=168)),
    }
)
@websocket_api.async_response
async def ws_debug(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    if not coordinator:
        connection.send_result(msg["id"], {})
        return
    result = await coordinator.get_debug(hours=msg.get("hours"))
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command({vol.Required("type"): "powerpilot/diagnostics"})
@websocket_api.async_response
async def ws_diagnostics(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    if not coordinator:
        connection.send_result(msg["id"], {})
        return
    result = await coordinator.get_diagnostics()
    connection.send_result(msg["id"], result)


@websocket_api.websocket_command(
    {vol.Required("type"): "powerpilot/consumption_stats", vol.Optional("days"): int}
)
@websocket_api.async_response
async def ws_consumption_stats(hass: HomeAssistant, connection, msg) -> None:
    coordinator = _coordinator(hass)
    if not coordinator:
        connection.send_result(msg["id"], {})
        return
    result = await coordinator.async_consumption_stats(msg.get("days", 63))
    connection.send_result(msg["id"], result)


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_plan)
    websocket_api.async_register_command(hass, ws_status)
    websocket_api.async_register_command(hass, ws_log)
    websocket_api.async_register_command(hass, ws_profiles)
    websocket_api.async_register_command(hass, ws_series)
    websocket_api.async_register_command(hass, ws_prices)
    websocket_api.async_register_command(hass, ws_accuracy)
    websocket_api.async_register_command(hass, ws_flow)
    websocket_api.async_register_command(hass, ws_efficiency)
    websocket_api.async_register_command(hass, ws_debug)
    websocket_api.async_register_command(hass, ws_diagnostics)
    websocket_api.async_register_command(hass, ws_consumption_stats)
