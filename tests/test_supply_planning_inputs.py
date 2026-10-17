"""What the coordinator hands the optimizer for apartment switching (stage 3).

Switches are modelled only with supply detection active, the actual supply
known and both meters' months complete; otherwise the plan keeps the actual
supply and says why. Calendar trips and a calendar read error make the user
unavailable.
"""

from __future__ import annotations

from datetime import timedelta
from functools import partial
from types import SimpleNamespace

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.const import (
    CONF_APARTMENT_METER,
    CONF_APARTMENT_POWER_SENSOR,
    CONF_CONSUMPTION_SENSOR,
    CONF_DEVICE_SENSORS,
    CONF_SUPPLY_RECOMMENDATIONS,
    CONF_VICTRON_OTHER_POWER_SENSORS,
    CONF_VICTRON_OUTPUT_POWER_SENSOR,
    DEFAULTS,
    PPE_1,
    PPE_2,
    SUPPLY_METER_2,
    SUPPLY_SOURCE_LIVE,
)
from custom_components.powerpilot.coordinator import PowerPilotCoordinator
from custom_components.powerpilot.models import (
    BillingPeriod,
    Forecast,
    HourSlot,
    SupplyTransition,
)
from custom_components.powerpilot.modules.billing import month_bounds
from custom_components.powerpilot.modules.supply import SupplyModule

NOW = dt_util.now().replace(minute=0, second=0, microsecond=0)
SUPPLY_CFG = {
    **DEFAULTS,
    CONF_CONSUMPTION_SENSOR: "sensor.main",
    CONF_DEVICE_SENSORS: ["sensor.apartment_energy"],
    CONF_APARTMENT_METER: "sensor.apartment_energy",
    CONF_APARTMENT_POWER_SENSOR: "sensor.apartment_power",
    CONF_VICTRON_OUTPUT_POWER_SENSOR: "sensor.victron_power",
    CONF_VICTRON_OTHER_POWER_SENSORS: ["sensor.garage_power"],
    CONF_SUPPLY_RECOMMENDATIONS: True,
}
SWITCHED_AT = NOW - timedelta(hours=5)


def _period(ppe: str, covered: int = 10, missing: tuple[str, ...] = ()) -> BillingPeriod:
    # Bounds taken at call time: the test harness sets the local time zone.
    month_start, month_end = month_bounds(NOW)
    return BillingPeriod(
        ppe=ppe,
        month_start=month_start,
        month_end=month_end,
        limit_kwh=0.61,
        energy_kwh=50.0,
        cost_pln=20.0,
        hours_expected=10,
        hours_covered=covered,
        missing=missing,
    )


class _Billing:
    def __init__(self, periods: dict[str, BillingPeriod]) -> None:
        self.periods = periods

    def periods_touching(self, ppe, first, last):
        return [self.periods[ppe]] if ppe in self.periods else [_period(ppe, covered=0)]

    def reasons_for(self, ppe):
        return [] if ppe in self.periods else [f"brak danych {ppe}"]


def _coordinator(
    state: str | None = SUPPLY_METER_2,
    periods: dict[str, BillingPeriod] | None = None,
    trips: list | None = None,
    read_errors: list[str] | None = None,
    config: dict | None = None,
):
    config = config if config is not None else SUPPLY_CFG
    supply = SupplyModule(None, SimpleNamespace(config=config))
    if state is not None:
        supply.transitions = [
            SupplyTransition(
                state=state,
                at=SWITCHED_AT,
                not_before=None,
                apartment_w=1000.0,
                victron_net_w=0.0,
                source=SUPPLY_SOURCE_LIVE,
            )
        ]
    fake = SimpleNamespace(
        supply=supply,
        billing=_Billing(
            periods if periods is not None else {PPE_1: _period(PPE_1), PPE_2: _period(PPE_2)}
        ),
        calendar=SimpleNamespace(
            trips=trips or [], read_errors=read_errors or [], last_error=None
        ),
        config=config,
    )
    fake._observed_month_problem = partial(
        PowerPilotCoordinator._observed_month_problem, fake
    )
    return fake


def _forecast() -> Forecast:
    return Forecast(slots=[HourSlot(start=NOW + timedelta(hours=i)) for i in range(3)])


def _inputs(fake, billing_reason: str | None = None, billing_periods=None):
    periods = billing_periods if billing_periods is not None else [_period(PPE_1)]
    return PowerPilotCoordinator._supply_inputs(fake, _forecast(), periods, billing_reason)


def test_switching_is_modelled_with_both_months_complete() -> None:
    trip = SimpleNamespace(depart=NOW + timedelta(hours=1), return_end=NOW + timedelta(hours=4))
    planning, ppe2_reason = _inputs(_coordinator(trips=[trip]))
    assert ppe2_reason is None
    assert planning.switching is True
    assert planning.actual == SUPPLY_METER_2
    assert [p.ppe for p in planning.ppe2_periods] == [PPE_2]
    assert planning.last_transition_at == SWITCHED_AT
    assert planning.unavailable == [(trip.depart, trip.return_end)]
    assert planning.calendar_error is None
    # The defaults of the options step.
    assert planning.cycle_threshold_pln == 2.0
    assert planning.min_stay_hours == 2
    assert (planning.quiet_start_hour, planning.quiet_end_hour) == (0, 9)


def test_an_incomplete_ppe_2_month_turns_switching_off_with_a_reason() -> None:
    periods = {PPE_1: _period(PPE_1), PPE_2: _period(PPE_2, covered=8, missing=("2 h",))}
    planning, ppe2_reason = _inputs(_coordinator(periods=periods))
    assert planning.switching is False
    assert "PPE 2" in planning.switching_reason
    assert "niekompletne" in planning.switching_reason
    assert "niekompletne" in ppe2_reason
    assert planning.ppe2_periods == []
    assert planning.actual == SUPPLY_METER_2  # the apartment stays where it is


def test_a_missing_ppe_2_month_names_the_billing_reason() -> None:
    planning, ppe2_reason = _inputs(_coordinator(periods={PPE_1: _period(PPE_1)}))
    assert planning.switching is False
    assert ppe2_reason == f"brak danych {PPE_2}"


def test_unknown_supply_is_not_switched_but_ppe_2_is_settled() -> None:
    planning, ppe2_reason = _inputs(_coordinator(state=None))
    assert planning.switching is False
    assert "nie wiadomo" in planning.switching_reason
    assert ppe2_reason is None
    assert [p.ppe for p in planning.ppe2_periods] == [PPE_2]


def test_ppe_1_unavailable_turns_switching_off_for_both_meters() -> None:
    planning, ppe2_reason = _inputs(
        _coordinator(), billing_reason="brak ceny pod Tarczę", billing_periods=[]
    )
    assert planning.switching is False
    assert "PPE 1" in planning.switching_reason
    assert ppe2_reason == "brak ceny pod Tarczę"


def test_no_cap_in_the_horizon_means_nothing_to_switch_for() -> None:
    planning, ppe2_reason = _inputs(_coordinator(), billing_periods=[])
    assert planning.switching is False
    assert "Tarcza" in planning.switching_reason
    assert ppe2_reason is None


def test_a_calendar_read_error_reaches_the_planning() -> None:
    planning, _ = _inputs(_coordinator(read_errors=["calendar.work: gone"]))
    assert planning.calendar_error == "calendar.work: gone"
    assert planning.user_available(NOW + timedelta(hours=2)) is False


def test_without_supply_detection_there_is_no_planning() -> None:
    config = {k: v for k, v in SUPPLY_CFG.items() if k != CONF_APARTMENT_METER}
    assert _inputs(_coordinator(config=config)) == (None, None)


def test_recommendations_off_by_default_keep_the_actual_supply_with_a_reason() -> None:
    """The opt-in: without it no switch is modelled, PPE 2 is still settled."""
    config = {**SUPPLY_CFG, CONF_SUPPLY_RECOMMENDATIONS: DEFAULTS[CONF_SUPPLY_RECOMMENDATIONS]}
    assert DEFAULTS[CONF_SUPPLY_RECOMMENDATIONS] is False
    planning, ppe2_reason = _inputs(_coordinator(config=config))
    assert planning.switching is False
    assert "rekomendacje przełączania są wyłączone" in planning.switching_reason
    assert planning.actual == SUPPLY_METER_2
    assert ppe2_reason is None
    assert [p.ppe for p in planning.ppe2_periods] == [PPE_2]
