"""Unit tests for EV calendar parsing and the EV charge allocator.

These exercise pure logic (no Home Assistant runtime): the optimizer's
``_plan_ev`` allocator and the calendar-event parsing on :class:`EVModule`.
"""

from __future__ import annotations

from datetime import timedelta

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_EV_BATTERY_KWH,
    CONF_EV_CHARGER_KW,
    CONF_EV_CHARGER_PHASE,
    CONF_EV_CHARGER_PHASES,
    CONF_EV_ENABLED,
    CONF_EV_TARGET_SOC_SENSOR,
)
from custom_components.powerpilot.models import Forecast, HourSlot
from custom_components.powerpilot.modules.ev import (
    DEFAULT_TARGET_SOC,
    EVChargeTarget,
    EVModule,
    EVRequest,
)
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


def _bare_module() -> EVModule:
    module = object.__new__(EVModule)
    module._targets = []
    module._forced_hours = set()
    return module


def _iso(hour: int) -> str:
    return (BASE + timedelta(hours=hour)).isoformat()


def test_parse_percent_event_is_deadline_target() -> None:
    module = _bare_module()
    module._parse_event(
        {"summary": "Kotek 100%", "start": _iso(4), "end": _iso(5)}, "Kotek", BASE
    )
    assert len(module._targets) == 1
    assert module._targets[0].target_soc == 100.0
    assert module._targets[0].deadline == BASE + timedelta(hours=4)
    assert module._forced_hours == set()


def test_parse_bare_event_is_forced_window() -> None:
    module = _bare_module()
    module._parse_event(
        {"summary": "Kotek", "start": _iso(6), "end": _iso(9)}, "Kotek", BASE
    )
    assert module._targets == []
    offsets = sorted(int((h - BASE).total_seconds() // 3600) for h in module._forced_hours)
    assert offsets == [6, 7, 8]


def test_parse_percent_accepts_comma_decimal_and_spaces() -> None:
    module = _bare_module()
    module._parse_event(
        {"summary": "Kotek 55,5 %", "start": _iso(3), "end": _iso(4)}, "Kotek", BASE
    )
    assert module._targets[0].target_soc == 55.5


def test_parse_skips_past_deadline() -> None:
    module = _bare_module()
    now = BASE + timedelta(hours=5)
    module._parse_event(
        {"summary": "Kotek 80%", "start": _iso(2), "end": _iso(3)}, "Kotek", now
    )
    assert module._targets == []


def test_parse_skips_non_matching_summary() -> None:
    module = _bare_module()
    module._parse_event(
        {"summary": "Pranie", "start": _iso(2), "end": _iso(3)}, "Kotek", BASE
    )
    assert module._targets == [] and module._forced_hours == set()


def test_parse_custom_keyword_case_insensitive() -> None:
    module = _bare_module()
    module._parse_event(
        {"summary": "auto 75%", "start": _iso(4), "end": _iso(5)}, "Auto", BASE
    )
    assert module._targets[0].target_soc == 75.0


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
        CONF_EV_TARGET_SOC_SENSOR: None,
    }
    module._soc = state.get("soc")
    module._target_soc = state.get("target_soc")
    module._energy_added = None
    module._connected = state.get("connected")
    module._charging = state.get("charging")
    module._available = state.get("available", True)
    module._targets = state.get("targets", [])
    module._forced_hours = state.get("forced_hours", set())
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


def test_get_request_unavailable_has_no_hours() -> None:
    fc = _forecast([0.5] * 6)
    module = _module_with_state(soc=20.0, available=False)
    req = module.get_request(fc)
    assert req.available_hours == set()


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
