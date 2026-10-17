"""Apartment supply: which point of delivery feeds the apartment (stage 2, slice 1).

The rule (parent spec, 2026-09-14): with the apartment drawing at least
``P_min``, when the Victron output minus the other
sub-meters behind it covers less than ``share`` of the apartment, it is on
meter 2, else on the Victron; N agreeing samples
in a row confirm a change, otherwise the last confirmed state holds.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_time_changed,
)

from custom_components.powerpilot.const import (
    CONF_APARTMENT_POWER_SENSOR,
    CONF_SOC_SENSOR,
    CONF_SUPPLY_CONFIRM_SAMPLES,
    CONF_SUPPLY_VICTRON_SHARE,
    CONF_SUPPLY_MIN_APARTMENT_W,
    CONF_VICTRON_OTHER_POWER_SENSORS,
    CONF_VICTRON_OUTPUT_POWER_SENSOR,
    DEFAULTS,
    DOMAIN,
    SENSOR_APARTMENT_SUPPLY_ACTUAL,
    SUPPLY_METER_2,
    SUPPLY_SOURCE_LIVE,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.models import SupplyTransition
from custom_components.powerpilot.modules.supply import (
    SupplyDetector,
    SupplyModule,
    classify_sample,
    decode_log,
)

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=timezone.utc)
STEP = timedelta(seconds=30)

APARTMENT = "sensor.apartment_power"
VICTRON = "sensor.victron_output_power"
GARAGE = "sensor.garage_socket_power"
SUPPLY_CONFIG = {
    CONF_APARTMENT_POWER_SENSOR: APARTMENT,
    CONF_VICTRON_OUTPUT_POWER_SENSOR: VICTRON,
    CONF_VICTRON_OTHER_POWER_SENSORS: [GARAGE],
    # The tests below confirm in three ticks; the shipped default is 10.
    CONF_SUPPLY_CONFIRM_SAMPLES: 3,
}
ENTRY_ID = "supply_entry"
STORE_KEY = f"{DOMAIN}_{ENTRY_ID}_supply"


def _t(i: int) -> datetime:
    return T0 + i * STEP


def _detector(n: int = 3, **kwargs) -> SupplyDetector:
    return SupplyDetector(300.0, 0.5, n, **kwargs)


# Samples as (apartment W, Victron net W): on the Victron the net output covers
# the apartment; on PPE 2 it does not; below 300 W the sample decides nothing.
ON_VICTRON = (1000.0, 1050.0)
ON_METER_2 = (1000.0, 120.0)
IDLE = (150.0, 0.0)


# ---------------------------------------------------------------------------
# The rule
# ---------------------------------------------------------------------------


def test_rule_decides_only_above_the_minimum_and_below_the_share() -> None:
    assert classify_sample(299.9, 0.0, 300.0, 0.5) is None
    assert classify_sample(300.0, 150.0, 300.0, 0.5) == SUPPLY_VICTRON  # r == share * a
    assert classify_sample(300.0, 149.9, 300.0, 0.5) == SUPPLY_METER_2
    assert classify_sample(2000.0, 2500.0, 300.0, 0.5) == SUPPLY_VICTRON


def test_rule_is_proportional_not_an_absolute_margin() -> None:
    """Live 2026-10: ``r >= a - 100 W`` gave 6-14 false meter-2 episodes in 14 days.

    The apartment meter and the Victron disagree by a few percent at ~2 kW and
    do not report simultaneously; 1850 W against 2000 W is the Victron.
    """
    assert classify_sample(2000.0, 1850.0, 300.0, 0.5) == SUPPLY_VICTRON
    assert classify_sample(2000.0, 900.0, 300.0, 0.5) == SUPPLY_METER_2
    # The same 150 W gap at 500 W is a real difference of 30 %, still the Victron.
    assert classify_sample(500.0, 350.0, 300.0, 0.5) == SUPPLY_VICTRON


def test_thresholds_are_parameters_not_constants() -> None:
    # 500 W apartment, 350 W net: victron with a share of 0.5, meter_2 with 0.8.
    assert classify_sample(500.0, 350.0, 300.0, 0.5) == SUPPLY_VICTRON
    assert classify_sample(500.0, 350.0, 300.0, 0.8) == SUPPLY_METER_2
    assert classify_sample(500.0, 350.0, 600.0, 0.5) is None


# ---------------------------------------------------------------------------
# The detector
# ---------------------------------------------------------------------------


def test_first_confirmation_from_unknown_has_no_lower_bound() -> None:
    detector = _detector()
    assert detector.observe(_t(0), *ON_METER_2) is None
    assert detector.observe(_t(1), *ON_METER_2) is None
    assert detector.state is None
    transition = detector.observe(_t(2), 1100.0, 90.0)

    assert transition == SupplyTransition(
        state=SUPPLY_METER_2,
        at=_t(0),
        not_before=None,
        apartment_w=1100.0,
        victron_net_w=90.0,
        source=SUPPLY_SOURCE_LIVE,
    )
    assert detector.state == SUPPLY_METER_2
    assert detector.streak == 0


def test_from_unknown_a_streak_of_the_other_state_starts_over() -> None:
    detector = _detector()
    detector.observe(_t(0), *ON_VICTRON)
    detector.observe(_t(1), *ON_VICTRON)
    assert detector.observe(_t(2), *ON_METER_2) is None
    assert detector.observe(_t(3), *ON_METER_2) is None
    transition = detector.observe(_t(4), *ON_METER_2)
    assert transition is not None
    assert (transition.state, transition.at, transition.not_before) == (
        SUPPLY_METER_2,
        _t(2),
        None,
    )


def test_missing_input_breaks_the_streak() -> None:
    detector = _detector(state=SUPPLY_VICTRON, last_decisive=_t(-1))
    detector.observe(_t(0), *ON_METER_2)
    detector.observe(_t(1), *ON_METER_2)
    assert detector.observe(_t(2), None, 120.0) is None
    assert detector.streak == 0
    detector.observe(_t(3), *ON_METER_2)
    assert detector.observe(_t(4), 1000.0, None) is None
    assert detector.streak == 0
    detector.observe(_t(5), *ON_METER_2)
    detector.observe(_t(6), *ON_METER_2)
    transition = detector.observe(_t(7), *ON_METER_2)
    assert transition is not None and transition.at == _t(5)
    assert transition.not_before == _t(-1)


def test_undecided_samples_neither_count_nor_reset() -> None:
    detector = _detector(state=SUPPLY_VICTRON, last_decisive=_t(-1))
    detector.observe(_t(0), *ON_METER_2)
    detector.observe(_t(1), *ON_METER_2)
    assert detector.observe(_t(2), *IDLE) is None
    assert detector.observe(_t(3), *IDLE) is None
    assert detector.streak == 2
    assert detector.state == SUPPLY_VICTRON
    transition = detector.observe(_t(4), *ON_METER_2)
    assert transition is not None and transition.at == _t(0)


def test_undecided_zone_keeps_the_confirmed_state() -> None:
    detector = _detector(state=SUPPLY_METER_2, last_decisive=_t(-1))
    for i in range(20):
        assert detector.observe(_t(i), *IDLE) is None
    assert detector.state == SUPPLY_METER_2


def test_agreeing_sample_resets_the_streak_and_moves_not_before() -> None:
    detector = _detector(state=SUPPLY_VICTRON, last_decisive=_t(-10))
    detector.observe(_t(0), *ON_VICTRON)
    detector.observe(_t(1), *ON_METER_2)
    detector.observe(_t(2), *ON_METER_2)
    assert detector.observe(_t(3), *ON_VICTRON) is None
    assert detector.streak == 0
    detector.observe(_t(4), *IDLE)
    detector.observe(_t(5), *ON_METER_2)
    detector.observe(_t(6), *ON_METER_2)
    transition = detector.observe(_t(7), *ON_METER_2)
    assert transition is not None
    assert (transition.not_before, transition.at) == (_t(3), _t(5))


def test_after_a_confirmation_the_last_streak_sample_bounds_the_next_switch() -> None:
    detector = _detector()
    for i in range(3):
        detector.observe(_t(i), *ON_METER_2)
    for i in range(3, 6):
        transition = detector.observe(_t(i), *ON_VICTRON)
    assert transition is not None
    assert (transition.state, transition.not_before, transition.at) == (
        SUPPLY_VICTRON,
        _t(2),
        _t(3),
    )


@pytest.mark.parametrize("n", [1, 2, 5])
def test_number_of_confirming_samples_is_configurable(n: int) -> None:
    detector = _detector(n)
    for i in range(n - 1):
        assert detector.observe(_t(i), *ON_METER_2) is None
    transition = detector.observe(_t(n - 1), *ON_METER_2)
    assert transition is not None and transition.at == _t(0)


def test_confirming_samples_must_be_positive() -> None:
    with pytest.raises(ValueError):
        _detector(0)


# ---------------------------------------------------------------------------
# The stored transition
# ---------------------------------------------------------------------------


def _transition(**overrides) -> SupplyTransition:
    values = {
        "state": SUPPLY_METER_2,
        "at": _t(4),
        "not_before": _t(1),
        "apartment_w": 1000.0,
        "victron_net_w": 120.0,
        "source": SUPPLY_SOURCE_LIVE,
    }
    values.update(overrides)
    return SupplyTransition(**values)


@pytest.mark.parametrize("not_before", [_t(1), None])
def test_transition_round_trips(not_before) -> None:
    transition = _transition(not_before=not_before)
    assert SupplyTransition.from_dict(json.loads(json.dumps(transition.to_dict()))) == transition


def test_transition_without_not_before_decodes_as_first_confirmation() -> None:
    data = _transition().to_dict()
    del data["not_before"]
    assert SupplyTransition.from_dict(data).not_before is None


def test_transition_times_come_back_in_utc() -> None:
    data = _transition().to_dict()
    data["at"] = "2026-10-17T14:02:00+02:00"
    assert SupplyTransition.from_dict(data).at == _t(4)
    assert SupplyTransition.from_dict(data).at.tzinfo == timezone.utc


@pytest.mark.parametrize(
    "change",
    [
        {"state": "unknown"},
        {"state": None},
        {"at": None},
        {"at": "yesterday"},
        {"at": "2026-10-17T12:00:00"},  # naive: no way to tell when it was
        {"not_before": "soon"},
        {"apartment_w": None},
        {"apartment_w": "a lot"},
        {"victron_net_w": float("nan")},
        {"source": None},
        {"source": "guess"},
    ],
)
def test_unusable_transition_raises_value_error(change: dict) -> None:
    data = {**_transition().to_dict(), **change}
    with pytest.raises(ValueError):
        SupplyTransition.from_dict(data)


def test_transition_that_is_not_a_mapping_raises_value_error() -> None:
    with pytest.raises(ValueError):
        SupplyTransition.from_dict(["meter_2"])  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "stored",
    [
        [],
        {},
        {"transitions": None},
        {"transitions": [_transition().to_dict(), {"state": "meter_2"}]},
        # Append-only: a log going back in time has been tampered with.
        {"transitions": [_transition(at=_t(5)).to_dict(), _transition(at=_t(1)).to_dict()]},
    ],
)
def test_one_unusable_part_makes_the_whole_log_undecodable(stored) -> None:
    with pytest.raises(ValueError):
        decode_log(stored)


# ---------------------------------------------------------------------------
# The module (sampling, log, store)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _no_history(supply_recorder):
    """The recorder holds no history of the inputs unless a test says otherwise."""
    supply_recorder()


def _coordinator(config: dict) -> SimpleNamespace:
    unloads: list = []
    warnings: list[str] = []
    return SimpleNamespace(
        config={**DEFAULTS, **config},
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


async def _module(hass: HomeAssistant, config: dict | None = None) -> SupplyModule:
    coordinator = _coordinator(SUPPLY_CONFIG if config is None else config)
    module = SupplyModule(hass, coordinator)
    await module.async_setup()
    if module._replay_task is not None:
        await module._replay_task  # live sampling starts once the replay is done
    return module


def _set_power(
    hass: HomeAssistant, apartment, victron, garage, unit: str = "W"
) -> None:
    for entity_id, value in ((APARTMENT, apartment), (VICTRON, victron), (GARAGE, garage)):
        hass.states.async_set(entity_id, str(value), {"unit_of_measurement": unit})


def _stored_log(*transitions: SupplyTransition) -> dict:
    return {
        "version": 1,
        "minor_version": 1,
        "key": STORE_KEY,
        "data": {"transitions": [t.to_dict() for t in transitions]},
    }


async def test_module_confirms_from_sampled_states_and_persists_the_log(
    hass: HomeAssistant, hass_storage
) -> None:
    module = await _module(hass)
    assert module.state is None
    # Apartment 1000 W, Victron 600 W of which the garage takes 500 W: the
    # Victron feeds 100 W of the apartment's 1000 W → PPE 2.
    _set_power(hass, 1000, 600, 500)
    for i in range(3):
        await module._async_tick(_t(i))

    assert module.state == SUPPLY_METER_2
    assert module.since == _t(0)
    (stored,) = hass_storage[STORE_KEY]["data"]["transitions"]
    assert SupplyTransition.from_dict(stored) == SupplyTransition(
        state=SUPPLY_METER_2,
        at=_t(0),
        not_before=None,
        apartment_w=1000.0,
        victron_net_w=100.0,
        source=SUPPLY_SOURCE_LIVE,
    )


async def test_module_reads_kw_and_treats_unusable_states_as_missing(
    hass: HomeAssistant,
) -> None:
    module = await _module(hass, {**SUPPLY_CONFIG, CONF_SUPPLY_CONFIRM_SAMPLES: 10})
    _set_power(hass, 1.0, 0.6, 0.5, unit="kW")
    await module._async_tick(_t(0))
    await module._async_tick(_t(1))
    assert module.detector.streak == 2

    for state in ("unavailable", "unknown", "n/a"):
        _set_power(hass, 1.0, 0.6, 0.5, unit="kW")
        await module._async_tick(_t(0))
        assert module.detector.streak >= 1
        hass.states.async_set(GARAGE, state, {"unit_of_measurement": "kW"})
        await module._async_tick(_t(1))
        assert module.detector.streak == 0, state

    # A power sensor in an unknown unit is not guessed into watts.
    _set_power(hass, 1.0, 0.6, 0.5, unit="kW")
    await module._async_tick(_t(0))
    hass.states.async_set(APARTMENT, "1000", {"unit_of_measurement": "VA"})
    await module._async_tick(_t(1))
    assert module.detector.streak == 0
    assert module.state is None


async def test_restart_restores_the_state_from_the_stored_log(
    hass: HomeAssistant, hass_storage
) -> None:
    first = _transition(state=SUPPLY_VICTRON, at=_t(-100), not_before=None)
    second = _transition(state=SUPPLY_METER_2, at=_t(-50), not_before=_t(-60))
    hass_storage[STORE_KEY] = _stored_log(first, second)

    module = await _module(hass)
    assert module.state == SUPPLY_METER_2
    assert module.since == _t(-50)
    assert module.transitions == [first, second]

    # Back on the Victron: the switch is bounded by the restored confirmation,
    # the last moment the log knows the apartment was on PPE 2.
    _set_power(hass, 1000, 1500, 400)
    for i in range(3):
        await module._async_tick(_t(i))
    assert module.state == SUPPLY_VICTRON
    stored = hass_storage[STORE_KEY]["data"]["transitions"]
    assert [SupplyTransition.from_dict(t) for t in stored[:2]] == [first, second]
    third = SupplyTransition.from_dict(stored[2])
    assert (third.state, third.not_before, third.at) == (SUPPLY_VICTRON, _t(-50), _t(0))


async def test_undecodable_store_is_moved_aside_and_never_overwritten(
    hass: HomeAssistant, hass_storage, tmp_path: Path, monkeypatch, caplog
) -> None:
    """Data rule: never start empty after a failed read."""
    monkeypatch.setattr(hass.config, "config_dir", str(tmp_path))
    bad = {"transitions": [{"state": "meter_2", "at": "not a time"}]}
    hass_storage[STORE_KEY] = {"version": 1, "minor_version": 1, "key": STORE_KEY, "data": bad}
    storage_dir = tmp_path / ".storage"
    storage_dir.mkdir()
    (storage_dir / STORE_KEY).write_text(json.dumps(hass_storage[STORE_KEY]), encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        module = await _module(hass)

    assert not (storage_dir / STORE_KEY).exists()
    (aside,) = storage_dir.glob(f"{STORE_KEY}.corrupt-*")
    assert json.loads(aside.read_text(encoding="utf-8"))["data"] == bad
    assert any("corrupt" in r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING)
    assert module.store_error
    assert module.coordinator.warnings
    # Inert until the log is sorted out: no sampling, no state, no save.
    assert module.coordinator.unloads == []
    assert module.state is None
    _set_power(hass, 1000, 600, 500)
    for i in range(3):
        await module._async_tick(_t(i))
    assert module.state is None
    assert hass_storage[STORE_KEY]["data"] == bad


@pytest.mark.parametrize(
    "config",
    [
        {},
        {CONF_APARTMENT_POWER_SENSOR: APARTMENT, CONF_VICTRON_OUTPUT_POWER_SENSOR: VICTRON},
        {**SUPPLY_CONFIG, CONF_VICTRON_OTHER_POWER_SENSORS: []},
    ],
)
async def test_module_is_inert_until_all_three_sensor_keys_are_set(
    hass: HomeAssistant, hass_storage, config: dict
) -> None:
    hass_storage[STORE_KEY] = _stored_log(_transition())
    module = await _module(hass, config)
    assert not module.enabled
    assert module.state is None and module.since is None
    assert module.coordinator.unloads == []  # no tick subscribed


async def test_tick_subscription_is_released_on_unload(hass: HomeAssistant) -> None:
    module = await _module(hass)
    unsub = module.coordinator.unloads[0]  # the tick, registered first
    unsub()
    _set_power(hass, 1000, 600, 500)
    for i in range(1, 4):
        async_fire_time_changed(hass, dt_util.utcnow() + i * timedelta(seconds=31))
        await hass.async_block_till_done()
    assert module.detector.streak == 0


async def test_thresholds_come_from_the_options(hass: HomeAssistant) -> None:
    module = await _module(
        hass,
        {
            **SUPPLY_CONFIG,
            CONF_SUPPLY_MIN_APARTMENT_W: 1500.0,
            CONF_SUPPLY_VICTRON_SHARE: 0.9,
            CONF_SUPPLY_CONFIRM_SAMPLES: 1,
        },
    )
    _set_power(hass, 1000, 600, 500)  # below 1500 W: undecided
    await module._async_tick(_t(0))
    assert module.state is None
    _set_power(hass, 2000, 600, 500)
    await module._async_tick(_t(1))
    assert module.state == SUPPLY_METER_2


# ---------------------------------------------------------------------------
# The entity
# ---------------------------------------------------------------------------


async def test_entity_is_unknown_until_confirmed_then_shows_state_and_evidence(
    hass: HomeAssistant,
) -> None:
    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    _set_power(hass, 200, 600, 500)  # apartment idle: nothing to decide
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc", **SUPPLY_CONFIG},
        title="PowerPilot",
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done(wait_background_tasks=True)

    entity_id = er.async_get(hass).async_get_entity_id(
        "sensor", DOMAIN, f"{entry.entry_id}_{SENSOR_APARTMENT_SUPPLY_ACTUAL}"
    )
    assert entity_id
    state = hass.states.get(entity_id)
    assert state.state == "unknown"
    assert state.attributes["since"] is None
    assert state.attributes["streak"] == 0
    assert state.attributes["options"] == [SUPPLY_VICTRON, SUPPLY_METER_2]

    _set_power(hass, 1000, 600, 500)
    now = dt_util.utcnow()
    for i in range(1, 3):
        async_fire_time_changed(hass, now + i * timedelta(seconds=31))
        await hass.async_block_till_done()
    # The streak shows up between plan refreshes, not only on them.
    assert hass.states.get(entity_id).attributes["streak"] == 2
    async_fire_time_changed(hass, now + 3 * timedelta(seconds=31))
    await hass.async_block_till_done()

    state = hass.states.get(entity_id)
    assert state.state == SUPPLY_METER_2
    attributes = state.attributes
    assert dt_util.parse_datetime(attributes["since"]) is not None
    assert attributes["not_before"] is None
    assert attributes["apartment_w"] == 1000.0
    assert attributes["victron_net_w"] == 100.0
    assert attributes["source"] == SUPPLY_SOURCE_LIVE
    assert attributes["streak"] == 0
    assert attributes["min_apartment_w"] == 300.0
    assert attributes["victron_share"] == 0.5
    assert attributes["confirm_samples"] == 3

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_supply_sensors_never_hold_up_the_planner_setup(
    hass: HomeAssistant,
) -> None:
    """Supply detection is an addition to the planner, not one of its inputs.

    An offline socket or apartment meter must not keep the whole integration —
    and with it the inverter plan — in setup retry. The module already treats
    an unavailable input as a missing sample.
    """
    from custom_components.powerpilot import _unready_inputs

    hass.states.async_set("sensor.soc", "55", {"unit_of_measurement": "%"})
    _set_power(hass, 200, 600, 500)
    hass.states.async_set(GARAGE, "unavailable", {})
    hass.states.async_remove(VICTRON)
    entry = MockConfigEntry(
        domain=DOMAIN, data={**DEFAULTS, CONF_SOC_SENSOR: "sensor.soc", **SUPPLY_CONFIG}
    )
    assert _unready_inputs(hass, entry) == []
