"""Unit tests for EV calendar parsing and the EV charge allocator.

These exercise pure logic (no Home Assistant runtime): the optimizer's
``_plan_ev`` allocator and the calendar-event parsing on :class:`EVModule`.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_EV_BATTERY_KWH,
    CONF_EV_CHARGER_KW,
    CONF_EV_CHARGER_PHASE,
    CONF_EV_CHARGER_PHASES,
    CONF_EV_ENABLED,
)
from custom_components.powerpilot.battery import BatteryModel
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.profiles import WeeklyAccumulator
from custom_components.powerpilot.modules.calendar import (
    CalendarEvent,
    Trip,
    trip_window,
)
from custom_components.powerpilot.modules.ev import (
    DEFAULT_TARGET_SOC,
    EVChargeTarget,
    EVModule,
    EVRequest,
    _capacity_samples,
    _segment_sessions,
    _spread_energy,
    _value_at,
)
from custom_components.powerpilot.travel import TravelInfo, parse_distance_matrix
from custom_components.powerpilot.optimizer import (
    ChargeCurve,
    Optimizer,
    OptimizerConfig,
)

BASE = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0)


def _forecast(prices: list[float]) -> Forecast:
    slots = []
    for hour, price in enumerate(prices):
        slot = HourSlot(start=BASE + timedelta(hours=hour), buy_price=price)
        slot.base_consumption_kwh = 0.3
        slots.append(slot)
    return Forecast(slots=slots)


def _optimizer() -> Optimizer:
    return Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=3.0,
            inverter_max_discharge_kw=3.0,
            grid_disconnect_soc=15.0,
            charge_curve=ChargeCurve(default_kw=3.0),
        )
    )


def _hours(alloc: dict[datetime, float]) -> dict[int, float]:
    """Allocation keyed by hour offset, rounded for easy assertions."""
    return {
        int((start - BASE).total_seconds() // 3600): round(kwh, 3)
        for start, kwh in alloc.items()
    }


# ---------------------------------------------------------------------------
# Optimizer: charge curve
# ---------------------------------------------------------------------------


def test_optimizer_uses_battery_capacity_for_charge_curve_cuts() -> None:
    fc = _forecast([0.2, 0.2, 0.8, 0.8])
    optimizer = Optimizer(
        OptimizerConfig(
            inverter_max_charge_kw=3.0,
            inverter_max_discharge_kw=3.0,
            grid_disconnect_soc=15.0,
            charge_curve=ChargeCurve(
                default_kw=3.0,
                segments=[
                    {"soc_from": 0, "soc_to": 51, "max_kw": 3.0},
                    {"soc_from": 51, "soc_to": 101, "max_kw": 1.5},
                ],
            ),
        )
    )
    battery = BatteryModel(capacity_kwh=10.0, soc=40.0)

    plan = optimizer.optimize(fc, battery)

    assert len(plan.decisions) == len(fc.slots)


# ---------------------------------------------------------------------------
# Allocator: forced windows
# ---------------------------------------------------------------------------


def test_forced_window_charges_full_power() -> None:
    fc = _forecast([0.8] * 10)
    req = EVRequest(
        enabled=True,
        charger_kw=7.0,
        battery_kwh=60.0,
        current_soc=20.0,
        available_hours={s.start for s in fc.slots},
        forced_hours={BASE + timedelta(hours=6), BASE + timedelta(hours=7)},
    )
    alloc = _hours(_optimizer()._plan_ev(fc, req))
    assert alloc == {6: 7.0, 7: 7.0}


def test_forced_window_ignores_unavailable_hours() -> None:
    fc = _forecast([0.8] * 10)
    req = EVRequest(
        enabled=True,
        charger_kw=7.0,
        battery_kwh=60.0,
        current_soc=20.0,
        available_hours=set(),  # car not plugged in / away
        forced_hours={BASE + timedelta(hours=6)},
    )
    assert _optimizer()._plan_ev(fc, req) == {}


def test_forced_window_capped_to_full_battery() -> None:
    """Forced charging never pushes the pack past 100 %."""
    fc = _forecast([0.8] * 10)
    # 10 kWh pack at 90 % → only 1 kWh of physical room left.
    req = EVRequest(
        enabled=True,
        charger_kw=7.0,
        battery_kwh=10.0,
        current_soc=90.0,
        available_hours={s.start for s in fc.slots},
        forced_hours={BASE + timedelta(hours=h) for h in (6, 7, 8)},
    )
    alloc = _optimizer()._plan_ev(fc, req)
    assert round(sum(alloc.values()), 3) == 1.0


# ---------------------------------------------------------------------------
# Allocator: deadline targets
# ---------------------------------------------------------------------------


def test_target_fills_cheapest_hours_before_deadline() -> None:
    # Cheap at h2/h3 (0.2), expensive elsewhere; deadline at h5.
    prices = [1.5, 1.5, 0.2, 0.2, 0.8, 0.8, 0.8, 0.8]
    fc = _forecast(prices)
    # 60 kWh pack, 20 % → 12 kWh; target 50 % → 30 kWh; need 18 kWh.
    req = EVRequest(
        enabled=True,
        charger_kw=7.0,
        battery_kwh=60.0,
        current_soc=20.0,
        available_hours={s.start for s in fc.slots},
        targets=[EVChargeTarget(deadline=BASE + timedelta(hours=5), target_soc=50.0)],
    )
    alloc = _hours(_optimizer()._plan_ev(fc, req))
    # 18 kWh at 7 kW → 3 on-hours (ceil), exact: two full blocks + a 4 kWh
    # remainder. Full power lands on the two cheapest hours (h2/h3 @ 0.2); the
    # remainder lands on the priciest valid top-off hour (h4 @ 0.8), since the
    # partial buys less energy there — cost-optimal, not an overshoot.
    assert round(sum(alloc.values()), 3) == 18.0
    assert alloc[2] == 7.0 and alloc[3] == 7.0 and round(alloc[4], 3) == 4.0
    assert all(h < 5 for h in alloc)


def test_partial_lands_on_last_chronological_hour_not_cheapest() -> None:
    # Real on/off charger: it draws FULL power the moment an hour opens, so a
    # fractional remainder must land on the hour where the car tops off (the last
    # chronological charging hour) — never on an earlier, pricier hour the cost
    # model would otherwise "save" on. Reproduces the reported 0.75-at-11:00 bug:
    # h0 is fractionally the priciest of the three chosen, but it must still be a
    # full-power hour; the 0.75-style remainder belongs on h2.
    prices = [0.2014, 0.2001, 0.2001, 0.30, 0.30]
    fc = _forecast(prices)
    # 75 kWh pack, 71 % → 53.25 kWh; target 100 % → 75 kWh; need 21.75 kWh.
    req = EVRequest(
        enabled=True,
        charger_kw=3.5,
        phases=3,  # 3.5 × 3 = 10.5 kW full power
        battery_kwh=75.0,
        current_soc=71.0,
        available_hours={s.start for s in fc.slots},
        targets=[EVChargeTarget(deadline=BASE + timedelta(hours=4), target_soc=100.0)],
    )
    alloc = _hours(_optimizer()._plan_ev(fc, req))
    assert round(sum(alloc.values()), 3) == 21.75
    # Early hours full power (incl. the priciest chosen one), remainder last.
    assert alloc[0] == 10.5 and alloc[1] == 10.5
    assert round(alloc[2], 3) == 0.75


def test_skips_expensive_full_hour_for_cheaper_remainder() -> None:
    # The key cost-optimal case: a real on/off charger draws FULL power the moment
    # an hour opens, so the planner must not start in an expensive hour just to
    # later top off cheaply. With 3h10m of charging needed and:
    #   h0 0.50 | h1 0.20 | h2 0.20 | h3 0.20 | h4 0.60
    # the right answer is full power on the three 0.20 hours (h1-h3) and the small
    # remainder on h4 (0.60) — NOT a full hour at h0 (0.50). Charging 10.5 kWh at
    # 0.50 to save a few minutes elsewhere would be far more expensive.
    prices = [0.50, 0.20, 0.20, 0.20, 0.60]
    fc = _forecast(prices)
    # 200 kWh pack so capacity isn't the binding limit; need 33.25 kWh = 3h10m at
    # 10.5 kW (3 full + 1.75 remainder).
    req = EVRequest(
        enabled=True,
        charger_kw=3.5,
        phases=3,  # 10.5 kW full power
        battery_kwh=200.0,
        current_soc=50.0,
        required_kwh=33.25,
        available_hours={s.start for s in fc.slots},
    )
    alloc = _hours(_optimizer()._plan_ev(fc, req))
    assert round(sum(alloc.values()), 3) == 33.25
    assert alloc[1] == 10.5 and alloc[2] == 10.5 and alloc[3] == 10.5
    assert round(alloc[4], 3) == 1.75
    assert 0 not in alloc  # the expensive 0.50 hour is never used


def test_earlier_deadline_honoured_before_later() -> None:
    # h0 cheapest, but the first deadline is at h2 so it cannot be used for it.
    prices = [0.1, 0.9, 0.9, 0.9, 0.9, 0.9]
    fc = _forecast(prices)
    req = EVRequest(
        enabled=True,
        charger_kw=10.0,
        battery_kwh=100.0,
        current_soc=0.0,
        available_hours={s.start for s in fc.slots},
        targets=[
            EVChargeTarget(deadline=BASE + timedelta(hours=2), target_soc=20.0),
            EVChargeTarget(deadline=BASE + timedelta(hours=5), target_soc=40.0),
        ],
    )
    alloc = _hours(_optimizer()._plan_ev(fc, req))
    # 20 kWh must be present by h2 → hours 0 and 1 (10 kWh each).
    assert alloc.get(0) == 10.0 and alloc.get(1) == 10.0
    # Another 20 kWh by h5 → two more hours before h5.
    assert round(sum(alloc.values()), 3) == 40.0
    assert all(h < 5 for h in alloc)


def test_target_already_met_allocates_nothing() -> None:
    fc = _forecast([0.5] * 6)
    req = EVRequest(
        enabled=True,
        charger_kw=7.0,
        battery_kwh=60.0,
        current_soc=80.0,
        available_hours={s.start for s in fc.slots},
        targets=[EVChargeTarget(deadline=BASE + timedelta(hours=4), target_soc=50.0)],
    )
    assert _optimizer()._plan_ev(fc, req) == {}


# ---------------------------------------------------------------------------
# Allocator: default top-up (no calendar)
# ---------------------------------------------------------------------------


def test_default_topup_uses_cheapest_hours() -> None:
    prices = [0.9, 0.2, 0.9, 0.2, 0.9]
    fc = _forecast(prices)
    req = EVRequest(
        enabled=True,
        required_kwh=5.0,
        charger_kw=3.0,
        battery_kwh=60.0,
        current_soc=50.0,
        available_hours={s.start for s in fc.slots},
    )
    alloc = _hours(_optimizer()._plan_ev(fc, req))
    # 5 kWh at 3 kW → 2 on-hours, exact: one full block + a 2 kWh remainder. Both
    # land on the two cheapest hours (h1/h3 @ 0.2) — full power on the earlier
    # one, the remainder on the later (the chronological top-off).
    assert round(sum(alloc.values()), 3) == 5.0
    assert alloc[1] == 3.0 and round(alloc[3], 3) == 2.0
    assert set(alloc) == {1, 3}


def test_not_actionable_returns_empty() -> None:
    fc = _forecast([0.5] * 4)
    req = EVRequest(enabled=False, available_hours={s.start for s in fc.slots})
    assert _optimizer()._plan_ev(fc, req) == {}


# ---------------------------------------------------------------------------
# Calendar parsing
# ---------------------------------------------------------------------------


def _bare_module(**state) -> EVModule:
    module = object.__new__(EVModule)
    module._targets = []
    module._trip_targets = []
    module._forced_hours = set()
    module._unavailable_hours = set()
    module._trip_drain = {}
    module._capacity = state.get("capacity", 60.0)
    module._kwh_per_km = state.get("kwh_per_km")
    module.min_soc_entity = (
        SimpleNamespace(native_value=state["min_soc"]) if "min_soc" in state else None
    )
    module.log_warning = lambda *a, **kw: None
    return module


def _event(summary: str, start: int, end: int, location: str = "") -> CalendarEvent:
    return CalendarEvent(
        summary=summary,
        location=location,
        start=BASE + timedelta(hours=start),
        end=BASE + timedelta(hours=end),
        calendar="calendar.test",
    )


def test_parse_percent_event_is_deadline_target() -> None:
    module = _bare_module()
    module._parse_keyword_event(_event("Kotek 100%", 4, 5), "Kotek", BASE)
    assert len(module._targets) == 1
    assert module._targets[0].target_soc == 100.0
    assert module._targets[0].deadline == BASE + timedelta(hours=4)
    assert module._forced_hours == set()


def test_parse_bare_event_is_forced_window() -> None:
    module = _bare_module()
    module._parse_keyword_event(_event("Kotek", 6, 9), "Kotek", BASE)
    assert module._targets == []
    offsets = sorted(int((h - BASE).total_seconds() // 3600) for h in module._forced_hours)
    assert offsets == [6, 7, 8]


def test_parse_percent_accepts_comma_decimal_and_spaces() -> None:
    module = _bare_module()
    module._parse_keyword_event(_event("Kotek 55,5 %", 3, 4), "Kotek", BASE)
    assert module._targets[0].target_soc == 55.5


def test_parse_skips_past_deadline() -> None:
    module = _bare_module()
    now = BASE + timedelta(hours=5)
    module._parse_keyword_event(_event("Kotek 80%", 2, 3), "Kotek", now)
    assert module._targets == []


def test_parse_skips_non_matching_summary() -> None:
    module = _bare_module()
    module._parse_keyword_event(_event("Pranie", 2, 3), "Kotek", BASE)
    assert module._targets == [] and module._forced_hours == set()


def test_parse_custom_keyword_case_insensitive() -> None:
    module = _bare_module()
    module._parse_keyword_event(_event("auto 75%", 4, 5), "Auto", BASE)
    assert module._targets[0].target_soc == 75.0


# ---------------------------------------------------------------------------
# Trips: unavailability window, drive drain and pre-departure targets
# ---------------------------------------------------------------------------


def _trip(
    start: int,
    end: int,
    distance_km: float | None = None,
    duration_min: float = 0.0,
    margin_min: float = 0.0,
    label: str = "Wyjazd",
) -> Trip:
    event_start = BASE + timedelta(hours=start)
    event_end = BASE + timedelta(hours=end)
    depart, return_end = trip_window(
        event_start,
        event_end,
        TravelInfo(distance_km=distance_km, duration_min=duration_min)
        if distance_km is not None
        else None,
        margin_min,
        margin_min,
    )
    return Trip(
        label=label,
        location="Warszawa",
        event_start=event_start,
        event_end=event_end,
        depart=depart,
        return_end=return_end,
        distance_km=distance_km,
        duration_min=duration_min if distance_km is not None else None,
    )


def test_trip_window_extends_by_travel_and_margins() -> None:
    depart, return_end = trip_window(
        BASE + timedelta(hours=10),
        BASE + timedelta(hours=12),
        TravelInfo(distance_km=50.0, duration_min=45.0),
        30.0,
        15.0,
    )
    assert depart == BASE + timedelta(hours=10) - timedelta(minutes=75)
    assert return_end == BASE + timedelta(hours=12) + timedelta(minutes=60)


def test_trip_window_without_travel_uses_margins_only() -> None:
    depart, return_end = trip_window(
        BASE + timedelta(hours=10), BASE + timedelta(hours=12), None, 30.0, 30.0
    )
    assert depart == BASE + timedelta(hours=9, minutes=30)
    assert return_end == BASE + timedelta(hours=12, minutes=30)


def test_apply_trip_marks_away_hours_unavailable() -> None:
    module = _bare_module()
    # Event 10–12, travel 45 min + margin 30 min → away 8:45 – 13:15.
    module._apply_trip(_trip(10, 12, 50.0, 45.0, 30.0), BASE)
    offsets = sorted(
        int((h - BASE).total_seconds() // 3600) for h in module._unavailable_hours
    )
    assert offsets == [8, 9, 10, 11, 12, 13]


def test_apply_trip_builds_drain_and_target() -> None:
    # 50 km one-way, 0.2 kWh/km → 10 kWh per leg, 20 kWh round trip.
    # 60 kWh pack, min SoC 20 % → target 20 + 20/60*100 = 53.33 %.
    module = _bare_module(kwh_per_km=0.2, min_soc=20.0)
    module._apply_trip(_trip(10, 12, 50.0, 60.0, 0.0), BASE)
    assert round(sum(module._trip_drain.values()), 3) == 20.0
    assert len(module._trip_targets) == 1
    target = module._trip_targets[0]
    assert target.source == "trip"
    assert target.deadline == BASE + timedelta(hours=9)
    assert round(target.target_soc, 2) == 53.33


def test_apply_trip_without_distance_is_unavailability_only() -> None:
    module = _bare_module(kwh_per_km=0.2, min_soc=20.0)
    module._apply_trip(_trip(10, 12, None, margin_min=30.0), BASE)
    assert module._unavailable_hours  # away window still applies
    assert module._trip_drain == {}
    assert module._trip_targets == []


def test_apply_trip_without_kwh_per_km_skips_energy_model() -> None:
    module = _bare_module(kwh_per_km=None, min_soc=20.0)
    module._apply_trip(_trip(10, 12, 50.0, 45.0, 0.0), BASE)
    assert module._trip_drain == {}
    assert module._trip_targets == []


def test_spread_energy_splits_evenly_over_hours() -> None:
    out = _spread_energy(BASE + timedelta(hours=2), BASE + timedelta(hours=4), 6.0)
    assert {int((h - BASE).total_seconds() // 3600): kwh for h, kwh in out.items()} == {
        2: 3.0,
        3: 3.0,
    }


def test_spread_energy_sub_hour_leg_lands_on_start_hour() -> None:
    start = BASE + timedelta(hours=2, minutes=10)
    out = _spread_energy(start, start + timedelta(minutes=20), 4.0)
    assert list(out.values()) == [4.0]
    assert list(out.keys())[0] == BASE + timedelta(hours=2)


# ---------------------------------------------------------------------------
# get_request wiring
# ---------------------------------------------------------------------------


def _module_with_state(**state) -> EVModule:
    module = object.__new__(EVModule)
    module.config = {
        CONF_EV_ENABLED: True,
        CONF_EV_BATTERY_KWH: 60.0,
        CONF_EV_CHARGER_KW: 7.0,
        CONF_EV_CHARGER_PHASE: 1,
        CONF_EV_CHARGER_PHASES: state.get("phases", 1),
    }
    module._soc = state.get("soc")
    target_soc = state.get("target_soc")
    module.target_soc_entity = (
        SimpleNamespace(native_value=target_soc) if target_soc is not None else None
    )
    module._energy_added = None
    module._home = state.get("home")
    module._charging = state.get("charging")
    module._targets = state.get("targets", [])
    module._trip_targets = state.get("trip_targets", [])
    module._forced_hours = state.get("forced_hours", set())
    module._unavailable_hours = state.get("unavailable_hours", set())
    module._trip_drain = state.get("trip_drain", {})
    min_soc = state.get("min_soc")
    module.min_soc_entity = (
        SimpleNamespace(native_value=min_soc) if min_soc is not None else None
    )
    # Capacity is learned at runtime; tests supply it directly.
    module._capacity = state.get("capacity", 60.0)
    module._kwh_per_km = state.get("kwh_per_km")
    module._drain_profile = state.get("drain_profile") or WeeklyAccumulator()
    module._request = EVRequest()
    return module


def test_get_request_calendar_governs_required_kwh() -> None:
    fc = _forecast([0.5] * 6)
    targets = [EVChargeTarget(deadline=BASE + timedelta(hours=4), target_soc=80.0)]
    module = _module_with_state(soc=20.0, targets=targets)
    req = module.get_request(fc)
    # Calendar present → no separate default top-up.
    assert req.required_kwh == 0.0
    assert req.targets == targets
    assert req.available_hours == {s.start for s in fc.slots}


def test_get_request_default_topup_uses_target_sensor() -> None:
    fc = _forecast([0.5] * 6)
    module = _module_with_state(soc=50.0, target_soc=70.0)
    req = module.get_request(fc)
    # (70 - 50) % of 60 kWh = 12 kWh.
    assert round(req.required_kwh, 3) == 12.0


def test_get_request_default_topup_falls_back_to_default_target() -> None:
    fc = _forecast([0.5] * 6)
    module = _module_with_state(soc=50.0)
    req = module.get_request(fc)
    expected = (DEFAULT_TARGET_SOC - 50.0) / 100.0 * 60.0
    assert round(req.required_kwh, 3) == round(expected, 3)


def test_get_request_with_no_signals_assumes_available() -> None:
    """No connection/location sensors configured → plan ahead regardless."""
    fc = _forecast([0.5] * 6)
    module = _module_with_state(soc=20.0)
    req = module.get_request(fc)
    assert req.available_hours == {s.start for s in fc.slots}


def test_get_request_calendar_away_hours_are_unavailable() -> None:
    fc = _forecast([0.5] * 6)
    away_hours = {BASE + timedelta(hours=2), BASE + timedelta(hours=3)}
    module = _module_with_state(soc=20.0, unavailable_hours=away_hours)
    req = module.get_request(fc)
    assert req.available_hours == {s.start for s in fc.slots} - away_hours


# ---------------------------------------------------------------------------
# Charger phases / full power
# ---------------------------------------------------------------------------


def test_charger_power_scales_with_phases() -> None:
    assert EVRequest(charger_kw=3.5, phases=1).charger_power_kw == 3.5
    assert EVRequest(charger_kw=3.5, phases=3).charger_power_kw == 10.5


def test_three_phase_charges_at_full_power() -> None:
    fc = _forecast([0.8] * 6)
    req = EVRequest(
        enabled=True,
        charger_kw=3.5,
        phases=3,
        battery_kwh=60.0,
        current_soc=20.0,
        available_hours={s.start for s in fc.slots},
        forced_hours={BASE + timedelta(hours=2)},
    )
    alloc = _hours(_optimizer()._plan_ev(fc, req))
    # One forced hour at the full 3-phase power, not the per-phase 3.5 kW.
    assert alloc == {2: 10.5}


def test_get_request_passes_phases() -> None:
    fc = _forecast([0.5] * 6)
    module = _module_with_state(soc=20.0, phases=3)
    req = module.get_request(fc)
    assert req.phases == 3
    assert req.charger_power_kw == 21.0  # 7 kW × 3


# ---------------------------------------------------------------------------
# SoC limit advisory
# ---------------------------------------------------------------------------


def test_soc_limit_forced_window_is_unlimited() -> None:
    now_hour = dt_util.now().replace(minute=0, second=0, microsecond=0)
    module = _module_with_state(forced_hours={now_hour})
    assert module.soc_limit_now() == 100.0


def test_soc_limit_uses_next_target() -> None:
    module = _module_with_state(
        targets=[
            EVChargeTarget(deadline=BASE + timedelta(hours=8), target_soc=90.0),
            EVChargeTarget(deadline=BASE + timedelta(hours=4), target_soc=60.0),
        ]
    )
    assert module.soc_limit_now() == 60.0


def test_soc_limit_defaults_to_target_sensor() -> None:
    module = _module_with_state(target_soc=70.0)
    assert module.soc_limit_now() == 70.0


def test_soc_limit_falls_back_to_default() -> None:
    module = _module_with_state()
    assert module.soc_limit_now() == DEFAULT_TARGET_SOC


def test_soc_limit_raised_by_trip_target() -> None:
    # A trip needing 95% must raise the advisory limit above the routine 80%.
    module = _module_with_state(
        target_soc=80.0,
        trip_targets=[
            EVChargeTarget(
                deadline=BASE + timedelta(hours=6), target_soc=95.0, source="trip"
            )
        ],
    )
    assert module.soc_limit_now() == 95.0


def test_soc_limit_not_lowered_by_small_trip_target() -> None:
    # Trip targets are a floor, not a ceiling — a small trip keeps the 80% cap.
    module = _module_with_state(
        target_soc=80.0,
        trip_targets=[
            EVChargeTarget(
                deadline=BASE + timedelta(hours=6), target_soc=35.0, source="trip"
            )
        ],
    )
    assert module.soc_limit_now() == 80.0


# ---------------------------------------------------------------------------
# Battery capacity learning
# ---------------------------------------------------------------------------


def _ts(hours: float):
    return BASE + timedelta(hours=hours)


def test_value_at_picks_last_sample_at_or_before() -> None:
    hist = [(_ts(0), 10.0), (_ts(1), 20.0), (_ts(2), 30.0)]
    assert _value_at(hist, _ts(1.5)) == 20.0
    assert _value_at(hist, _ts(0)) == 10.0
    assert _value_at(hist, _ts(-1)) == 10.0  # before first → earliest


def test_segment_sessions_splits_on_reset() -> None:
    # Two charging runs: 0→8 kWh, reset, then 0→5 kWh.
    energy = [
        (_ts(0), 0.0), (_ts(1), 4.0), (_ts(2), 8.0),
        (_ts(3), 0.0), (_ts(4), 5.0),
    ]
    sessions = _segment_sessions(energy)
    assert len(sessions) == 2
    assert sessions[0][2] == 8.0 and sessions[1][2] == 5.0


def test_capacity_samples_from_clean_session() -> None:
    # 30 kWh added while SoC went 20% → 60% (Δ40%) → capacity = 30/40*100 = 75 kWh.
    soc = [(_ts(0), 20.0), (_ts(1), 40.0), (_ts(2), 60.0)]
    energy = [(_ts(0), 0.0), (_ts(1), 15.0), (_ts(2), 30.0)]
    samples = _capacity_samples(soc, energy)
    assert len(samples) == 1
    assert round(samples[0], 1) == 75.0


def test_capacity_samples_skips_tiny_soc_swing() -> None:
    # Only 5% SoC swing → too noisy, ignored.
    soc = [(_ts(0), 80.0), (_ts(2), 85.0)]
    energy = [(_ts(0), 0.0), (_ts(2), 4.0)]
    assert _capacity_samples(soc, energy) == []


def test_get_request_not_actionable_without_capacity() -> None:
    fc = _forecast([0.5] * 6)
    module = _module_with_state(soc=20.0, target_soc=80.0, capacity=None)
    req = module.get_request(fc)
    assert req.battery_kwh == 0.0
    assert req.is_actionable is False


# ---------------------------------------------------------------------------
# Driving-consumption learning + drain-based charging
# ---------------------------------------------------------------------------


def test_hourly_drain_attributes_soc_drops() -> None:
    from custom_components.powerpilot.modules.ev import _hourly_drain

    # 60 kWh pack; SoC 50→40 over [0,1) then steady → 6 kWh out at hour 0.
    soc = [(_ts(0), 50.0), (_ts(1), 40.0), (_ts(2), 40.0)]
    drain = _hourly_drain(soc, 60.0)
    assert round(drain[_ts(0)], 3) == 6.0


def test_kwh_per_km_from_distance_and_soc() -> None:
    from custom_components.powerpilot.modules.ev import _kwh_per_km

    # 12 kWh out (SoC 60→40 of 60 kWh) over 60 km → 0.2 kWh/km.
    soc = [(_ts(0), 60.0), (_ts(2), 40.0)]
    odo = [(_ts(0), 1000.0), (_ts(2), 1060.0)]
    assert round(_kwh_per_km(soc, odo, 60.0), 3) == 0.2


def _flat_drain(per_hour: float) -> WeeklyAccumulator:
    """A drain profile with the same kWh every (weekday, hour)."""
    acc = WeeklyAccumulator()
    for i in range(7 * 24):
        acc.observe(BASE + timedelta(hours=i), per_hour)
    acc.mark_date_observed(BASE.date())
    return acc


def test_get_request_drain_based_topup() -> None:
    # 60 kWh pack, at 20% (12 kWh), car target 80% (48 kWh), reserve 20% (12 kWh).
    # Drain profile predicts 0.25 kWh/h → 6 kWh over the next 24 h.
    # target_energy = min(6 + 12, 48) = 18 kWh; required = 18 - 12 = 6 kWh.
    fc = _forecast([0.5] * 6)
    module = _module_with_state(
        soc=20.0, target_soc=80.0, drain_profile=_flat_drain(0.25)
    )
    req = module.get_request(fc)
    assert round(req.required_kwh, 3) == 6.0


def test_get_request_passes_trip_targets_and_drain() -> None:
    fc = _forecast([0.5] * 6)
    trip_target = EVChargeTarget(
        deadline=BASE + timedelta(hours=4), target_soc=50.0, source="trip"
    )
    drain = {BASE + timedelta(hours=4): 6.0}
    module = _module_with_state(
        soc=70.0, target_soc=80.0, trip_targets=[trip_target], trip_drain=drain,
        min_soc=25.0,
    )
    req = module.get_request(fc)
    # Trip targets do NOT suppress the routine top-up (only keyword plans do).
    assert req.required_kwh > 0
    assert req.targets == [trip_target]
    assert req.drain_kwh == drain
    assert req.min_soc == 25.0


# ---------------------------------------------------------------------------
# Allocator + SoC projection with trip drain
# ---------------------------------------------------------------------------


def test_target_after_drain_buys_the_drained_energy_back() -> None:
    # 60 kWh pack at 50% (30 kWh). A 12 kWh trip drains before the deadline at
    # h5; target 50% by h5 → without drain nothing to buy, with drain 12 kWh.
    prices = [0.2, 0.2, 0.9, 0.9, 0.9, 0.9]
    fc = _forecast(prices)
    req = EVRequest(
        enabled=True,
        charger_kw=7.0,
        battery_kwh=60.0,
        current_soc=50.0,
        available_hours={s.start for s in fc.slots},
        targets=[EVChargeTarget(deadline=BASE + timedelta(hours=5), target_soc=50.0)],
        drain_kwh={BASE + timedelta(hours=3): 12.0},
    )
    alloc = _hours(_optimizer()._plan_ev(fc, req))
    assert round(sum(alloc.values()), 3) == 12.0
    assert all(h < 5 for h in alloc)


def test_full_pack_with_upcoming_trip_can_still_top_up() -> None:
    # Pack already at 100 % but a 10 kWh trip drain is coming — the room-to-100%
    # cap must credit the drain so the default top-up can buy the energy back.
    fc = _forecast([0.3] * 8)
    req = EVRequest(
        enabled=True,
        required_kwh=10.0,
        charger_kw=5.0,
        battery_kwh=60.0,
        current_soc=100.0,
        available_hours={s.start for s in fc.slots},
        drain_kwh={BASE + timedelta(hours=2): 10.0},
    )
    alloc = _optimizer()._plan_ev(fc, req)
    assert round(sum(alloc.values()), 3) == 10.0


def test_plan_ev_soc_line_subtracts_trip_drain() -> None:
    fc = _forecast([0.5] * 4)
    req = EVRequest(
        enabled=True,
        charger_kw=7.0,
        battery_kwh=60.0,
        current_soc=50.0,
        available_hours=set(),  # away → no charging planned
        drain_kwh={
            BASE + timedelta(hours=1): 6.0,
            BASE + timedelta(hours=2): 6.0,
        },
        # Keep the request actionable so it reaches the projection.
        targets=[EVChargeTarget(deadline=BASE + timedelta(hours=3), target_soc=50.0)],
    )
    battery = BatteryModel(capacity_kwh=10.0, soc=50.0)
    plan = _optimizer().optimize(fc, battery, req)
    socs = [d.ev_soc for d in plan.decisions]
    # 50% → 40% → 30% (6 kWh = 10% of 60 kWh per drained hour), then flat.
    assert socs == [50.0, 40.0, 30.0, 30.0]


# ---------------------------------------------------------------------------
# Google Maps response parsing
# ---------------------------------------------------------------------------


def test_parse_distance_matrix_ok() -> None:
    payload = {
        "status": "OK",
        "rows": [
            {
                "elements": [
                    {
                        "status": "OK",
                        "distance": {"value": 52340},
                        "duration": {"value": 2712},
                    }
                ]
            }
        ],
    }
    info = parse_distance_matrix(payload)
    assert info is not None
    assert round(info.distance_km, 2) == 52.34
    assert round(info.duration_min, 1) == 45.2


def test_parse_distance_matrix_rejects_unresolved() -> None:
    assert parse_distance_matrix({"status": "REQUEST_DENIED"}) is None
    assert (
        parse_distance_matrix(
            {"status": "OK", "rows": [{"elements": [{"status": "NOT_FOUND"}]}]}
        )
        is None
    )
    assert parse_distance_matrix({}) is None


# ---------------------------------------------------------------------------
# Deadline feasibility reminders
# ---------------------------------------------------------------------------


def test_reminder_when_target_unreachable_before_departure() -> None:
    fc = _forecast([0.5] * 3)
    # 60 kWh pack at 10% (6 kWh); needs 90% (54 kWh) by h2 with only two 7 kW
    # hours available → 48 kWh short of the 48 kWh deficit... clearly infeasible.
    module = _module_with_state(soc=10.0, target_soc=80.0)
    module._targets = [
        EVChargeTarget(deadline=BASE + timedelta(hours=2), target_soc=90.0)
    ]
    module.get_request(fc)
    reminders = module._deadline_feasibility_reminders()
    assert len(reminders) == 1
    assert "nie zdąży" in reminders[0]


def test_no_reminder_when_target_reachable() -> None:
    fc = _forecast([0.5] * 6)
    module = _module_with_state(soc=60.0, target_soc=80.0)
    module._targets = [
        EVChargeTarget(deadline=BASE + timedelta(hours=5), target_soc=70.0)
    ]
    module.get_request(fc)
    assert module._deadline_feasibility_reminders() == []
