"""Mid-hour re-plan tracking.

The hour's vintage is frozen as the hour begins, so the panel's "prognoza"
column cannot, on its own, tell a deliberate mid-hour re-plan (the EV cable went
in at :10) from the plan simply having been wrong. These tests pin the record
that answers it: every material re-plan of the running hour, labelled with what
triggered it, plus the flag for EV charging no plan ever asked for.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    CONF_EV_ENERGY_ADDED_SENSOR,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_PLUG_SENSOR,
    CONF_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
    InverterMode,
)


async def _setup(hass: HomeAssistant, **config):
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc", **config},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async def _empty_recent_soc(*args):
        return {}

    coordinator._recent_soc = _empty_recent_soc
    return coordinator


async def test_unchanged_replan_records_nothing(hass: HomeAssistant) -> None:
    """Re-running the optimizer with the same verdict is not a revision.

    Every state flip re-runs the plan; recording identical results would bury
    the one re-plan that matters under dozens of no-ops.
    """
    coordinator = await _setup(hass)
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)

    await coordinator._maybe_record_snapshot(
        coordinator.data.forecast, coordinator.data, "kalendarz"
    )

    assert coordinator.snapshots.revisions_at(hour) == []


async def test_replan_that_starts_ev_charging_is_recorded(hass: HomeAssistant) -> None:
    """The car becoming chargeable mid-hour leaves a trace with its cause."""
    coordinator = await _setup(hass)
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    plan = coordinator.data
    assert plan.decisions[0].start == hour

    # What the 13:10 re-plan looked like: charging the car for the rest of the
    # hour, where the hour-start plan had none.
    plan.decisions[0].ev_charge = True
    plan.decisions[0].ev_charge_kwh = 9.08
    plan.decisions[0].ev_charge_minutes = 49

    await coordinator._maybe_record_snapshot(
        plan.forecast, plan, "kabel EV: podłączono"
    )

    revisions = coordinator.snapshots.revisions_at(hour)
    assert len(revisions) == 1
    assert revisions[0]["why"] == "kabel EV: podłączono"
    assert revisions[0]["ev"] == 9.08
    assert revisions[0]["ev_min"] == 49
    # The vintage still holds the plan the hour STARTED with (no EV charging),
    # so forecast accuracy keeps measuring against it.
    assert (coordinator.snapshots.run0_at(hour, "ev") or 0.0) == 0.0


async def test_mode_change_is_a_revision(hass: HomeAssistant) -> None:
    """A different inverter mode for the running hour always counts."""
    coordinator = await _setup(hass)
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    plan = coordinator.data
    started_as = plan.decisions[0].inverter_mode
    plan.decisions[0].inverter_mode = (
        InverterMode.DISCHARGE
        if started_as != InverterMode.DISCHARGE
        else InverterMode.CHARGE
    )

    await coordinator._maybe_record_snapshot(plan.forecast, plan, None)

    revisions = coordinator.snapshots.revisions_at(hour)
    assert len(revisions) == 1
    assert revisions[0]["why"] is None  # an unattributed re-plan is still recorded


async def test_shrinking_kwh_alone_is_not_a_revision(hass: HomeAssistant) -> None:
    """Slot 0 covers only the hour's remainder, so its kWh shrink on every
    mid-hour re-run. That is arithmetic, not a change of mind."""
    coordinator = await _setup(hass)
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    decision = coordinator.data.decisions[0]
    decision.battery_charge_kwh = decision.battery_charge_kwh / 2 - 1.0
    decision.grid_buy_kwh = decision.grid_buy_kwh / 2 - 1.0

    await coordinator._maybe_record_snapshot(
        coordinator.data.forecast, coordinator.data, None
    )

    assert coordinator.snapshots.revisions_at(hour) == []


async def test_trigger_label_names_the_entity_that_flipped(
    hass: HomeAssistant,
) -> None:
    """The recorded cause reads as the panel shows it, per configured role."""
    hass.states.async_set("binary_sensor.plug", "off")
    hass.states.async_set("device_tracker.car", "not_home")
    coordinator = await _setup(
        hass,
        **{
            CONF_EV_PLUG_SENSOR: "binary_sensor.plug",
            CONF_EV_LOCATION_SENSOR: "device_tracker.car",
        },
    )

    hass.states.async_set("binary_sensor.plug", "on")
    coordinator._note_trigger("binary_sensor.plug", hass.states.get("binary_sensor.plug"))
    assert coordinator._consume_trigger() == "kabel EV: podłączono"
    # Consumed once — a later re-plan must not inherit someone else's reason.
    assert coordinator._consume_trigger() is None

    hass.states.async_set("device_tracker.car", "home")
    coordinator._note_trigger("device_tracker.car", hass.states.get("device_tracker.car"))
    hass.states.async_set("binary_sensor.plug", "off")
    coordinator._note_trigger("binary_sensor.plug", hass.states.get("binary_sensor.plug"))
    assert coordinator._consume_trigger() == "lokalizacja auta: w domu, kabel EV: odłączono"

    # An entity PowerPilot doesn't watch contributes no reason.
    coordinator._note_trigger("sensor.random", hass.states.get("sensor.soc"))
    assert coordinator._consume_trigger() is None


def _vintage(hour, ev: list[float]) -> dict:
    """Minimal vintage for ``hour`` planning ``ev`` kWh over its horizon."""
    return {
        "run_at": hour.isoformat(),
        "start": hour.isoformat(),
        "n": len(ev),
        "horizon_hours": len(ev),
        "revcap": True,
        "ev": list(ev),
        "charge": [0.0] * len(ev),
        "charge_pw": [0.0] * len(ev),
        "grid": [0.5] * len(ev),
        "soc": [50.0] * len(ev),
        "mode": ["p"] * len(ev),
    }


def _with_ev_meter(coordinator, readings: dict) -> None:
    async def fake_range(sensor, start, end):
        return dict(readings) if sensor == "sensor.ev_added" else {}

    async def fake_partial(sensor, start, end):
        return None

    coordinator.config[CONF_EV_ENERGY_ADDED_SENSOR] = "sensor.ev_added"
    coordinator.consumption.async_range_kwh = fake_range
    coordinator.consumption.async_partial_kwh = fake_partial


async def test_series_flags_ev_charging_no_plan_asked_for(
    hass: HomeAssistant,
) -> None:
    """Realized EV energy in an hour no plan ever scheduled raises the flag."""
    coordinator = await _setup(hass)
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    past_hour = hour - timedelta(hours=1)

    # A vintage backs the past hour, and it planned no EV charging.
    coordinator.snapshots.add(_vintage(past_hour, [0.0, 0.0]))
    _with_ev_meter(coordinator, {past_hour: 7.5})

    result = await coordinator.get_series(past_hours=2)
    row = next(h for h in result["hours"] if h["start"] == past_hour.isoformat())
    assert row["ev_off_plan"] is True

    # Once a mid-hour revision shows the charging WAS planned, the flag clears —
    # that is the whole difference between a decision and a surprise.
    coordinator.snapshots.add_revision(past_hour, {"at": "x", "why": "kabel", "ev": 7.5})
    result = await coordinator.get_series(past_hours=2)
    row = next(h for h in result["hours"] if h["start"] == past_hour.isoformat())
    assert row["ev_off_plan"] is None
    assert row["revisions"][0]["why"] == "kabel"


async def test_series_never_accuses_hours_older_than_revision_tracking(
    hass: HomeAssistant,
) -> None:
    """A vintage from before re-plans were recorded has no revisions because
    nobody was looking — that silence must not read as "nothing was planned"."""
    coordinator = await _setup(hass)
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    past_hour = hour - timedelta(hours=1)

    old_vintage = _vintage(past_hour, [0.0, 0.0])
    del old_vintage["revcap"]  # as written by a build without revision tracking
    coordinator.snapshots.add(old_vintage)
    _with_ev_meter(coordinator, {past_hour: 7.5})

    result = await coordinator.get_series(past_hours=2)
    row = next(h for h in result["hours"] if h["start"] == past_hour.isoformat())
    assert row["ev_off_plan"] is None


async def test_series_tolerates_a_session_spilling_past_its_planned_hour(
    hass: HomeAssistant,
) -> None:
    """A charger stopping a few minutes late is the previous hour's session
    ending, not an unplanned one — but a full session there still counts."""
    coordinator = await _setup(hass)
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    spill_hour = hour - timedelta(hours=1)
    planned_hour = hour - timedelta(hours=2)

    coordinator.snapshots.add(_vintage(planned_hour, [3.5, 0.0]))  # charged, then not
    coordinator.snapshots.add(_vintage(spill_hour, [0.0, 0.0]))

    # Under 10 minutes at the configured 3.5 kW → the session finishing.
    _with_ev_meter(coordinator, {spill_hour: 0.4})
    result = await coordinator.get_series(past_hours=3)
    row = next(h for h in result["hours"] if h["start"] == spill_hour.isoformat())
    assert row["ev_off_plan"] is None

    # Far more than an overrun can explain → still flagged.
    _with_ev_meter(coordinator, {spill_hour: 5.0})
    result = await coordinator.get_series(past_hours=3)
    row = next(h for h in result["hours"] if h["start"] == spill_hour.isoformat())
    assert row["ev_off_plan"] is True
