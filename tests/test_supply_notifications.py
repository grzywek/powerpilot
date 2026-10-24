"""The switch notification and the switching-rule numbers (stage 3, slice 3).

One Home Assistant persistent notification per cycle id and direction while
``switch_required`` is on, dismissed when it turns off, never repeated for
the same pair — also not after a restart (the pairs live in the coordinator's
snapshot store). The cycle threshold and minimum stay numbers write the
options of the supply step.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest
from homeassistant.core import HomeAssistant
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot import coordinator as coordinator_module
from custom_components.powerpilot.const import (
    CONF_SUPPLY_CYCLE_THRESHOLD_PLN,
    CONF_SUPPLY_MIN_STAY_HOURS,
    DEFAULTS,
    DOMAIN,
    SUPPLY_METER_2,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.coordinator import PowerPilotCoordinator
from custom_components.powerpilot.number import (
    ApartmentCycleThresholdNumber,
    ApartmentMinStayNumber,
)
from custom_components.powerpilot.supply_cycles import (
    REASON_NOT_ACTIVE,
    SwitchNotices,
    recommendation,
    settle_cycles,
)
from tests.test_optimizer_supply import _planning
from tests.test_supply_cycles import NOW, _weak


def test_a_notice_is_created_once_and_dismissed_when_no_longer_required() -> None:
    notices = SwitchNotices()
    a = ("2026-10-17T14:00:00+00:00", SUPPLY_METER_2)
    assert notices.update(a) == (False, True)
    assert notices.update(a) == (False, False)
    assert notices.update(None) == (True, False)
    assert notices.update(None) == (False, False)
    assert notices.update(a) == (False, False)  # never repeated
    b = ("2026-10-17T14:00:00+00:00", SUPPLY_VICTRON)  # same cycle, the return
    assert notices.update(b) == (False, True)
    c = ("2026-10-17T18:00:00+00:00", SUPPLY_METER_2)
    assert notices.update(c) == (True, True)  # another cycle replaces it


def test_stored_notices_round_trip_and_garbage_is_refused() -> None:
    notices = SwitchNotices([("x", SUPPLY_METER_2)])
    assert SwitchNotices.from_list(notices.to_list()).notified == [("x", SUPPLY_METER_2)]
    assert SwitchNotices.from_list(None).notified == []
    for bad in ({"x": 1}, [["x", SUPPLY_METER_2]], [{"id": "x", "direction": "up"}]):
        with pytest.raises(ValueError):
            SwitchNotices.from_list(bad)


class _Store:
    def __init__(self) -> None:
        self.saves: list[float] = []

    def async_delay_save(self, _payload, delay: float) -> None:
        self.saves.append(delay)


def _fake_coordinator(plan, planning, state: dict, notices: SwitchNotices):
    fake = SimpleNamespace(
        hass=object(),
        entry=SimpleNamespace(entry_id="entry"),
        switch_notices=notices,
        _snapshot_store=_Store(),
        _snapshots_payload=lambda: None,
        logged=[],
        snapshots=SimpleNamespace(to_dict=lambda: {}),
        plan_asks=SimpleNamespace(to_dict=lambda: {}),
        _battery_energy_cost=0.0,
        _last_snapshot_hour=None,
    )
    fake.supply_recommendation = lambda plan=None: recommendation(
        plan, planning, state["actual"], state["since"], NOW
    )
    fake.log_info = lambda *args, **kwargs: fake.logged.append(args)
    return fake


@pytest.fixture
def notifications(monkeypatch):
    calls: list[tuple] = []
    monkeypatch.setattr(
        coordinator_module.persistent_notification,
        "async_create",
        lambda hass, message, title=None, notification_id=None: calls.append(
            ("create", notification_id, title, message)
        ),
    )
    monkeypatch.setattr(
        coordinator_module.persistent_notification,
        "async_dismiss",
        lambda hass, notification_id: calls.append(("dismiss", notification_id)),
    )
    return calls


def test_the_notification_follows_switch_required_and_survives_a_restart(
    notifications,
) -> None:
    planning = _planning()
    plan = settle_cycles(_weak(), planning)
    state = {"actual": SUPPLY_VICTRON, "since": planning.last_transition_at}
    fake = _fake_coordinator(plan, planning, state, SwitchNotices())
    sync = PowerPilotCoordinator._sync_switch_notification
    notification_id = f"{DOMAIN}_apartment_switch_entry"

    sync(fake, plan)
    assert len(notifications) == 1
    kind, nid, title, message = notifications[0]
    assert (kind, nid) == ("create", notification_id)
    assert "licznik 2" in title and message.startswith("Przełącz mieszkanie na licznik 2")
    assert fake._snapshot_store.saves == [1.0]
    sync(fake, plan)  # every refresh while still required
    assert len(notifications) == 1

    state["actual"] = SUPPLY_METER_2  # detection confirms the switch
    sync(fake, plan)
    assert notifications[-1] == ("dismiss", notification_id)
    state["actual"] = SUPPLY_VICTRON  # back again, the same cycle still planned
    sync(fake, plan)
    assert len(notifications) == 2  # not repeated

    # Restart: the notified pairs come back from the snapshot store's payload.
    payload = PowerPilotCoordinator._snapshots_payload(fake)
    restarted = _fake_coordinator(
        plan, planning, state, SwitchNotices.from_list(payload["supply_notified"])
    )
    sync(restarted, plan)
    assert len(notifications) == 2


def _supply_listener(planning, state: str | None, since):
    """The coordinator's supply-signal handler around a fake, and what it did."""
    did: dict[str, list] = {"synced": [], "refreshes": []}

    async def _refresh() -> None:
        did["refreshes"].append(True)

    def _create_task(coro) -> None:
        coro.close()
        did["refreshes"].append(True)

    fake = SimpleNamespace(
        data=None,
        supply_planning=planning,
        supply=SimpleNamespace(state=state, since=since),
        _pending_triggers=[],
        _sync_switch_notification=lambda plan: did["synced"].append(plan),
        async_request_refresh=_refresh,
        hass=SimpleNamespace(async_create_task=_create_task),
    )
    PowerPilotCoordinator._async_on_supply_changed(fake)
    return fake, did


def test_a_plan_made_under_another_supply_is_replanned_at_once() -> None:
    """Live 2026-10-18 01:04: a reload planned while the replay still ran.

    The plan saw the supply as unknown and modelled no switching; the replay
    then confirmed meter 2, and the recommendation stayed unknown until the
    next hour. Likewise the confirmation at 18:52 of the switch at 17:58 was
    planned only at 19:00.
    """
    since = NOW - timedelta(hours=7)
    made_unknown = _planning(actual=None)
    fake, did = _supply_listener(made_unknown, SUPPLY_METER_2, since)
    assert did["synced"] == [None]
    assert did["refreshes"] == [True]
    assert fake._pending_triggers == ["zasilanie mieszkania"]

    # A rule rebuild re-dates the same state: the plan's cycle starts move.
    made_meter_2 = _planning(actual=SUPPLY_METER_2, last_transition_at=since)
    _, did = _supply_listener(made_meter_2, SUPPLY_METER_2, since - timedelta(minutes=3))
    assert did["refreshes"] == [True]


def test_a_streak_tick_or_no_plan_yet_replans_nothing() -> None:
    since = NOW - timedelta(hours=7)
    made = _planning(actual=SUPPLY_METER_2, last_transition_at=since)
    fake, did = _supply_listener(made, SUPPLY_METER_2, since)
    assert did["synced"] == [None]  # the notification still follows
    assert did["refreshes"] == []
    assert fake._pending_triggers == []

    _, did = _supply_listener(None, SUPPLY_METER_2, since)
    assert did["refreshes"] == []


# ------------------------------------------------------------------ numbers


@pytest.mark.parametrize(
    ("cls", "option", "value", "stored"),
    [
        (ApartmentCycleThresholdNumber, CONF_SUPPLY_CYCLE_THRESHOLD_PLN, 3.5, 3.5),
        (ApartmentMinStayNumber, CONF_SUPPLY_MIN_STAY_HOURS, 4.0, 4),
    ],
)
async def test_the_switching_rule_numbers_write_the_options(
    hass: HomeAssistant, cls, option: str, value: float, stored
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={}, options={"other": 1}, title="PowerPilot")
    entry.add_to_hass(hass)
    coordinator = SimpleNamespace(
        config={**DEFAULTS, **entry.options},
        supply_recommendation=lambda: recommendation(None, None, None, None, NOW),
    )
    number = cls(coordinator, entry)
    number.hass = hass
    assert number.native_value == DEFAULTS[option]
    assert number.extra_state_attributes == {"reason": REASON_NOT_ACTIVE}

    await number.async_set_native_value(value)
    assert entry.options == {"other": 1, option: stored}
    assert type(entry.options[option]) is type(stored)
