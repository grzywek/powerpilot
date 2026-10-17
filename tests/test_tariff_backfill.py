"""Distribution-tariff backfill: classifying past days via workday.check_date."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from homeassistant.exceptions import HomeAssistantError

from custom_components.powerpilot.modules.tariff import TariffModule


def _module(call) -> TariffModule:
    module = object.__new__(TariffModule)
    module.hass = SimpleNamespace(services=SimpleNamespace(async_call=call))
    return module


async def test_a_failed_check_date_skips_the_day_instead_of_aborting_the_backfill() -> None:
    """Found 2026-10-17 by ruff (F821): the except branch logged through an
    undefined ``_LOGGER``, so the first failing ``workday.check_date`` raised
    ``NameError`` and aborted the whole backfill it was meant to protect.
    """

    async def failing_call(*args, **kwargs):
        raise HomeAssistantError("workday reloading")

    result = await _module(failing_call)._classify_day(
        "binary_sensor.workday_today", date(2026, 10, 1)
    )
    assert result is None
