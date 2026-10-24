"""Missed plan actions recorded by the coordinator (ADR 002).

The pure rules are in ``test_misses.py``; these pin the wiring: what the plan
asked of an hour is kept across the boundary and a restart, a miss is found
from the live states and costed by solving the ask followed, it is settled
from the recorder and the supply log once its hour is over, the watchdog's
alarm becomes episodes, and none of it is ever deleted.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot import coordinator as coordinator_module
from custom_components.powerpilot.const import (
    CONF_EV_ENABLED,
    CONF_EV_ENERGY_ADDED_SENSOR,
    CONF_EV_LOCATION_SENSOR,
    CONF_EV_PLUG_SENSOR,
    CONF_SOC_SENSOR,
    DEFAULTS,
    DOMAIN,
)
from custom_components.powerpilot.misses import (
    KIND_ESS,
    KIND_EV_OFF_PLAN,
    KIND_EV_UNPLUGGED,
    KIND_SUPPLY,
    Ask,
    Counterfactual,
    miss_id,
    supply_record,
)
from custom_components.powerpilot.models import SupplyPlanning
from custom_components.powerpilot.modules.snapshots import SnapshotStore

ENTRY_ID = "misses_entry"
EV = {
    CONF_EV_ENABLED: True,
    CONF_EV_PLUG_SENSOR: "binary_sensor.plug",
    CONF_EV_LOCATION_SENSOR: "device_tracker.car",
}


async def _setup(hass: HomeAssistant, **config):
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    entry = MockConfigEntry(
        domain=DOMAIN,
        entry_id=ENTRY_ID,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc", **config},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    async def _empty_recent_soc(*args):
        return {}

    coordinator._recent_soc = _empty_recent_soc
    return coordinator


def _hour():
    return dt_util.now().replace(minute=0, second=0, microsecond=0)


def _ask(hour, **fields) -> Ask:
    values = {
        "at": hour - timedelta(minutes=10),
        "ev_kwh": 5.5,
        "ev_minutes": 60,
        "supply": None,
        "mode": "passthrough",
        "charge_power_kw": 0.0,
        "price_kwh": 0.45,
        **fields,
    }
    return Ask(hour=hour, **values)


async def _refresh(hass, coordinator) -> None:
    await coordinator.async_refresh()
    await hass.async_block_till_done(wait_background_tasks=True)


# ------------------------------------------------------------------ the ask


async def test_the_next_hours_ask_is_kept_with_the_snapshots(hass: HomeAssistant) -> None:
    coordinator = await _setup(hass)
    upcoming = coordinator.plan_asks.upcoming
    assert upcoming is not None
    assert upcoming.hour == _hour() + timedelta(hours=1)
    decision = coordinator.data.decision_at(upcoming.hour)
    assert upcoming.mode == decision.inverter_mode
    assert coordinator._snapshots_payload()["asks"]["upcoming"]["hour"] == upcoming.hour.isoformat()


async def test_an_hour_begins_with_its_ask_written_into_its_vintage(
    hass: HomeAssistant,
) -> None:
    """The vintage is the plan already adapted to reality; the ask is what was
    asked before it, kept beside it."""
    coordinator = await _setup(hass)
    hour = _hour()
    coordinator.snapshots = SnapshotStore()
    coordinator._last_snapshot_hour = None
    coordinator.plan_asks.current = None
    coordinator.plan_asks.upcoming = _ask(hour, ev_kwh=4.2)

    await _refresh(hass, coordinator)

    assert coordinator.plan_asks.current.ev_kwh == 4.2
    vintage = coordinator.snapshots.get(dt_util.as_utc(hour).isoformat())
    assert vintage["ask"]["ev_kwh"] == 4.2


async def test_asks_are_restored_after_a_restart(hass: HomeAssistant, hass_storage) -> None:
    hour = _hour()
    asks = {"current": None, "upcoming": _ask(hour, ev_kwh=3.3).to_dict()}
    hass_storage[f"{DOMAIN}_{ENTRY_ID}_snapshots"] = {
        "version": 1,
        "key": f"{DOMAIN}_{ENTRY_ID}_snapshots",
        "data": {"records": {}, "asks": asks},
    }
    coordinator = await _setup(hass)
    # The first run after the restart began the hour with the stored ask.
    assert coordinator.plan_asks.current.ev_kwh == 3.3


# ------------------------------------------------------------------ finding


async def test_a_car_left_unplugged_at_home_is_recorded_and_costed(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("binary_sensor.plug", "off")
    hass.states.async_set("device_tracker.car", "home")
    coordinator = await _setup(hass, **EV)
    hour = _hour()
    coordinator.plan_asks.current = _ask(hour)

    await _refresh(hass, coordinator)

    record = coordinator.misses.get(miss_id(KIND_EV_UNPLUGGED, hour))
    assert record is not None
    assert record["asked"]["ev_kwh"] == 5.5
    assert record["actual"] == {"plugged": False, "home": True}
    # Its counterfactual was solved after the plan was out.
    assert record["cf"] is not None and record["cf"]["error"] is None
    assert isinstance(record["cf"]["delta_pln"], float)
    assert any("niepodłączone" in (e.get("message") or "") for e in coordinator.get_log())

    # Found once: another run in the same hour adds nothing.
    await _refresh(hass, coordinator)
    assert len(coordinator.misses) == 1


async def test_the_counterfactual_has_the_car_on_the_cable_this_hour(
    hass: HomeAssistant,
) -> None:
    """Only the running hour's plug gate differs, and the plan's own request
    (what the reminders and the panel read) is left alone."""
    hass.states.async_set("binary_sensor.plug", "off")
    hass.states.async_set("device_tracker.car", "home")
    coordinator = await _setup(hass, **EV)
    forecast = coordinator.data.forecast
    hour = _hour()

    real = coordinator.ev.get_request(forecast)
    followed = coordinator.ev.request_as_plugged(forecast)

    assert hour not in real.available_hours
    assert followed.available_hours == real.available_hours | {hour}
    assert coordinator.ev._request is real


async def test_no_miss_while_the_car_is_away(hass: HomeAssistant) -> None:
    hass.states.async_set("binary_sensor.plug", "off")
    hass.states.async_set("device_tracker.car", "not_home")
    coordinator = await _setup(hass, **EV)
    coordinator.plan_asks.current = _ask(_hour())

    await _refresh(hass, coordinator)

    assert len(coordinator.misses) == 0


async def test_the_apartment_on_the_other_meter_is_costed_as_if_switched(
    hass: HomeAssistant,
) -> None:
    coordinator = await _setup(hass)
    hour = _hour()
    now = hour + timedelta(minutes=3)
    ask = _ask(hour, supply="meter_2", ev_kwh=0.0)
    planning = SupplyPlanning(actual="victron")
    solved: list = []

    def solve(supply=None):
        solved.append(supply)
        return coordinator.data

    pending = coordinator._find_misses(ask, now, None, solve, None, planning)

    record = coordinator.misses.get(miss_id(KIND_SUPPLY, hour))
    assert record["actual"] == {"supply": "victron"}
    (record_id, followed), = pending
    assert record_id == record["id"]
    followed()
    assert solved[0].actual == "meter_2"
    assert solved[0].last_transition_at == hour  # as if switched as the hour began


# ------------------------------------------------------------------ settling


async def test_a_late_switch_is_settled_by_its_minutes_on_the_wrong_meter(
    hass: HomeAssistant,
) -> None:
    coordinator = await _setup(hass)
    hour = _hour() - timedelta(hours=2)
    record = supply_record(_ask(hour, supply="meter_2"), hour, "victron")
    coordinator.misses.add(record)
    coordinator.misses.set_counterfactual(record["id"], Counterfactual(2.0))
    coordinator.supply.transitions = [
        SimpleNamespace(state="victron", at=hour - timedelta(days=2)),
        SimpleNamespace(state="meter_2", at=hour + timedelta(minutes=45)),
    ]

    await coordinator._async_settle_misses()

    settled = coordinator.misses.get(record["id"])
    assert settled["cost_pln"] == 1.5
    assert settled["actual"]["switched_at"] == (hour + timedelta(minutes=45)).isoformat()
    assert settled["settled_at"] is not None


async def test_an_unplugged_hour_is_settled_by_the_energy_that_stayed_missed(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("binary_sensor.plug", "on")
    hass.states.async_set("device_tracker.car", "home")
    hass.states.async_set(
        "sensor.ev_added", "12.0", {"unit_of_measurement": "kWh", "state_class": "total_increasing"}
    )
    coordinator = await _setup(hass, **EV, **{CONF_EV_ENERGY_ADDED_SENSOR: "sensor.ev_added"})
    hour = _hour() - timedelta(hours=2)
    found = hour + timedelta(minutes=1)
    from custom_components.powerpilot.misses import ev_unplugged_record

    record = ev_unplugged_record(_ask(hour), found)
    coordinator.misses.add(record)
    coordinator.misses.set_counterfactual(record["id"], Counterfactual(0.60, 5.5))

    async def partial_kwh(sensor, start, end):
        assert sensor == "sensor.ev_added"
        return 3.85  # the cable went in after all, part-way through

    coordinator.consumption.async_partial_kwh = partial_kwh

    async def no_range(sensor, start, end):
        return {}

    coordinator.consumption.async_range_kwh = no_range

    await coordinator._async_settle_misses()

    settled = coordinator.misses.get(record["id"])
    assert settled["cost_pln"] == 0.18
    assert settled["actual"]["ev_kwh"] == 3.85


async def test_a_running_hours_miss_waits_for_its_statistics(hass: HomeAssistant) -> None:
    coordinator = await _setup(hass)
    hour = _hour()
    record = supply_record(_ask(hour, supply="meter_2"), hour, "victron")
    coordinator.misses.add(record)

    await coordinator._async_settle_misses()

    assert coordinator.misses.get(record["id"])["settled_at"] is None


async def test_charging_no_plan_asked_for_is_recorded_once_with_history(
    hass: HomeAssistant,
) -> None:
    coordinator = await _setup(hass, **{CONF_EV_ENABLED: True})
    hour = _hour()
    old = hour - timedelta(hours=5)
    for back in range(5, 1, -1):
        h = hour - timedelta(hours=back)
        coordinator.snapshots.add(
            {
                "run_at": h.isoformat(),
                "start": h.isoformat(),
                "revcap": True,
                "ev": [0.0, 0.0],
                "mode": ["p", "p"],
            }
        )

    async def realized(sensor, start, end):
        assert start == dt_util.as_utc(old)  # backfilled from the oldest vintage
        return {old + timedelta(hours=1): 6.0}

    coordinator.consumption.async_range_kwh = realized
    now = dt_util.now()
    ready = hour - timedelta(minutes=1)

    assert await coordinator._async_find_off_plan_charging(ready, "sensor.ev_added", now)

    record = coordinator.misses.get(miss_id(KIND_EV_OFF_PLAN, old + timedelta(hours=1)))
    assert record["actual"]["ev_kwh"] == 6.0
    assert record["settled_at"] is not None
    assert coordinator.misses.off_plan_through == dt_util.as_utc(hour - timedelta(hours=2))
    # Nothing new to check until another hour settles.
    assert not await coordinator._async_find_off_plan_charging(ready, "sensor.ev_added", now)


# ------------------------------------------------------------------ the watchdog


async def test_the_watchdogs_alarm_becomes_one_episode(hass: HomeAssistant) -> None:
    coordinator = await _setup(hass)
    coordinator._set_execution_alarm(True, "ESS oddał 0.30 kWh", "charge", 3.0)
    since = coordinator.execution_alarm["since"]
    record_id = miss_id(KIND_ESS, since)
    assert coordinator.misses.get(record_id)["asked"] == {"mode": "charge", "charge_power_kw": 3.0}

    coordinator._set_execution_alarm(True, "ESS oddał 0.30 kWh", "charge", 3.0)
    coordinator._set_execution_alarm(False, None, "charge")

    assert len(coordinator.misses.records()) == 1
    record = coordinator.misses.get(record_id)
    assert record["settled_at"] is not None
    assert record["actual"]["until"] is not None
    assert record["cost_pln"] is None and record["note"]


async def test_an_episode_a_restart_cut_short_ends_where_it_was_last_seen(
    hass: HomeAssistant, hass_storage
) -> None:
    from custom_components.powerpilot.misses import MissLedger, ess_record

    start = _hour() - timedelta(hours=3)
    ledger = MissLedger()
    record = ess_record(start, "charge", 3.0, "ESS oddał 0.30 kWh")
    record["actual"]["last_seen"] = (start + timedelta(minutes=25)).isoformat()
    ledger.add(record)
    hass_storage[f"{DOMAIN}_{ENTRY_ID}_misses"] = {
        "version": 1,
        "key": f"{DOMAIN}_{ENTRY_ID}_misses",
        "data": ledger.to_dict(),
    }

    coordinator = await _setup(hass)

    closed = coordinator.misses.get(record["id"])
    assert closed["settled_at"] is not None
    assert closed["actual"]["until"] == (start + timedelta(minutes=25)).isoformat()
    assert "restartem" in closed["note"]


# ------------------------------------------------------------------ the store


async def test_an_unreadable_ledger_is_moved_aside_before_a_new_one_starts(
    hass: HomeAssistant, hass_storage, monkeypatch
) -> None:
    hass_storage[f"{DOMAIN}_{ENTRY_ID}_misses"] = {
        "version": 1,
        "key": f"{DOMAIN}_{ENTRY_ID}_misses",
        "data": {"records": "garbled"},
    }
    moved: list = []
    monkeypatch.setattr(coordinator_module.os, "rename", lambda a, b: moved.append((a, b)))

    coordinator = await _setup(hass)

    assert moved and ".corrupt-" in moved[0][1]
    assert coordinator._misses_writable
    assert "odłożony" in coordinator.misses_store_error


async def test_a_ledger_that_cannot_be_moved_aside_is_never_overwritten(
    hass: HomeAssistant, hass_storage, monkeypatch
) -> None:
    hass_storage[f"{DOMAIN}_{ENTRY_ID}_misses"] = {
        "version": 1,
        "key": f"{DOMAIN}_{ENTRY_ID}_misses",
        "data": {"records": "garbled"},
    }

    def refuse(a, b):
        raise OSError("read-only")

    monkeypatch.setattr(coordinator_module.os, "rename", refuse)
    coordinator = await _setup(hass)

    assert not coordinator._misses_writable
    coordinator.plan_asks.current = _ask(_hour(), supply="meter_2")
    assert coordinator._find_misses(
        coordinator.plan_asks.current, dt_util.now(), None, None, None, SupplyPlanning(actual="victron")
    ) == []
    assert hass_storage[f"{DOMAIN}_{ENTRY_ID}_misses"]["data"] == {"records": "garbled"}


async def test_clearing_the_data_keeps_the_ledger(hass: HomeAssistant) -> None:
    coordinator = await _setup(hass)
    coordinator.misses.add(supply_record(_ask(_hour(), supply="meter_2"), _hour(), "victron"))

    await coordinator.async_clear_data()

    assert len(coordinator.misses) == 1


# ------------------------------------------------------------------ shown


async def test_the_panel_gets_the_ledger_and_each_hour_its_misses(
    hass: HomeAssistant, hass_ws_client
) -> None:
    coordinator = await _setup(hass)
    hour = _hour() - timedelta(hours=1)
    record = supply_record(_ask(hour, supply="meter_2"), hour, "victron")
    coordinator.misses.add(record)
    coordinator.misses.settle(
        record["id"],
        coordinator_module.Settlement(1.25, None, {"supply": "victron", "switched_at": None}),
        dt_util.now(),
    )

    client = await hass_ws_client(hass)
    await client.send_json({"id": 1, "type": "powerpilot/misses", "days": 7})
    msg = await client.receive_json()
    assert msg["success"]
    result = msg["result"]
    assert [r["id"] for r in result["records"]] == [record["id"]]
    assert result["totals"]["7"] == {
        "count": 1,
        "cost_pln": 1.25,
        "costed": 1,
        "open": 0,
        "by_kind": {KIND_SUPPLY: 1},
    }
    assert set(result["watched"]) == {"ev_unplugged", "supply", "ess", "ev_off_plan"}

    series = await coordinator.get_series(past_hours=3)
    row = next(h for h in series["hours"] if h["start"] == hour.isoformat())
    assert row["misses"][0]["id"] == record["id"]
