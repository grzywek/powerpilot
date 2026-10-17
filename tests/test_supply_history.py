"""Apartment supply from history (stage 2, slice 2).

* At setup the live detector first replays the recorder's state history of
  the inputs on the live tick's 30 s grid, from the last stored transition (or
  the oldest history) to now.
* ``supply_for_hour`` tells each hour's supply from the log: a switch window
  makes the hours it overlaps ``mixed``; hours the log cannot tell are
  ``unknown``.
* Hours older than the log are classified from hourly means, skipping
  undecided hours and hours next to a change (plan answer 2).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_APARTMENT_METER,
    CONF_APARTMENT_POWER_SENSOR,
    CONF_VICTRON_OTHER_POWER_SENSORS,
    CONF_VICTRON_OUTPUT_POWER_SENSOR,
    DEFAULTS,
    DOMAIN,
    SUPPLY_HOUR_MIXED,
    SUPPLY_HOUR_UNKNOWN,
    SUPPLY_METER_2,
    SUPPLY_SOURCE_LIVE,
    SUPPLY_SOURCE_REPLAY,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.models import SupplyTransition
from custom_components.powerpilot.modules.supply import (
    SUPPLY_REPLAY_CHUNK,
    SUPPLY_SAMPLE_INTERVAL,
    SupplyModule,
    carry_hour_states,
    classify_hours,
    decode_log,
    hour_supply,
    past_hour_states,
)

APARTMENT = "sensor.apartment_power"
VICTRON = "sensor.victron_output_power"
GARAGE = "sensor.garage_socket_power"
SUPPLY_CONFIG = {
    CONF_APARTMENT_POWER_SENSOR: APARTMENT,
    CONF_VICTRON_OUTPUT_POWER_SENSOR: VICTRON,
    CONF_VICTRON_OTHER_POWER_SENSORS: [GARAGE],
}
ENTRY_ID = "supply_history_entry"
STORE_KEY = f"{DOMAIN}_{ENTRY_ID}_supply"
STEP = SUPPLY_SAMPLE_INTERVAL

FIXTURE = json.loads(
    (Path(__file__).parent / "fixtures" / "supply_states_victron_2026-10-16.json").read_text(
        encoding="utf-8"
    )
)
START = dt_util.parse_datetime(FIXTURE["start"])
MINUTE_15 = START + timedelta(minutes=15)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _value_at(points: list, seconds: float) -> float | None:
    value = None
    for at, watts in points:
        if at > seconds:
            break
        value = watts
    return value


def _moved_to_meter_2(seconds_from: float) -> list:
    """The Victron series as if the apartment had left it at ``seconds_from``.

    From then on the Victron no longer carries the apartment: each of its
    reports loses the apartment power last reported at that moment — the
    sensors' report times stay as misaligned as they really were.
    """
    out = []
    for at, watts in FIXTURE["victron"]:
        if watts is not None and at >= seconds_from:
            apartment = _value_at(FIXTURE["apartment"], at)
            watts = None if apartment is None else watts - apartment
        out.append([at, watts])
    return out


def _from_phase(points: list, phase_s: float) -> list:
    """The same series starting at ``phase_s``: shifts the replay grid, not the data."""
    return [[phase_s, _value_at(points, phase_s)]] + [p for p in points if p[0] > phase_s]


def _history(apartment: list, victron: list, garage: list) -> dict:
    def series(points: list) -> list:
        return [
            (START + timedelta(seconds=at), "unavailable" if watts is None else str(watts))
            for at, watts in points
        ]

    return {APARTMENT: series(apartment), VICTRON: series(victron), GARAGE: series(garage)}


def _real_history() -> dict:
    return _history(FIXTURE["apartment"], FIXTURE["victron"], FIXTURE["other"])


def _moved_history() -> dict:
    return _history(FIXTURE["apartment"], _moved_to_meter_2(15 * 60), FIXTURE["other"])


def _set_units(hass: HomeAssistant, victron_unit: str = "W") -> None:
    """Current states: history is read without attributes, the unit comes from here."""
    hass.states.async_set(APARTMENT, "500", {"unit_of_measurement": "W"})
    hass.states.async_set(VICTRON, "0.7", {"unit_of_measurement": victron_unit})
    hass.states.async_set(GARAGE, "200", {"unit_of_measurement": "W"})


def _coordinator() -> SimpleNamespace:
    unloads: list = []
    warnings: list[str] = []
    return SimpleNamespace(
        config={**DEFAULTS, **SUPPLY_CONFIG},
        entry=SimpleNamespace(
            entry_id=ENTRY_ID,
            async_on_unload=unloads.append,
            async_create_background_task=lambda hass, target, name: (
                hass.async_create_background_task(target, name)
            ),
        ),
        unloads=unloads,
        warnings=warnings,
        log_info=lambda module, message, extra=None: None,
        log_warning=lambda module, message, extra=None: warnings.append(message),
    )


async def _module(hass: HomeAssistant) -> SupplyModule:
    module = SupplyModule(hass, _coordinator())
    await module.async_setup()
    assert module._replay_task is not None
    await module._replay_task
    return module


def _transition(state: str, at: datetime, not_before: datetime | None = None, **kw):
    values = {
        "state": state,
        "at": at,
        "not_before": not_before,
        "apartment_w": 1000.0,
        "victron_net_w": 1050.0 if state == SUPPLY_VICTRON else 100.0,
        "source": SUPPLY_SOURCE_LIVE,
    }
    values.update(kw)
    return SupplyTransition(**values)


def _stored(data: dict) -> dict:
    return {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": data}


# ---------------------------------------------------------------------------
# Replay of a captured real sequence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("phase_s", [0, 10, 20])
async def test_replay_of_real_states_on_the_victron_confirms_it_once(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer, phase_s: int
) -> None:
    """30 min of the live installation's states, apartment on the Victron.

    The sensors report a few seconds apart, so a 30 s sample now and then sees
    the apartment's new power before the Victron's (one such sample at 12:30
    here): a single disagreeing sample must not confirm a switch at N = 3,
    whatever the grid's phase.
    """
    freezer.move_to(START + timedelta(minutes=30))
    _set_units(hass)
    supply_recorder(
        history=_history(
            _from_phase(FIXTURE["apartment"], phase_s),
            _from_phase(FIXTURE["victron"], phase_s),
            _from_phase(FIXTURE["other"], phase_s),
        )
    )
    module = await _module(hass)

    (transition,) = module.transitions
    assert transition.state == SUPPLY_VICTRON
    assert transition.at == START + timedelta(seconds=phase_s)
    assert transition.not_before is None
    assert transition.source == SUPPLY_SOURCE_REPLAY
    assert module.covered_from == START + timedelta(seconds=phase_s)
    assert module.replay_error is None
    stored = hass_storage[STORE_KEY]["data"]
    transitions, covered_from, checkpoint = decode_log(stored)
    assert (transitions, covered_from) == ([transition], module.covered_from)
    # The replay ends with no streak in progress: it checkpoints its last instant.
    assert checkpoint == module.checkpoint
    assert checkpoint.at > START + timedelta(minutes=29)
    # Live sampling runs on from the replayed detector.
    assert module.detector is not None and module.detector.state == SUPPLY_VICTRON
    assert module.detector.source == SUPPLY_SOURCE_LIVE
    assert len(module.coordinator.unloads) == 3  # tick, unload checkpoint, stop listener


async def test_replay_finds_the_apartment_leaving_the_victron(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    freezer.move_to(START + timedelta(minutes=30))
    _set_units(hass)
    recorder = supply_recorder(history=_moved_history())
    module = await _module(hass)

    first, switch = module.transitions
    assert first.state == SUPPLY_VICTRON
    assert switch.state == SUPPLY_METER_2
    assert switch.source == SUPPLY_SOURCE_REPLAY
    assert switch.not_before is not None
    # The switch at minute 15 lies inside [not_before, at], one grid step wide.
    assert switch.not_before <= MINUTE_15 <= switch.at
    assert switch.at - switch.not_before <= STEP
    assert module.state == SUPPLY_METER_2
    # Read in windows of at most six hours, each from one step before it.
    reads = [w for w in recorder.windows if w[3] is None]
    assert reads
    assert all(end - start <= SUPPLY_REPLAY_CHUNK + STEP for _, start, end, _ in reads)


async def test_a_long_replay_is_read_in_six_hour_windows(
    hass: HomeAssistant, supply_recorder, freezer
) -> None:
    freezer.move_to(START + timedelta(hours=20))
    _set_units(hass)
    recorder = supply_recorder(history=_real_history())
    module = await _module(hass)

    reads = [(start, end) for entity_id, start, end, limit in recorder.windows if limit is None]
    apartment_reads = [
        (start, end)
        for entity_id, start, end, limit in recorder.windows
        if limit is None and entity_id == APARTMENT
    ]
    assert len(reads) == 3 * len(apartment_reads)
    assert len(apartment_reads) == 4  # 20 h in windows of 6 h
    assert all(end - start <= SUPPLY_REPLAY_CHUNK + STEP for start, end in reads)
    assert apartment_reads[-1][1] == START + timedelta(hours=20)
    # After the captured half hour the last states hold: still on the Victron.
    assert [t.state for t in module.transitions] == [SUPPLY_VICTRON]


# ---------------------------------------------------------------------------
# Replay against the stored log
# ---------------------------------------------------------------------------


async def test_replay_starts_at_the_last_stored_transition_and_appends(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    """A slice-1 log (no ``covered_from``) still loads; it covers from its first transition."""
    stored_at = START + timedelta(minutes=5)
    stored = _transition(SUPPLY_VICTRON, stored_at)
    hass_storage[STORE_KEY] = _stored({"transitions": [stored.to_dict()]})
    freezer.move_to(START + timedelta(minutes=30))
    _set_units(hass)
    recorder = supply_recorder(history=_moved_history())

    module = await _module(hass)

    assert {start for _, start, _, _ in recorder.windows[:3]} == {stored_at - STEP}
    assert not any(limit for *_, limit in recorder.windows)  # no oldest-state lookup
    first, switch = module.transitions
    assert first == stored
    assert switch.state == SUPPLY_METER_2 and switch.at > stored_at
    assert switch.not_before <= MINUTE_15 <= switch.at
    assert module.covered_from == stored_at
    data = hass_storage[STORE_KEY]["data"]
    assert data["covered_from"] == stored_at.isoformat()
    assert decode_log(data)[:2] == ([first, switch], stored_at)


async def test_covered_from_is_written_on_the_first_run_and_kept(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    freezer.move_to(START + timedelta(minutes=30))
    _set_units(hass)
    supply_recorder(history=_moved_history())
    first_run = await _module(hass)
    assert first_run.covered_from == START
    assert hass_storage[STORE_KEY]["data"]["covered_from"] == START.isoformat()
    switch = first_run.transitions[-1]

    # Next start, later: the recorder has purged the oldest states meanwhile.
    freezer.move_to(START + timedelta(minutes=40))
    purged = {
        entity_id: [(at, raw) for at, raw in series if at >= START + timedelta(minutes=10)]
        for entity_id, series in _moved_history().items()
    }
    recorder = supply_recorder(history=purged)
    second_run = await _module(hass)

    # Resumed from the first run's checkpoint, later than its last switch.
    assert first_run.checkpoint.at > switch.at
    assert {start for _, start, _, _ in recorder.windows[:3]} == {
        first_run.checkpoint.at - STEP
    }
    assert second_run.covered_from == START
    assert second_run.transitions == first_run.transitions  # nothing new, nothing doubled
    assert hass_storage[STORE_KEY]["data"]["covered_from"] == START.isoformat()


async def test_without_any_history_the_log_covers_from_the_first_live_sample(
    hass: HomeAssistant, hass_storage, supply_recorder
) -> None:
    supply_recorder()
    _set_units(hass)
    module = await _module(hass)
    assert module.covered_from is None
    assert module.transitions == []
    moment = datetime(2026, 10, 17, 12, 0, 7, tzinfo=timezone.utc)
    await module._async_tick(moment)
    assert module.covered_from == moment


@pytest.mark.parametrize(
    "data",
    [
        # Transitions but no instant the log covers from.
        {"transitions": [_transition(SUPPLY_VICTRON, START).to_dict()], "covered_from": None},
        # Covering from after its own first transition.
        {
            "transitions": [_transition(SUPPLY_VICTRON, START).to_dict()],
            "covered_from": (START + timedelta(minutes=1)).isoformat(),
        },
        {"transitions": [], "covered_from": "yesterday"},
        {"transitions": [], "covered_from": "2026-10-16T13:10:00"},  # naive
    ],
)
def test_unusable_covered_from_makes_the_log_undecodable(data: dict) -> None:
    with pytest.raises(ValueError):
        decode_log(data)


def test_empty_logs_old_and_new_decode() -> None:
    assert decode_log({"transitions": []}) == ([], None, None)
    assert decode_log({"transitions": [], "covered_from": None}) == ([], None, None)
    assert decode_log({"transitions": [], "covered_from": START.isoformat()}) == (
        [],
        START,
        None,
    )


# ---------------------------------------------------------------------------
# Replay failures
# ---------------------------------------------------------------------------


async def test_recorder_failure_is_reported_and_live_sampling_still_starts(
    hass: HomeAssistant, hass_storage, supply_recorder, caplog
) -> None:
    stored = _transition(SUPPLY_METER_2, START)
    hass_storage[STORE_KEY] = _stored(
        {"transitions": [stored.to_dict()], "covered_from": START.isoformat()}
    )
    _set_units(hass)
    supply_recorder(error=RuntimeError("database is locked"))

    with caplog.at_level(logging.ERROR):
        module = await _module(hass)

    assert module.replay_error and "database is locked" in module.replay_error
    assert module.replay_error in module.coordinator.warnings
    assert any(
        r.levelno >= logging.ERROR and r.exc_info for r in caplog.records
    ), "the failure is logged with its traceback"
    # The stored log is untouched and live sampling runs from it.
    assert module.transitions == [stored]
    assert hass_storage[STORE_KEY]["data"]["transitions"] == [stored.to_dict()]
    assert len(module.coordinator.unloads) == 3  # tick, unload checkpoint, stop listener
    hass.states.async_set(APARTMENT, "1000", {"unit_of_measurement": "W"})
    hass.states.async_set(VICTRON, "1.5", {"unit_of_measurement": "kW"})
    hass.states.async_set(GARAGE, "400", {"unit_of_measurement": "W"})
    for i in range(3):
        await module._async_tick(dt_util.utcnow() + i * STEP)
    assert module.state == SUPPLY_VICTRON
    assert module.transitions[-1].source == SUPPLY_SOURCE_LIVE
    assert module.transitions[-1].not_before == START


async def test_input_without_a_state_stops_the_replay_not_live_sampling(
    hass: HomeAssistant, supply_recorder
) -> None:
    """History is read without attributes: with no current state its unit is unknown."""
    hass.states.async_set(APARTMENT, "500", {"unit_of_measurement": "W"})
    hass.states.async_set(VICTRON, "unavailable", {"unit_of_measurement": "kW"})
    recorder = supply_recorder(history=_real_history())
    module = await _module(hass)

    assert module.replay_error and GARAGE in module.replay_error
    assert VICTRON not in module.replay_error  # unavailable, but its unit is known
    assert recorder.windows == []
    assert module.transitions == []
    assert len(module.coordinator.unloads) == 3  # tick, unload checkpoint, stop listener


async def test_clearing_data_during_the_replay_does_not_save_the_old_log(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    hass_storage[STORE_KEY] = _stored(
        {"transitions": [_transition(SUPPLY_VICTRON, START).to_dict()]}
    )
    freezer.move_to(START + timedelta(minutes=30))
    _set_units(hass)
    recorder = supply_recorder(history=_moved_history())
    release = asyncio.Event()

    async def blocked(func, *args):
        await release.wait()
        return func(*args)

    recorder.async_add_executor_job = blocked
    module = SupplyModule(hass, _coordinator())
    await module.async_setup()
    await asyncio.sleep(0)
    assert not module._replay_task.done()

    await module.async_clear_data()
    release.set()
    await hass.async_block_till_done()

    assert module._replay_task.cancelled()
    assert STORE_KEY not in hass_storage
    assert module.transitions == [] and module.covered_from is None


# ---------------------------------------------------------------------------
# Per-hour supply from the log
# ---------------------------------------------------------------------------

H = datetime(2026, 10, 17, 10, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
COVERED = H - 6 * HOUR
FIRST = _transition(SUPPLY_VICTRON, H - 4 * HOUR + timedelta(minutes=15))


def test_a_switch_mid_hour_makes_that_hour_mixed_and_leaves_its_neighbours_clean() -> None:
    switch = _transition(
        SUPPLY_METER_2, H + timedelta(minutes=20), H + timedelta(minutes=19, seconds=30)
    )
    log = [FIRST, switch]
    assert hour_supply(log, COVERED, H - HOUR) == SUPPLY_VICTRON
    assert hour_supply(log, COVERED, H) == SUPPLY_HOUR_MIXED
    assert hour_supply(log, COVERED, H + HOUR) == SUPPLY_METER_2
    assert hour_supply(log, COVERED, H + 5 * HOUR) == SUPPLY_METER_2


def test_a_long_switch_window_makes_every_hour_it_overlaps_mixed() -> None:
    """Apartment idle for hours: the switch happened somewhere in between."""
    away = _transition(SUPPLY_METER_2, H + timedelta(minutes=20), H + timedelta(minutes=19))
    back = _transition(
        SUPPLY_VICTRON,
        H + 5 * HOUR + timedelta(minutes=10),
        H + 2 * HOUR + timedelta(minutes=30),
    )
    log = [FIRST, away, back]
    assert hour_supply(log, COVERED, H + HOUR) == SUPPLY_METER_2
    for offset in (2, 3, 4, 5):
        assert hour_supply(log, COVERED, H + offset * HOUR) == SUPPLY_HOUR_MIXED, offset
    assert hour_supply(log, COVERED, H + 6 * HOUR) == SUPPLY_VICTRON


def test_hours_the_log_cannot_tell_are_unknown() -> None:
    log = [FIRST, _transition(SUPPLY_METER_2, H + timedelta(minutes=20), H)]
    assert hour_supply(log, COVERED, COVERED - HOUR) == SUPPLY_HOUR_UNKNOWN  # before coverage
    assert hour_supply(log, COVERED, H - 5 * HOUR) == SUPPLY_HOUR_UNKNOWN  # before the first
    assert hour_supply(log, COVERED, H - 4 * HOUR) == SUPPLY_HOUR_UNKNOWN  # holds the first
    assert hour_supply(log, COVERED, H - 3 * HOUR) == SUPPLY_VICTRON
    assert hour_supply([], COVERED, H) == SUPPLY_HOUR_UNKNOWN
    assert hour_supply(log, None, H + 3 * HOUR) == SUPPLY_HOUR_UNKNOWN


def test_a_window_without_width_marks_only_the_hour_of_the_switch() -> None:
    # ``not_before`` at or after ``at`` bounds nothing: the window is ``at`` alone.
    switch = _transition(SUPPLY_METER_2, H + timedelta(minutes=20), H + timedelta(minutes=50))
    log = [FIRST, switch]
    assert hour_supply(log, COVERED, H - HOUR) == SUPPLY_VICTRON
    assert hour_supply(log, COVERED, H) == SUPPLY_HOUR_MIXED
    assert hour_supply(log, COVERED, H + HOUR) == SUPPLY_METER_2


def test_hours_are_clock_hours_in_any_time_zone() -> None:
    switch = _transition(SUPPLY_METER_2, H + timedelta(minutes=20), H + timedelta(minutes=19))
    log = [FIRST, switch]
    local = H.astimezone(ZoneInfo("Europe/Warsaw"))
    assert hour_supply(log, COVERED, local) == SUPPLY_HOUR_MIXED
    assert hour_supply(log, COVERED, local - HOUR) == SUPPLY_VICTRON


async def test_module_answers_per_hour_from_its_log(
    hass: HomeAssistant, supply_recorder, freezer
) -> None:
    freezer.move_to(START + timedelta(minutes=30))
    _set_units(hass)
    supply_recorder(history=_moved_history())
    module = await _module(hass)
    hour = START.replace(minute=0)  # 13:00, holding the first transition and the switch
    assert module.supply_for_hour(hour) == SUPPLY_HOUR_UNKNOWN
    assert module.supply_for_hour(hour + HOUR) == SUPPLY_METER_2


# ---------------------------------------------------------------------------
# Hours older than the log, from hourly means
# ---------------------------------------------------------------------------

ON_VICTRON = (1000.0, 1050.0)
ON_METER_2 = (1000.0, 120.0)


def _hours(*means) -> dict:
    return {H + i * HOUR: m for i, m in enumerate(means) if m is not ...}


def test_hourly_classification_keeps_decided_hours_only() -> None:
    """Undecided hours and hours without a mean are no evidence; changes are
    left to :func:`carry_hour_states`, which makes their neighbours mixed."""
    means = _hours(
        ON_VICTRON,  # 0
        ON_VICTRON,  # 1
        ON_VICTRON,  # 2
        (200.0, 40.0),  # 3 undecided: apartment below the minimum
        (None, 500.0),  # 4 a mean missing
        ON_METER_2,  # 5
        ON_METER_2,  # 6
        ON_METER_2,  # 7 next to a change
        ON_VICTRON,  # 8 next to a change
        (1000.0, None),  # 9 a mean missing
        ...,  # 10 no row at all
        ON_VICTRON,  # 11
    )
    assert classify_hours(means, 300.0, 100.0) == {
        H: SUPPLY_VICTRON,
        H + HOUR: SUPPLY_VICTRON,
        H + 2 * HOUR: SUPPLY_VICTRON,
        H + 5 * HOUR: SUPPLY_METER_2,
        H + 6 * HOUR: SUPPLY_METER_2,
        H + 7 * HOUR: SUPPLY_METER_2,
        H + 8 * HOUR: SUPPLY_VICTRON,
        H + 11 * HOUR: SUPPLY_VICTRON,
    }


def test_hourly_classification_uses_the_thresholds_given() -> None:
    means = _hours((500.0, 350.0))
    assert classify_hours(means, 300.0, 100.0) == {H: SUPPLY_METER_2}
    assert classify_hours(means, 300.0, 200.0) == {H: SUPPLY_VICTRON}
    assert classify_hours(means, 600.0, 100.0) == {}


async def test_hourly_states_come_from_mean_statistics_before_the_log(
    hass: HomeAssistant, supply_recorder
) -> None:
    _set_units(hass, victron_unit="kW")

    def rows(values) -> list[dict]:
        return [
            {"start": (H + i * HOUR).timestamp(), "mean": v}
            for i, v in enumerate(values)
            if v is not None
        ]

    # Hours 0-2 on the Victron, 3 without a garage mean, 4-6 on PPE 2.
    recorder = supply_recorder(
        statistics={
            APARTMENT: rows([900, 1000, 1100, 1000, 1000, 1000, 1000]),
            VICTRON: rows([1.2, 1.3, 1.4, 1.3, 0.35, 0.3, 0.3]),  # kW
            GARAGE: rows([200, 200, 200, None, 200, 200, 200]),
        }
    )
    module = SupplyModule(hass, _coordinator())
    module.covered_from = H + 6 * HOUR + timedelta(minutes=20)

    states = await module.async_hourly_states(H, H + 7 * HOUR)

    assert states == {
        H: SUPPLY_VICTRON,
        H + HOUR: SUPPLY_VICTRON,
        H + 2 * HOUR: SUPPLY_VICTRON,
        H + 4 * HOUR: SUPPLY_METER_2,
        H + 5 * HOUR: SUPPLY_METER_2,
        H + 6 * HOUR: SUPPLY_METER_2,  # starts before covered_from
    }
    assert recorder.statistics_calls == [({APARTMENT, VICTRON, GARAGE}, "hour", None, {"mean"})]

    # Hours the log covers are its own, not the statistics'.
    module.covered_from = H + 3 * HOUR
    assert set(await module.async_hourly_states(H, H + 7 * HOUR)) == {H, H + HOUR, H + 2 * HOUR}


# ---------------------------------------------------------------------------
# Past hour states for learning (slice 3): hourly states carried forward
# ---------------------------------------------------------------------------

V, M2, MIX, UNK = SUPPLY_VICTRON, SUPPLY_METER_2, SUPPLY_HOUR_MIXED, SUPPLY_HOUR_UNKNOWN


def _states(states: dict, count: int) -> list[str]:
    return [states[H + i * HOUR] for i in range(count)]


def test_a_decided_state_carries_through_undecided_and_missing_hours() -> None:
    """Plan answer 2, clarified: an undecided hour is no evidence, not unknown."""
    decided = {H + 1 * HOUR: V, H + 4 * HOUR: V, H + 7 * HOUR: V}
    states = carry_hour_states(decided, H, H + 10 * HOUR)
    assert _states(states, 10) == [UNK, V, V, V, V, V, V, V, V, V]


def test_a_change_makes_the_hours_between_and_both_neighbours_mixed() -> None:
    decided = {
        H: V,
        H + 2 * HOUR: V,  # last on the Victron
        H + 5 * HOUR: M2,  # first on PPE 2: the switch was in 2..5
        H + 6 * HOUR: M2,
        H + 7 * HOUR: V,  # straight back: 6 and 7 are next to a change
    }
    states = carry_hour_states(decided, H, H + 9 * HOUR)
    assert _states(states, 9) == [V, V, MIX, MIX, MIX, MIX, MIX, MIX, V]


def test_hours_before_the_first_decided_hour_are_unknown() -> None:
    assert carry_hour_states({}, H, H + 3 * HOUR) == {
        H: UNK, H + HOUR: UNK, H + 2 * HOUR: UNK
    }
    assert _states(carry_hour_states({H + 2 * HOUR: M2}, H, H + 4 * HOUR), 4) == [
        UNK, UNK, M2, M2
    ]


def test_carried_states_meet_an_agreeing_log() -> None:
    """The log cannot tell the hours up to its first transition; an agreeing
    carried state fills them, and from there on the log answers."""
    covered = H + 3 * HOUR + timedelta(minutes=10)
    first = _transition(V, H + 5 * HOUR + timedelta(minutes=30))
    switch = _transition(M2, H + 7 * HOUR + timedelta(minutes=5), H + 7 * HOUR)
    decided = {H: V, H + 1 * HOUR: V}
    states = past_hour_states(H, H + 9 * HOUR, decided, [first, switch], covered)
    assert _states(states, 9) == [V, V, V, V, V, V, V, MIX, M2]


def test_carried_states_meet_a_disagreeing_log() -> None:
    """The log's first state differs: the switch happened between the last
    decided hour and that first confirmation — every hour of it is mixed."""
    covered = H + 3 * HOUR + timedelta(minutes=10)
    first = _transition(M2, H + 5 * HOUR + timedelta(minutes=30))
    decided = {H: V, H + 1 * HOUR: V}
    states = past_hour_states(H, H + 8 * HOUR, decided, [first], covered)
    assert _states(states, 8) == [V, MIX, MIX, MIX, MIX, MIX, M2, M2]


def test_without_a_log_the_carried_states_reach_the_end() -> None:
    states = past_hour_states(H, H + 4 * HOUR, {H + HOUR: M2}, [], None)
    assert _states(states, 4) == [UNK, M2, M2, M2]


def test_a_log_without_transitions_stays_unknown_after_it_starts_watching() -> None:
    states = past_hour_states(H, H + 4 * HOUR, {H: V}, [], H + 2 * HOUR)
    assert _states(states, 4) == [V, V, UNK, UNK]


def test_a_window_ending_before_the_log_still_sees_the_disagreement() -> None:
    """Decided hours are read up to the log; the window is cut afterwards."""
    covered = H + 6 * HOUR
    first = _transition(M2, H + 6 * HOUR + timedelta(minutes=30))
    states = past_hour_states(H, H + 3 * HOUR, {H + HOUR: V}, [first], covered)
    assert states == {H: UNK, H + HOUR: MIX, H + 2 * HOUR: MIX}


async def test_past_hour_states_join_hourly_means_and_the_log(
    hass: HomeAssistant, supply_recorder
) -> None:
    _set_units(hass)

    def rows(values) -> list[dict]:
        return [{"start": (H + i * HOUR).timestamp(), "mean": v} for i, v in enumerate(values)]

    recorder = supply_recorder(
        statistics={
            APARTMENT: rows([1000, 100, 100]),  # on the Victron, then undecided
            VICTRON: rows([1300, 300, 300]),
            GARAGE: rows([200, 200, 200]),
        }
    )
    coordinator = _coordinator()
    coordinator.config[CONF_APARTMENT_METER] = "sensor.apartment_energy"
    module = SupplyModule(hass, coordinator)
    module.covered_from = H + 2 * HOUR + timedelta(minutes=30)
    module.transitions = [_transition(V, H + 3 * HOUR + timedelta(minutes=10))]

    states = await module.async_past_hour_states(H, H + 5 * HOUR)

    assert _states(states, 5) == [V, V, V, V, V]
    # Statistics are read only for the hours before the log.
    assert len(recorder.statistics_calls) == 1
    assert await module.async_past_hour_states(H + 3 * HOUR, H + 5 * HOUR) == {
        H + 3 * HOUR: UNK,  # the log alone cannot tell the hour of its first transition
        H + 4 * HOUR: V,
    }
    assert len(recorder.statistics_calls) == 1


@pytest.mark.parametrize("config", [{CONF_APARTMENT_METER: ""}, {CONF_APARTMENT_POWER_SENSOR: ""}])
async def test_without_detection_or_apartment_meter_every_past_hour_is_victron(
    hass: HomeAssistant, supply_recorder, config: dict
) -> None:
    recorder = supply_recorder()
    coordinator = _coordinator()
    coordinator.config[CONF_APARTMENT_METER] = "sensor.apartment_energy"
    coordinator.config.update(config)
    module = SupplyModule(hass, coordinator)
    module.transitions = [_transition(M2, H - HOUR)]
    module.covered_from = H - 2 * HOUR

    states = await module.async_past_hour_states(H, H + 3 * HOUR)

    assert states == {H: V, H + HOUR: V, H + 2 * HOUR: V}
    assert recorder.statistics_calls == []


# ---------------------------------------------------------------------------
# Replay checkpoint
# ---------------------------------------------------------------------------


def _checkpoint(at: datetime, last_decisive: datetime | None) -> dict:
    return {
        "at": at.isoformat(),
        "last_decisive": last_decisive.isoformat() if last_decisive is not None else None,
    }


async def test_restart_with_a_checkpoint_replays_only_the_downtime(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    stored = _transition(SUPPLY_VICTRON, START)
    checkpoint_at = START + timedelta(minutes=20)
    hass_storage[STORE_KEY] = _stored(
        {
            "transitions": [stored.to_dict()],
            "covered_from": START.isoformat(),
            "checkpoint": _checkpoint(checkpoint_at, checkpoint_at - STEP),
        }
    )
    freezer.move_to(START + timedelta(minutes=30))
    _set_units(hass)
    recorder = supply_recorder(history=_real_history())

    module = await _module(hass)

    reads = [(entity_id, start) for entity_id, start, _, limit in recorder.windows]
    assert reads[:3] == [(e, checkpoint_at - STEP) for e in (APARTMENT, VICTRON, GARAGE)]
    assert len(reads) == 3  # ten minutes of downtime: one window per input
    assert module.transitions == [stored]
    assert module.checkpoint.at == START + timedelta(minutes=30) - STEP
    assert hass_storage[STORE_KEY]["data"]["checkpoint"] == module.checkpoint.to_dict()


async def test_a_switch_while_home_assistant_was_down_is_confirmed_by_the_replay(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    """The checkpoint carries the switch's lower bound across the downtime.

    The apartment was on the Victron until minute 9:30, idle (undecided) from
    minute 10; the checkpoint was saved at minute 12, in the idle spell, and
    from minute 15 the apartment draws again — from PPE 2. Only the checkpoint
    knows the Victron was last seen at minute 9:30.
    """
    hass_storage[STORE_KEY] = _stored(
        {
            "transitions": [_transition(SUPPLY_VICTRON, START).to_dict()],
            "covered_from": START.isoformat(),
            "checkpoint": _checkpoint(
                START + timedelta(minutes=12), START + timedelta(minutes=9, seconds=30)
            ),
        }
    )
    freezer.move_to(START + timedelta(minutes=30))
    _set_units(hass)
    supply_recorder(
        history=_history(
            [[0, 1000], [600, 200], [900, 1000]],  # apartment
            [[0, 1250], [900, 250]],  # Victron: no longer carries it from 15:00
            [[0, 200]],  # garage
        )
    )

    module = await _module(hass)

    _, switch = module.transitions
    assert switch.state == SUPPLY_METER_2
    assert switch.at == MINUTE_15
    assert switch.not_before == START + timedelta(minutes=9, seconds=30)
    assert switch.source == SUPPLY_SOURCE_REPLAY


T_LIVE = START + timedelta(hours=1)
ON_VICTRON_W = (1000, 1250, 200)
ON_METER_2_W = (1000, 300, 200)


def _set_power(hass: HomeAssistant, watts: tuple) -> None:
    for entity_id, value in zip((APARTMENT, VICTRON, GARAGE), watts):
        hass.states.async_set(entity_id, str(value), {"unit_of_measurement": "W"})


async def _live_module(hass, hass_storage, supply_recorder, freezer) -> SupplyModule:
    """On the Victron since an hour ago; no history, so the replay checkpoints its end."""
    hass_storage[STORE_KEY] = _stored(
        {
            "transitions": [_transition(SUPPLY_VICTRON, T_LIVE - HOUR).to_dict()],
            "covered_from": (T_LIVE - HOUR).isoformat(),
        }
    )
    freezer.move_to(T_LIVE)
    _set_power(hass, ON_VICTRON_W)
    supply_recorder(history={})
    module = await _module(hass)
    assert module.checkpoint.at == T_LIVE - STEP
    return module


def _stored_checkpoint(hass_storage) -> dict:
    return hass_storage[STORE_KEY]["data"]["checkpoint"]


async def test_live_checkpoints_are_written_at_most_every_15_minutes(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    module = await _live_module(hass, hass_storage, supply_recorder, freezer)
    first = module.checkpoint
    for minute in range(1, 15):
        await module._async_tick(T_LIVE + timedelta(minutes=minute))
    assert _stored_checkpoint(hass_storage) == first.to_dict()

    due = first.at + timedelta(minutes=15)
    await module._async_tick(due)
    assert _stored_checkpoint(hass_storage) == {
        "at": due.isoformat(),
        "last_decisive": due.isoformat(),  # the agreeing sample just taken
    }


async def test_a_checkpoint_is_never_written_mid_streak(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    module = await _live_module(hass, hass_storage, supply_recorder, freezer)
    first = _stored_checkpoint(hass_storage)
    _set_power(hass, ON_METER_2_W)
    late = T_LIVE + timedelta(minutes=20)  # past due
    await module._async_tick(late)
    await module._async_tick(late + STEP)
    assert module.detector.streak == 2
    assert _stored_checkpoint(hass_storage) == first
    # Nor on stop or unload while the streak runs.
    await module._async_on_stop(None)
    for callback in reversed(module.coordinator.unloads):
        if (job := callback()) is not None:
            await job
    assert _stored_checkpoint(hass_storage) == first

    # The streak ends (an agreeing sample): the checkpoint follows at once.
    _set_power(hass, ON_VICTRON_W)
    await module._async_tick(late + 2 * STEP)
    assert _stored_checkpoint(hass_storage)["at"] == (late + 2 * STEP).isoformat()


async def test_a_confirmed_switch_is_saved_with_a_fresh_checkpoint(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    module = await _live_module(hass, hass_storage, supply_recorder, freezer)
    _set_power(hass, ON_METER_2_W)
    for i in range(1, 4):
        await module._async_tick(T_LIVE + i * STEP)
    data = hass_storage[STORE_KEY]["data"]
    assert data["transitions"][-1]["state"] == SUPPLY_METER_2
    assert data["checkpoint"]["at"] == (T_LIVE + 3 * STEP).isoformat()


async def test_unload_writes_the_checkpoint(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    module = await _live_module(hass, hass_storage, supply_recorder, freezer)
    last = T_LIVE + timedelta(minutes=3)
    await module._async_tick(last)
    assert _stored_checkpoint(hass_storage)["at"] == (T_LIVE - STEP).isoformat()
    for callback in reversed(module.coordinator.unloads):
        if (job := callback()) is not None:
            await job
    assert _stored_checkpoint(hass_storage) == {
        "at": last.isoformat(),
        "last_decisive": last.isoformat(),
    }


async def test_home_assistant_stop_writes_the_checkpoint(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer
) -> None:
    from homeassistant.const import EVENT_HOMEASSISTANT_STOP

    module = await _live_module(hass, hass_storage, supply_recorder, freezer)
    last = T_LIVE + timedelta(minutes=3)
    await module._async_tick(last)
    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()
    assert _stored_checkpoint(hass_storage)["at"] == last.isoformat()


async def test_a_failed_checkpoint_save_is_reported_and_sampling_goes_on(
    hass: HomeAssistant, hass_storage, supply_recorder, freezer, caplog
) -> None:
    module = await _live_module(hass, hass_storage, supply_recorder, freezer)
    real_save = module._store.async_save

    async def failing_save(data):
        raise OSError("disk full")

    module._store.async_save = failing_save
    due = T_LIVE + timedelta(minutes=15)
    with caplog.at_level(logging.ERROR):
        await module._async_tick(due)
    assert module.save_error and "disk full" in module.save_error
    assert module.save_error in module.coordinator.warnings
    assert any(r.levelno >= logging.ERROR and r.exc_info for r in caplog.records)

    # Sampling goes on, and the next save carries what failed.
    module._store.async_save = real_save
    _set_power(hass, ON_METER_2_W)
    for i in range(1, 4):
        await module._async_tick(due + i * STEP)
    assert module.state == SUPPLY_METER_2
    assert module.save_error is None
    assert hass_storage[STORE_KEY]["data"]["transitions"][-1]["state"] == SUPPLY_METER_2


def test_a_log_without_a_checkpoint_still_loads() -> None:
    data = {
        "transitions": [_transition(SUPPLY_VICTRON, START).to_dict()],
        "covered_from": START.isoformat(),
    }
    assert decode_log(data)[2] is None
    assert decode_log({**data, "checkpoint": None})[2] is None
    with_checkpoint = {**data, "checkpoint": _checkpoint(START + HOUR, None)}
    assert decode_log(with_checkpoint)[2].at == START + HOUR


@pytest.mark.parametrize(
    "checkpoint",
    [
        "2026-10-16T14:00:00+00:00",  # not a mapping
        {"at": "2026-10-16T14:00:00", "last_decisive": None},  # naive
        {"at": "soon", "last_decisive": None},
        {"last_decisive": None},
        {"at": "2026-10-16T14:00:00+00:00", "last_decisive": "2026-10-16T14:00:00"},
        # Earlier than the log covers.
        {"at": "2026-10-16T13:00:00+00:00", "last_decisive": None},
        # The last decisive sample after the checkpoint itself.
        {"at": "2026-10-16T14:00:00+00:00", "last_decisive": "2026-10-16T14:00:30+00:00"},
    ],
)
def test_unusable_checkpoint_makes_the_log_undecodable(checkpoint) -> None:
    data = {
        "transitions": [_transition(SUPPLY_VICTRON, START).to_dict()],
        "covered_from": START.isoformat(),
        "checkpoint": checkpoint,
    }
    with pytest.raises(ValueError):
        decode_log(data)


def test_a_checkpoint_needs_a_log_that_covers_something() -> None:
    with pytest.raises(ValueError):
        decode_log({"transitions": [], "checkpoint": _checkpoint(START, None)})
