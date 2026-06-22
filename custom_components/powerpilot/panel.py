"""Custom sidebar panel registration for PowerPilot.

Serves the bundled Lit frontend and registers it as a sidebar menu item. Static
path + WebSocket registration happen once globally; the panel itself is added on
setup and removed on unload.
"""

from __future__ import annotations

import os

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import DOMAIN
from .websocket_api import async_register_ws

PANEL_URL_PATH = "powerpilot"
PANEL_JS_URL = "/powerpilot_static/powerpilot-panel.js"
_GLOBAL = f"{DOMAIN}_frontend_global"


async def async_register_panel(hass: HomeAssistant) -> None:
    """Register static assets, WS commands (once) and the sidebar panel."""
    flags = hass.data.setdefault(_GLOBAL, {"static": False, "ws": False})

    if not flags["static"]:
        js_path = os.path.join(os.path.dirname(__file__), "frontend", "powerpilot-panel.js")
        if os.path.exists(js_path):
            await hass.http.async_register_static_paths(
                [StaticPathConfig(PANEL_JS_URL, js_path, False)]
            )
            flags["static"] = True

    if not flags["ws"]:
        async_register_ws(hass)
        flags["ws"] = True

    # The sidebar panel needs the frontend component (always present in a normal
    # HA install via default_config; absent in minimal test environments).
    if "frontend" not in hass.config.components:
        return

    if PANEL_URL_PATH not in hass.data.get("frontend_panels", {}):
        frontend.async_register_built_in_panel(
            hass,
            component_name="custom",
            sidebar_title="PowerPilot",
            sidebar_icon="mdi:home-battery",
            frontend_url_path=PANEL_URL_PATH,
            require_admin=False,
            config={
                "_panel_custom": {
                    "name": "powerpilot-panel",
                    "module_url": PANEL_JS_URL,
                    "embed_iframe": False,
                    "trust_external": False,
                }
            },
        )


def async_unregister_panel(hass: HomeAssistant) -> None:
    if PANEL_URL_PATH in hass.data.get("frontend_panels", {}):
        frontend.async_remove_panel(hass, PANEL_URL_PATH)
