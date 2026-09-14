"""Reserve recovery end to end: a SoC drop below the minimum re-plans at once.

The coordinator latches the recovery from the live SoC sensor and re-plans the
moment the reading crosses the latch — not at the next clock hour — so the
active hour switches to charging while the pack is still being drained.
"""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.battery import RECOVERY_MARGIN_SOC
from custom_components.powerpilot.const import (
    CONF_MIN_SOC,
    CONF_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
    InverterMode,
)

MIN_SOC = 6.0


async def _setup(hass: HomeAssistant):
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc", CONF_MIN_SOC: MIN_SOC},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return hass.data[DOMAIN][entry.entry_id]


async def _set_soc(hass: HomeAssistant, value: float) -> None:
    hass.states.async_set("sensor.soc", str(value), {"unit_of_measurement": "%"})
    await hass.async_block_till_done()


async def test_drop_below_minimum_replans_into_recovery(hass: HomeAssistant) -> None:
    coordinator = await _setup(hass)
    assert coordinator._ess_recovering is False

    await _set_soc(hass, 2)
    # The debounced refresh the listener requested.
    await coordinator.async_refresh()
    await hass.async_block_till_done()

    assert coordinator._ess_recovering is True
    first = coordinator.data.decisions[0]
    assert first.trace["soc_before"] == 2.0
    assert first.inverter_mode == InverterMode.CHARGE
    assert "odbudowa rezerwy" in first.trace["reason"]


async def test_latch_holds_until_margin(hass: HomeAssistant) -> None:
    coordinator = await _setup(hass)
    await _set_soc(hass, 2)
    await coordinator.async_refresh()

    # Back above the minimum but short of the margin: still recovering.
    assert coordinator._soc_flips_recovery(str(MIN_SOC + 1)) is False
    # Reaching the margin is a flip worth a re-plan.
    assert coordinator._soc_flips_recovery(str(MIN_SOC + RECOVERY_MARGIN_SOC)) is True

    await _set_soc(hass, MIN_SOC + RECOVERY_MARGIN_SOC)
    await coordinator.async_refresh()
    assert coordinator._ess_recovering is False


async def test_ordinary_soc_changes_do_not_flip(hass: HomeAssistant) -> None:
    coordinator = await _setup(hass)
    assert coordinator._soc_flips_recovery("40") is False
    assert coordinator._soc_flips_recovery("unavailable") is False
    assert coordinator._soc_flips_recovery(str(MIN_SOC - 0.5)) is True
