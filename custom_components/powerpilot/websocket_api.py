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


@callback
def async_register_ws(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, ws_plan)
    websocket_api.async_register_command(hass, ws_status)
    websocket_api.async_register_command(hass, ws_log)
