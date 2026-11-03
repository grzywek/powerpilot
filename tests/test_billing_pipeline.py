"""The price cap wired end-to-end: config → coordinator → plan → entities."""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.powerpilot.const import (
    BINARY_TARCZA_PPE1_CAP_BINDS,
    CONF_EV_ENABLED,
    CONF_EV_ENERGY_ADDED_SENSOR,
    CONF_PPE1_IMPORT_SENSORS,
    CONF_SOC_SENSOR,
    CONF_TARCZA_TERMS,
    DEFAULTS,
    DOMAIN,
    PPE_1,
    PRICE_TYPE_CERTAIN,
    SENSOR_TARCZA_PPE1_AVERAGE,
    SENSOR_TARCZA_PPE1_DISCOUNT,
    SENSOR_TARCZA_PPE1_ENERGY,
    SENSOR_TARCZA_PPE1_FORECAST_AVERAGE,
    SENSOR_TARCZA_PPE2_AVERAGE,
    SENSOR_TARCZA_PPE2_ENERGY,
    SUPPLY_VICTRON,
)
from custom_components.powerpilot.models import (
    BillingPeriod,
    CapOutlook,
    CapScenario,
    ESSRequest,
    Forecast,
    HourSlot,
    MonthTail,
    SupplyPlanning,
)
from custom_components.powerpilot.modules.billing import month_bounds
from custom_components.powerpilot.modules.ev import _local_day_hours


TERMS = {
    "id": "t",
    "name": "Tarcza Pstryk 2026",
    "valid_from": "2025-01-01",
    "valid_to": "2099-12-31",
    "limit_kwh": 0.61,
    "limit_includes_vat": True,
    "handling_fee_included": True,
}


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
    return entry, hass.data[DOMAIN][entry.entry_id]


def _state(hass: HomeAssistant, entry, platform: str, key: str):
    entity_id = er.async_get(hass).async_get_entity_id(
        platform, DOMAIN, f"{entry.entry_id}_{key}"
    )
    assert entity_id, f"{platform}.{key} not registered"
    return hass.states.get(entity_id)


async def test_cap_disabled_by_default_leaves_the_plan_without_billing(
    hass: HomeAssistant,
) -> None:
    entry, coordinator = await _setup(hass)
    assert coordinator.data.billing == []
    for key in (
        SENSOR_TARCZA_PPE1_ENERGY,
        SENSOR_TARCZA_PPE1_AVERAGE,
        SENSOR_TARCZA_PPE1_FORECAST_AVERAGE,
        SENSOR_TARCZA_PPE1_DISCOUNT,
    ):
        assert _state(hass, entry, "sensor", key).state == "unknown"
    assert _state(hass, entry, "binary_sensor", BINARY_TARCZA_PPE1_CAP_BINDS).state == "unknown"


async def test_enabled_without_meters_reports_why_instead_of_guessing(
    hass: HomeAssistant,
) -> None:
    entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [TERMS]})
    outcomes = coordinator.data.billing
    assert len(outcomes) == 1
    outcome = outcomes[0]
    assert not outcome.available
    assert "liczników" in outcome.unavailable_reason
    assert outcome.energy_kwh == 0.0
    # The battery was planned without the cap and the entities say so.
    state = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_FORECAST_AVERAGE)
    assert state.state == "unknown"
    assert "liczników" in state.attributes["unavailable_reason"]
    binds = _state(hass, entry, "binary_sensor", BINARY_TARCZA_PPE1_CAP_BINDS)
    assert binds.state == "unknown"
    status = coordinator.billing_status()
    assert status["enabled"] is True
    assert status["outcomes"][0]["available"] is False


async def test_enabled_with_meters_but_unreadable_recorder_reports_the_error(
    hass: HomeAssistant,
) -> None:
    """No recorder in this test harness → the meters cannot be read at all.

    That must surface as the reason (not as a zero-import month): the module
    records it, the plan carries it and the entities show it.
    """
    hass.states.async_set(
        "sensor.acin",
        "12.5",
        {"unit_of_measurement": "kWh", "state_class": "total_increasing"},
    )
    entry, coordinator = await _setup(
        hass,
        **{CONF_TARCZA_TERMS: [TERMS], CONF_PPE1_IMPORT_SENSORS: ["sensor.acin"]},
    )
    outcome = coordinator.data.billing[0]
    assert not outcome.available
    assert "błąd odczytu liczników" in outcome.unavailable_reason
    assert coordinator.billing.periods == {}
    assert coordinator.billing.last_error is not None
    energy = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_ENERGY)
    assert energy.state == "unknown"
    assert any("błąd odczytu" in r for r in energy.attributes["reasons"])


async def test_ppe2_entities_say_why_without_supply_detection(hass: HomeAssistant) -> None:
    entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [TERMS]})
    for key in (SENSOR_TARCZA_PPE2_ENERGY, SENSOR_TARCZA_PPE2_AVERAGE):
        state = _state(hass, entry, "sensor", key)
        assert state.state == "unknown"
        assert any("wykrywanie zasilania" in r for r in state.attributes["reasons"])
    # PPE 1's entities and the plan carry only PPE 1's reasons.
    ppe1 = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_ENERGY)
    assert not any("zasilania" in r for r in ppe1.attributes["reasons"])
    assert "zasilania" not in coordinator.data.billing[0].unavailable_reason


async def test_cap_outside_its_validity_is_inactive(hass: HomeAssistant) -> None:
    ended = {**TERMS, "valid_from": "2019-01-01", "valid_to": "2019-12-31"}
    entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [ended]})
    assert coordinator.data.billing == []
    state = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_ENERGY)
    assert state.state == "unknown"
    assert any("nieaktywna" in r for r in state.attributes["reasons"])


async def test_unusable_terms_reach_the_entities_instead_of_a_silent_no_cap(
    hass: HomeAssistant,
) -> None:
    partial = {**TERMS, "valid_from": "2025-01-15"}
    entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [partial]})
    # No month can be settled under broken terms, so no outcome is invented…
    assert coordinator.data.billing == []
    # …and the reason is on the entities and in the status, not swallowed.
    state = _state(hass, entry, "sensor", SENSOR_TARCZA_PPE1_FORECAST_AVERAGE)
    assert any("pełnych miesięcy" in r for r in state.attributes["reasons"])
    assert "pełnych miesięcy" in coordinator.billing_status()["config_error"]


async def test_a_cap_starting_next_month_needs_nothing_observed_this_month(
    hass: HomeAssistant,
) -> None:
    """Late December with only the 2027 edition: January is still planned.

    The running month has no cap, so there is no month-to-date position to
    read — that must not block settling the next, capped, month.
    """
    now = dt_util.now()
    _, next_month = month_bounds(now)
    _, month_after = month_bounds(next_month)
    starts_next_month = {
        **TERMS,
        "valid_from": dt_util.as_local(next_month).date().isoformat(),
        "valid_to": (dt_util.as_local(month_after).date() - timedelta(days=1)).isoformat(),
    }
    _entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [starts_next_month]})
    assert coordinator.billing.periods == {}  # nothing to observe this month

    # A horizon from now to the end of the capped month (no tail needed).
    hour = now.replace(minute=0, second=0, microsecond=0)
    slots = []
    while hour < month_after:
        slots.append(HourSlot(start=hour, buy_price=0.5, cap_price_kwh=0.5))
        hour += timedelta(hours=1)
    periods, tail, reason = await coordinator._billing_inputs(Forecast(slots=slots), None)
    assert reason is None
    assert [p.month_start for p in periods] == [next_month]
    assert periods[0].energy_kwh == 0.0
    assert tail.slots == []


async def _next_month_with_a_tail(hass: HomeAssistant, series_days: int):
    """A cap starting next month, a horizon ending a day into it, the EV on.

    Returns the coordinator (its EV charging profile read from ``series_days``
    complete days of 1 kWh every hour) and the horizon.
    """
    now = dt_util.now()
    _, next_month = month_bounds(now)
    _, month_after = month_bounds(next_month)
    starts_next_month = {
        **TERMS,
        "valid_from": dt_util.as_local(next_month).date().isoformat(),
        "valid_to": (dt_util.as_local(month_after).date() - timedelta(days=1)).isoformat(),
    }
    hass.states.async_set("sensor.ev_added", "3.2", {"unit_of_measurement": "kWh"})
    _entry, coordinator = await _setup(
        hass,
        **{
            CONF_TARCZA_TERMS: [starts_next_month],
            CONF_EV_ENABLED: True,
            CONF_EV_ENERGY_ADDED_SENSOR: "sensor.ev_added",
        },
    )

    def priced(start, end) -> list[HourSlot]:
        slots = []
        while start < end:
            slots.append(HourSlot(start=start, buy_price=0.5, cap_price_kwh=0.5))
            start += timedelta(hours=1)
        return slots

    horizon_end = next_month + timedelta(days=1)
    coordinator.forecast_builder.build_tail = lambda start, end: Forecast(
        slots=priced(start, end)
    )
    today = dt_util.as_local(now).date()
    series = {
        hour: 1.0
        for back in range(1, series_days + 1)
        for hour in _local_day_hours(today - timedelta(days=back))
    }

    async def reader(entity_id, start, end):
        return {h: v for h, v in series.items() if start <= h < end}

    coordinator.consumption.async_range_kwh = reader
    coordinator.ev._charge_profile_through = None
    await coordinator.ev._learn_charge_profile(now)
    hour = now.replace(minute=0, second=0, microsecond=0)
    return coordinator, Forecast(slots=priced(hour, horizon_end)), month_after - horizon_end


async def test_the_tail_ev_follows_the_learned_charging_on_the_grid_side(
    hass: HomeAssistant,
) -> None:
    coordinator, horizon, tail_length = await _next_month_with_a_tail(hass, 28)
    periods, tail, reason = await coordinator._billing_inputs(
        horizon, SimpleNamespace(charge_efficiency=0.8)
    )
    assert reason is None and periods
    assert len(tail.slots) == int(tail_length / timedelta(hours=1))
    assert tail.ev_grid_kwh == [1.25] * len(tail.slots)


async def test_too_few_days_of_ev_charging_make_the_cap_unavailable(
    hass: HomeAssistant,
) -> None:
    """2026-11-02: without the car's real charging hours the tail priced
    October's EV at 0.31 against 0.56 on the meter — no profile, no cap."""
    coordinator, horizon, _ = await _next_month_with_a_tail(hass, 10)
    periods, tail, reason = await coordinator._billing_inputs(horizon, None)
    assert periods is None and tail is None
    assert "sensor.ev_added" in reason
    assert "10 z 28" in reason


async def test_each_refresh_judges_ppe1_on_the_replayed_weeks(hass: HomeAssistant) -> None:
    """The scenarios come from the price archive's certain weeks under the
    month's edition; one relaxed solve per usable scenario (ADR 003)."""
    coordinator, horizon, _ = await _next_month_with_a_tail(hass, 28)
    periods, tail, reason = await coordinator._billing_inputs(horizon, None)
    assert reason is None
    calls: list[list[float]] = []

    def cap_regimes(forecast, battery, ev_request, ess_request=None, billing=None, tail=None, supply=None):
        calls.append([slot.buy_price for slot in tail.slots[:2]])
        return [(billing[0], 10.0, 8.0, len(calls) % 2 == 1)]

    optimizer = SimpleNamespace(cap_regimes=cap_regimes)
    # An empty archive: every scenario is dropped, none is solved.
    [outlook] = coordinator._cap_outlooks(
        optimizer, horizon, None, None, ESSRequest(), periods, tail, None
    )
    assert calls == []
    assert outlook.bind_probability is None
    assert "żaden z 4" in outlook.unavailable_reason

    # Four weeks of certain prices behind every tail hour.
    archive = coordinator.prices.archive
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    for back in range(1, 40 * 24):
        moment = hour - timedelta(hours=back)
        archive.record(
            moment,
            0.4,
            PRICE_TYPE_CERTAIN,
            "pradcast",
            "t",
            formula={"tge": 0.3, "markup": 0.1, "vat": 0.0},
        )
    [outlook] = coordinator._cap_outlooks(
        optimizer, horizon, None, None, ESSRequest(), periods, tail, None
    )
    assert calls == [[0.4, 0.4]] * 4
    assert outlook.month_start == periods[0].month_start
    assert outlook.bind_probability == 0.5
    assert coordinator.cap_outlook_seconds is not None


async def _refresh_with_outlook(hass: HomeAssistant, monkeypatch, outlook_of):
    """One refresh over a short capped horizon, three solves of it.

    ``outlook_of(period)`` is what the scenarios come to. Returns the
    coordinator, the outlook calls and the ``bind_probability`` of every
    solve.
    """
    from custom_components.powerpilot import coordinator as coordinator_module
    from custom_components.powerpilot.optimizer import Optimizer

    _entry, coordinator = await _setup(hass, **{CONF_TARCZA_TERMS: [TERMS]})
    hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    slots = [
        HourSlot(start=hour + timedelta(hours=i), buy_price=0.5, cap_price_kwh=0.5)
        for i in range(3)
    ]
    period = BillingPeriod(
        ppe=PPE_1,
        month_start=hour - timedelta(days=2),
        month_end=hour + timedelta(hours=5),
        limit_kwh=0.61,
        energy_kwh=10.0,
        cost_pln=5.0,
        hours_expected=48,
        hours_covered=48,
    )
    tail = MonthTail(
        slots=[
            HourSlot(start=hour + timedelta(hours=i), buy_price=0.7, cap_price_kwh=0.7)
            for i in range(3, 5)
        ]
    )
    coordinator.forecast_builder.build = lambda: Forecast(slots=list(slots))

    async def billing_inputs(forecast, ev_request):
        return [period], tail, None

    coordinator._billing_inputs = billing_inputs
    outlook_calls: list[list[BillingPeriod]] = []

    def cap_outlooks(optimizer, forecast, battery, ev_request, ess, periods, tail, supply):
        outlook_calls.append(periods)
        return [outlook_of(period)]

    coordinator._cap_outlooks = cap_outlooks
    odds_seen: list = []
    optimize = Optimizer.optimize

    def spy(self, *args, **kwargs):
        odds_seen.append(kwargs.get("bind_probability"))
        return optimize(self, *args, **kwargs)

    monkeypatch.setattr(Optimizer, "optimize", spy)

    def settle_cycles(solve, planning):
        solve(supply=planning)
        solve(supply=planning)
        return solve(supply=planning)

    monkeypatch.setattr(coordinator_module, "settle_cycles", settle_cycles)
    coordinator._supply_inputs = lambda forecast, periods, reason: (
        SupplyPlanning(actual=SUPPLY_VICTRON, switching_reason="test"),
        None,
    )
    await coordinator.async_refresh()
    return coordinator, outlook_calls, odds_seen


def _weeks(binding: int, dropped: int = 0) -> tuple[CapScenario, ...]:
    return tuple(
        CapScenario(week=k, dropped_reason=f"tydzień {k}: brak")
        if k > 4 - dropped
        else CapScenario(week=k, average_kwh=0.7 if k <= binding else 0.5, binds=k <= binding)
        for k in range(1, 5)
    )


async def test_a_refresh_with_re_solves_evaluates_the_scenarios_once(
    hass: HomeAssistant, monkeypatch
) -> None:
    coordinator, outlook_calls, odds_seen = await _refresh_with_outlook(
        hass,
        monkeypatch,
        lambda period: CapOutlook(month_start=period.month_start, scenarios=_weeks(2)),
    )
    assert len(outlook_calls) == 1
    assert len(odds_seen) == 3
    month = outlook_calls[0][0].month_start
    assert odds_seen == [{month: 0.5}] * 3
    outcome = coordinator.data.billing[0]
    assert outcome.available
    assert outcome.outlook.bind_probability == 0.5


async def test_without_a_usable_week_the_cap_is_unavailable(
    hass: HomeAssistant, monkeypatch
) -> None:
    coordinator, _calls, odds_seen = await _refresh_with_outlook(
        hass,
        monkeypatch,
        lambda period: CapOutlook(month_start=period.month_start, scenarios=_weeks(0, 4)),
    )
    assert odds_seen and all(odds is None for odds in odds_seen)
    outcome = coordinator.data.billing[0]
    assert not outcome.available
    assert "żaden z 4" in outcome.unavailable_reason
