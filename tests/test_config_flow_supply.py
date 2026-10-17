"""Options flow: the apartment supply section (sensors, apartment meter, thresholds)."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    CONF_APARTMENT_METER,
    CONF_APARTMENT_POWER_SENSOR,
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    CONF_SENSOR_PARENTS,
    CONF_SOC_SENSOR,
    CONF_SUPPLY_CONFIRM_SAMPLES,
    CONF_SUPPLY_MARGIN_W,
    CONF_SUPPLY_MIN_APARTMENT_W,
    CONF_VICTRON_OTHER_POWER_SENSORS,
    CONF_VICTRON_OUTPUT_POWER_SENSOR,
    DEFAULTS,
    DOMAIN,
)

SENSORS = {
    CONF_APARTMENT_POWER_SENSOR: "sensor.apartment_power",
    CONF_VICTRON_OUTPUT_POWER_SENSOR: "sensor.victron_output_power",
    CONF_VICTRON_OTHER_POWER_SENSORS: ["sensor.garage_socket_power"],
}


async def _open_supply(hass: HomeAssistant, options: dict | None = None):
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **DEFAULTS,
            CONF_SOC_SENSOR: "sensor.soc",
            CONF_CONSUMPTION_SENSOR: "sensor.main_energy",
            CONF_DEVICE_SENSORS: ["sensor.apartment_energy", "sensor.boiler_energy"],
        },
        options=options or {},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.MENU
    assert "supply" in result["menu_options"]
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"next_step_id": "supply"}
    )
    assert result["step_id"] == "supply"
    return entry, result


async def _configure(hass: HomeAssistant, result, user_input: dict):
    return await hass.config_entries.options.async_configure(result["flow_id"], user_input)


async def _finish(hass: HomeAssistant, result):
    assert result["type"] is FlowResultType.MENU
    result = await _configure(hass, result, {"next_step_id": "finish"})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result


def _field(result, name: str):
    for key, value in result["data_schema"].schema.items():
        if key == name:
            return key, value
    raise AssertionError(f"no field {name!r}")


async def test_supply_section_saves_sensors_meter_and_thresholds(
    hass: HomeAssistant,
) -> None:
    entry, result = await _open_supply(hass)
    # Defaults come from the plan: 300 W, 100 W, 3 samples.
    assert _field(result, CONF_SUPPLY_MIN_APARTMENT_W)[0].default() == 300.0
    assert _field(result, CONF_SUPPLY_MARGIN_W)[0].default() == 100.0
    assert _field(result, CONF_SUPPLY_CONFIRM_SAMPLES)[0].default() == 3
    # The apartment is picked among the metered devices.
    _, meter = _field(result, CONF_APARTMENT_METER)
    assert [o["value"] for o in meter.config["options"]] == [
        "sensor.apartment_energy",
        "sensor.boiler_energy",
    ]

    result = await _configure(
        hass,
        result,
        {
            **SENSORS,
            CONF_APARTMENT_METER: "sensor.apartment_energy",
            CONF_SUPPLY_MIN_APARTMENT_W: 400,
            CONF_SUPPLY_MARGIN_W: 150,
            CONF_SUPPLY_CONFIRM_SAMPLES: 4,
        },
    )
    await _finish(hass, result)

    options = entry.options
    assert options[CONF_APARTMENT_POWER_SENSOR] == "sensor.apartment_power"
    assert options[CONF_VICTRON_OUTPUT_POWER_SENSOR] == "sensor.victron_output_power"
    assert options[CONF_VICTRON_OTHER_POWER_SENSORS] == ["sensor.garage_socket_power"]
    assert options[CONF_APARTMENT_METER] == "sensor.apartment_energy"
    assert options[CONF_SUPPLY_MIN_APARTMENT_W] == 400.0
    assert options[CONF_SUPPLY_MARGIN_W] == 150.0
    assert options[CONF_SUPPLY_CONFIRM_SAMPLES] == 4
    assert isinstance(options[CONF_SUPPLY_CONFIRM_SAMPLES], int)


async def test_a_partial_sensor_set_is_refused(hass: HomeAssistant) -> None:
    _, result = await _open_supply(hass)
    result = await _configure(
        hass,
        result,
        {
            CONF_APARTMENT_POWER_SENSOR: "sensor.apartment_power",
            CONF_VICTRON_OUTPUT_POWER_SENSOR: "sensor.victron_output_power",
        },
    )
    assert result["step_id"] == "supply"
    assert result["errors"] == {"base": "supply_incomplete"}


async def test_one_sensor_in_two_roles_is_refused(hass: HomeAssistant) -> None:
    _, result = await _open_supply(hass)
    result = await _configure(
        hass,
        result,
        {**SENSORS, CONF_VICTRON_OTHER_POWER_SENSORS: ["sensor.apartment_power"]},
    )
    assert result["errors"] == {"base": "supply_sensor_reused"}


async def test_clearing_the_sensors_turns_detection_off(hass: HomeAssistant) -> None:
    entry, result = await _open_supply(
        hass, {**SENSORS, CONF_APARTMENT_METER: "sensor.apartment_energy"}
    )
    result = await _configure(hass, result, {})
    await _finish(hass, result)
    for key in (*SENSORS, CONF_APARTMENT_METER):
        assert key not in entry.options, key


async def test_an_apartment_meter_below_another_meter_is_refused(hass: HomeAssistant) -> None:
    entry, result = await _open_supply(hass)
    hass.config_entries.async_update_entry(
        entry,
        data={
            **entry.data,
            CONF_SENSOR_PARENTS: {"sensor.apartment_energy": "sensor.boiler_energy"},
        },
    )
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await _configure(hass, result, {"next_step_id": "supply"})
    result = await _configure(
        hass,
        result,
        {
            **SENSORS,
            CONF_APARTMENT_METER: "sensor.apartment_energy",
            CONF_SUPPLY_MIN_APARTMENT_W: 300,
            CONF_SUPPLY_MARGIN_W: 100,
            CONF_SUPPLY_CONFIRM_SAMPLES: 3,
        },
    )
    assert result["step_id"] == "supply"
    assert result["errors"] == {"base": "supply_apartment_not_top_level"}
