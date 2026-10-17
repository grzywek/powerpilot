"""Options flow: editions of the price cap (Tarcza) and the PPE 1 meters."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    CONF_PPE1_IMPORT_SENSORS,
    CONF_SOC_SENSOR,
    CONF_TARCZA_TERMS,
    DEFAULTS,
    DOMAIN,
)

TERMS_2026 = {
    "name": "Tarcza Pstryk 2026",
    "valid_from": "2025-01-01",
    "valid_to": "2026-12-31",
    "limit_kwh": 0.61,
    "limit_includes_vat": True,
    "handling_fee_included": True,
}
TERMS_2027 = {
    "name": "Tarcza Pstryk 2027",
    "valid_from": "2027-01-01",
    "valid_to": "2027-12-31",
    "limit_kwh": 0.50,
    "limit_includes_vat": False,
    "handling_fee_included": False,
}


async def _open_billing(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc"},
        options=options or {},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "billing"}
    )
    assert result["step_id"] == "billing"
    return entry, result


async def _configure(hass: HomeAssistant, result, user_input: dict):
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def _finish(hass: HomeAssistant, result):
    result = await _configure(hass, result, {"action": "__back__"})
    assert result["type"] is FlowResultType.MENU
    result = await _configure(hass, result, {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result


def _actions(result) -> list[str]:
    schema = result["data_schema"].schema
    (selector,) = schema.values()
    return [option["value"] for option in selector.config["options"]]


async def test_editions_are_added_one_by_one_and_kept_in_date_order(
    hass: HomeAssistant,
) -> None:
    entry, result = await _open_billing(hass)
    for terms in (TERMS_2027, TERMS_2026):
        result = await _configure(hass, result, {"action": "__add__"})
        assert result["step_id"] == "billing_terms"
        result = await _configure(hass, result, terms)
        assert result["step_id"] == "billing"
    await _finish(hass, result)

    stored = entry.options[CONF_TARCZA_TERMS]
    assert [t["name"] for t in stored] == ["Tarcza Pstryk 2026", "Tarcza Pstryk 2027"]
    assert stored[1]["limit_includes_vat"] is False
    assert stored[1]["handling_fee_included"] is False
    assert all(t["id"] for t in stored)


async def test_partial_months_and_overlaps_are_refused_in_the_form(
    hass: HomeAssistant,
) -> None:
    entry, result = await _open_billing(hass, {CONF_TARCZA_TERMS: []})
    result = await _configure(hass, result, {"action": "__add__"})
    result = await _configure(hass, result, {**TERMS_2026, "valid_from": "2025-01-15"})
    assert result["step_id"] == "billing_terms"
    assert result["errors"] == {"base": "cap_terms_whole_months"}

    result = await _configure(hass, result, TERMS_2026)
    result = await _configure(hass, result, {"action": "__add__"})
    result = await _configure(hass, result, {**TERMS_2027, "valid_from": "2026-12-01"})
    assert result["errors"] == {"base": "cap_terms_overlap"}

    result = await _configure(hass, result, {**TERMS_2027, "limit_kwh": 0})
    assert result["errors"] == {"base": "cap_terms_limit"}


async def test_an_edition_is_edited_in_place_and_deleted(hass: HomeAssistant) -> None:
    stored = {**TERMS_2026, "id": "t2026"}
    entry, result = await _open_billing(hass, {CONF_TARCZA_TERMS: [stored]})
    assert "edit:t2026" in _actions(result) and "del:t2026" in _actions(result)

    result = await _configure(hass, result, {"action": "edit:t2026"})
    assert result["step_id"] == "billing_terms"
    # Editing an edition must not count as overlapping itself.
    result = await _configure(hass, result, {**TERMS_2026, "limit_kwh": 0.65})
    assert result["step_id"] == "billing"
    await _finish(hass, result)
    (edited,) = entry.options[CONF_TARCZA_TERMS]
    assert edited["id"] == "t2026" and edited["limit_kwh"] == 0.65

    entry2, result = await _open_billing(hass, {CONF_TARCZA_TERMS: [stored]})
    result = await _configure(hass, result, {"action": "del:t2026"})
    assert result["step_id"] == "billing"
    await _finish(hass, result)
    assert entry2.options[CONF_TARCZA_TERMS] == []


async def test_ppe1_meters_have_their_own_form(hass: HomeAssistant) -> None:
    entry, result = await _open_billing(hass)
    result = await _configure(hass, result, {"action": "__meters__"})
    assert result["step_id"] == "billing_meters"
    result = await _configure(
        hass, result, {CONF_PPE1_IMPORT_SENSORS: ["sensor.acin_out", "sensor.acin_inv"]}
    )
    assert result["step_id"] == "billing"
    await _finish(hass, result)
    assert entry.options[CONF_PPE1_IMPORT_SENSORS] == ["sensor.acin_out", "sensor.acin_inv"]
