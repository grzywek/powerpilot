"""Integration setup tests — proves the integration loads inside a real HA loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.powerpilot.const import (
    CONF_BUY_PRICE_SENSOR,
    CONF_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
    InverterMode,
)
from custom_components.powerpilot.models import Decision, Forecast, HourSlot, Plan


def test_plan_decision_at_uses_hour_window() -> None:
    """A plan selects the decision whose hour contains the requested moment."""
    hour = datetime(2026, 6, 28, 10, tzinfo=timezone.utc)
    plan = Plan(
        forecast=Forecast(
            slots=[
                HourSlot(start=hour),
                HourSlot(start=hour + timedelta(hours=1)),
            ]
        ),
        decisions=[
            Decision(start=hour, inverter_mode=InverterMode.DISCHARGE),
            Decision(
                start=hour + timedelta(hours=1),
                inverter_mode=InverterMode.CHARGE,
            ),
        ],
    )

    assert (
        plan.decision_at(hour + timedelta(minutes=59)).inverter_mode
        == InverterMode.DISCHARGE
    )
    assert (
        plan.decision_at(hour + timedelta(hours=1)).inverter_mode
        == InverterMode.CHARGE
    )
    assert plan.decision_at(hour - timedelta(seconds=1)) is None


def test_decision_from_dict_roundtrip() -> None:
    """A committed decision survives serialise → restore (mid-hour restart)."""
    hour = datetime(2026, 6, 28, 14, tzinfo=timezone.utc)
    original = Decision(
        start=hour,
        inverter_mode=InverterMode.CHARGE,
        charge_power_kw=4.2,
        battery_charge_kwh=3.9,
        battery_soc=61.5,
        grid_buy_kwh=4.2,
    )
    restored = Decision.from_dict(original.as_dict())
    assert restored.start == hour
    assert restored.inverter_mode == InverterMode.CHARGE
    assert restored.charge_power_kw == 4.2
    assert restored.battery_charge_kwh == 3.9
    assert restored.battery_soc == 61.5


async def test_setup_creates_entities(hass: HomeAssistant) -> None:
    """The integration sets up and creates its output entities."""
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc"},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    for entity_id in (
        "sensor.powerpilot_inverter_mode",
        "sensor.powerpilot_charge_power",
        "sensor.powerpilot_battery_energy_cost",
        "sensor.powerpilot_optimization_plan",
        "binary_sensor.powerpilot_grid_connected",
        "binary_sensor.powerpilot_ev_charge",
    ):
        assert hass.states.get(entity_id) is not None, entity_id


async def test_plan_reacts_to_price_sensor(hass: HomeAssistant) -> None:
    """With a price sensor, the plan exposes a priced forecast."""
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    hass.states.async_set(
        "sensor.energy_price",
        "0.50",
        {
            "unit_of_measurement": "PLN/kWh",
            "prices": [
                {"hour": h, "value": 0.3 if h < 6 else 1.0} for h in range(24)
            ],
        },
    )
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **DEFAULTS,
            CONF_SOC_SENSOR: "sensor.soc",
            CONF_BUY_PRICE_SENSOR: "sensor.energy_price",
        },
        title="PowerPilot",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    plan = hass.states.get("sensor.powerpilot_optimization_plan")
    assert plan is not None
    assert plan.attributes.get("horizon_hours", 0) >= 1


async def test_unready_inputs_flags_unavailable_core_sensor(hass: HomeAssistant) -> None:
    """A configured core sensor that is unavailable/missing defers setup."""
    from custom_components.powerpilot import _unready_inputs
    from custom_components.powerpilot.const import (
        CONF_CONSUMPTION_SENSOR,
        CONF_SOC_SENSOR,
    )

    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.cons", "unavailable", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **DEFAULTS,
            CONF_SOC_SENSOR: "sensor.soc",
            CONF_CONSUMPTION_SENSOR: "sensor.cons",  # unavailable
        },
    )
    # Unavailable consumption + a never-created (still configured? no) → only the
    # unavailable one is flagged; the available SoC is not.
    assert _unready_inputs(hass, entry) == ["sensor.cons"]


async def test_unready_inputs_ignores_ev_and_unset(hass: HomeAssistant) -> None:
    """EV sensors may flap (car asleep) and must never block setup."""
    from custom_components.powerpilot import _unready_inputs
    from custom_components.powerpilot.const import CONF_EV_SOC_SENSOR, CONF_SOC_SENSOR

    hass.states.async_set("sensor.soc", "60", {"unit_of_measurement": "%"})
    hass.states.async_set("sensor.ev", "unavailable", {})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={
            **DEFAULTS,
            CONF_SOC_SENSOR: "sensor.soc",
            CONF_EV_SOC_SENSOR: "sensor.ev",  # unavailable but optional
        },
    )
    assert _unready_inputs(hass, entry) == []


async def test_hour_boundary_updates_current_mode_without_waiting_for_interval(
    hass: HomeAssistant,
    freezer,
) -> None:
    """The exposed current mode advances exactly at the next clock hour."""
    now = datetime(2026, 6, 28, 10, 7, 13, tzinfo=timezone.utc)
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc"},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]

    async def _noop_refresh() -> None:
        return None

    coordinator.async_refresh = _noop_refresh

    hour = now.replace(minute=0, second=0, microsecond=0)
    plan = Plan(
        forecast=Forecast(
            slots=[
                HourSlot(start=hour),
                HourSlot(start=hour + timedelta(hours=1)),
            ]
        ),
        decisions=[
            Decision(start=hour, inverter_mode=InverterMode.DISCHARGE),
            Decision(
                start=hour + timedelta(hours=1),
                inverter_mode=InverterMode.CHARGE,
            ),
        ],
        created_at=now,
    )
    coordinator.async_set_updated_data(plan)
    await hass.async_block_till_done()

    assert (
        hass.states.get("sensor.powerpilot_inverter_mode").state
        == InverterMode.DISCHARGE
    )

    next_hour = hour + timedelta(hours=1)
    freezer.move_to(next_hour)
    async_fire_time_changed(hass, next_hour)
    await hass.async_block_till_done()

    assert (
        hass.states.get("sensor.powerpilot_inverter_mode").state
        == InverterMode.CHARGE
    )


async def test_active_hour_decision_is_frozen(
    hass: HomeAssistant,
    freezer,
) -> None:
    """A mid-hour re-run keeps the committed action for the active hour.

    Reproduces the restart-mid-charge bug: the optimizer would re-decide the
    current hour (charge → passthrough); the freeze pins it to what was
    committed when the hour began.
    """
    now = datetime(2026, 6, 28, 14, 38, tzinfo=timezone.utc)
    freezer.move_to(now)
    async_fire_time_changed(hass, now)
    hass.states.async_set("sensor.soc", "50", {"unit_of_measurement": "%"})

    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc"},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    coordinator = hass.data[DOMAIN][entry.entry_id]
    hour = now.replace(minute=0, second=0, microsecond=0)

    # First run this hour decides CHARGE; a later re-run would decide PASSTHROUGH.
    modes = iter(
        [InverterMode.CHARGE, InverterMode.PASSTHROUGH, InverterMode.PASSTHROUGH]
    )

    class _StubOptimizer:
        def optimize(self, *args, **kwargs):
            mode = next(modes)
            return Plan(
                forecast=Forecast(
                    slots=[
                        HourSlot(start=hour),
                        HourSlot(start=hour + timedelta(hours=1)),
                    ]
                ),
                decisions=[
                    Decision(start=hour, inverter_mode=mode),
                    Decision(
                        start=hour + timedelta(hours=1),
                        inverter_mode=InverterMode.PASSTHROUGH,
                    ),
                ],
                created_at=now,
            )

    coordinator._build_optimizer = lambda: _StubOptimizer()
    # Start from a clean freeze so the first stub run does the committing.
    coordinator._committed_hour = None
    coordinator._committed_decision = None

    first = await coordinator._async_update_data()
    assert first.decisions[0].inverter_mode == InverterMode.CHARGE

    # The re-run's fresh decision is PASSTHROUGH, but the active hour is frozen.
    second = await coordinator._async_update_data()
    assert second.decisions[0].inverter_mode == InverterMode.CHARGE
