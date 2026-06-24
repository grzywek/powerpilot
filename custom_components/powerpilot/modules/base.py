"""Module contract and registry for PowerPilot.

A module is an independent provider that contributes one slice of information to
the shared :class:`Forecast`. Modules never read each other directly, which keeps
them decoupled and independently testable.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from ..models import Forecast

if TYPE_CHECKING:
    from ..coordinator import PowerPilotCoordinator

_LOGGER = logging.getLogger(__name__)


class PowerPilotModule:
    """Base class for all PowerPilot modules."""

    #: Unique domain identifier for the module (e.g. ``"prices"``).
    domain: str = "base"

    def __init__(self, hass: HomeAssistant, coordinator: "PowerPilotCoordinator") -> None:
        self.hass = hass
        self.coordinator = coordinator
        self.config = coordinator.config
        self.last_error: str | None = None

    async def async_setup(self) -> None:
        """One-time setup (subscribe to entities, load history, etc.)."""

    async def async_update(self) -> None:
        """Refresh internal state before a planning run."""

    async def async_clear_data(self) -> None:
        """Wipe persisted data/cache for this module, keeping configuration.

        Modules that own a :class:`Store` override this to delete the store
        file (which also cancels any pending delayed save) and reset their
        in-memory state to a clean slate.
        """

    def contribute(self, forecast: Forecast) -> None:
        """Add this module's information to the forecast slots.

        Implementations must only *add* to slots, never overwrite another
        module's contribution.
        """

    def collect_reminders(self) -> list[str]:
        """Optional immediate reminders surfaced to the user."""
        return []

    # Convenience: structured log entries surfaced in the panel "Log" tab.
    def log_info(self, message: str, extra: dict | None = None) -> None:
        self.coordinator.log_info(self.domain, message, extra)

    def log_warning(self, message: str, extra: dict | None = None) -> None:
        self.coordinator.log_warning(self.domain, message, extra)


class ModuleRegistry:
    """Holds the active modules and runs the contribution pipeline in order."""

    def __init__(self) -> None:
        self._modules: list[PowerPilotModule] = []

    def register(self, module: PowerPilotModule) -> None:
        self._modules.append(module)

    @property
    def modules(self) -> list[PowerPilotModule]:
        return list(self._modules)

    async def async_setup_all(self) -> None:
        for module in self._modules:
            try:
                await module.async_setup()
            except Exception as err:  # noqa: BLE001 - one module must not break others
                module.last_error = f"setup: {err}"
                _LOGGER.exception("Error setting up module %s", module.domain)

    async def async_update_all(self) -> None:
        for module in self._modules:
            try:
                await module.async_update()
                module.last_error = None
            except Exception as err:  # noqa: BLE001
                module.last_error = f"update: {err}"
                _LOGGER.exception("Error updating module %s", module.domain)

    async def async_clear_all(self) -> None:
        """Wipe persisted data/cache for every module (config untouched)."""
        for module in self._modules:
            try:
                await module.async_clear_data()
            except Exception:  # noqa: BLE001 - one module must not break others
                _LOGGER.exception("Error clearing data for module %s", module.domain)

    def contribute_all(self, forecast: Forecast) -> None:
        for module in self._modules:
            try:
                module.contribute(forecast)
            except Exception as err:  # noqa: BLE001
                module.last_error = f"contribute: {err}"
                _LOGGER.exception("Error in contribute() of module %s", module.domain)

    def collect_reminders(self) -> list[str]:
        reminders: list[str] = []
        for module in self._modules:
            try:
                reminders.extend(module.collect_reminders())
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Error collecting reminders from %s", module.domain)
        return reminders
