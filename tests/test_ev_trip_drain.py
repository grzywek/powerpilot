"""Trip drain must only cover driving that is still AHEAD.

The plan starts from the car's live SoC, and that reading already reflects
every kilometre driven so far. Forecasting a leg the car is part-way through
therefore subtracts the same drive twice — once in the sensor, once in the
projection.

The regression this pins down: a plan re-run at 18:00, with the car already
home from a trip whose return was scheduled 18:00–18:59, subtracted the whole
21.8 kWh return leg from a SoC that had just dropped by exactly that drive —
projecting 3.2 % on a car sitting at 31 %, well under the 10 % reserve.
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import pytest

from homeassistant.util import dt as dt_util

from custom_components.powerpilot.modules.calendar import Trip
from custom_components.powerpilot.modules.ev import EVModule

H = dt_util.now().replace(minute=0, second=0, microsecond=0)
KWH_PER_KM = 0.287
CAPACITY = 70.7
OUT_KM = 78.62
RET_KM = 75.85


def _module(soc: float | None = None) -> EVModule:
    ev = EVModule.__new__(EVModule)
    ev._unavailable_hours = set()
    ev._partial_hours = {}
    ev._trip_drain = {}
    ev._kwh_per_km = KWH_PER_KM
    ev._capacity = CAPACITY
    ev._soc = soc
    ev._trip_soc_baseline = {}
    ev.min_soc_entity = SimpleNamespace(native_value=10.0)
    return ev


def _trip() -> Trip:
    """Leaves 5 h ago, event 4 h ago → now, home an hour from now."""
    return Trip(
        label="Babcia",
        location="Kraków",
        event_start=H - timedelta(hours=4),
        event_end=H,
        depart=H - timedelta(hours=5),
        return_end=H + timedelta(hours=1),
        outbound_distance_km=OUT_KM,
        return_distance_km=RET_KM,
    )


def _drain_ahead(ev: EVModule, now) -> float:
    """Drain the projection will still subtract from the live SoC."""
    floor = now.replace(minute=0, second=0, microsecond=0)
    return sum(kwh for hour, kwh in ev._trip_drain.items() if hour >= floor)


def test_leg_still_ahead_is_counted_in_full() -> None:
    ev = _module()
    ev._apply_trip(_trip(), H - timedelta(hours=1))

    assert _drain_ahead(ev, H - timedelta(hours=1)) == pytest.approx(RET_KM * KWH_PER_KM)


def test_leg_already_driven_is_not_counted_again() -> None:
    """The whole return is behind us — it is already in the SoC reading."""
    ev = _module()
    now = H + timedelta(hours=1)
    ev._apply_trip(_trip(), now)

    assert _drain_ahead(ev, now) == 0.0


def test_leg_in_progress_counts_only_what_is_left() -> None:
    """Half-way home → half the leg's energy is still to come."""
    ev = _module()
    now = H + timedelta(minutes=30)
    ev._apply_trip(_trip(), now)

    assert _drain_ahead(ev, now) == pytest.approx(RET_KM * KWH_PER_KM / 2)


def test_outbound_already_driven_is_dropped_too() -> None:
    """Mid-trip: the drive out is spent, only the drive home is ahead."""
    ev = _module()
    now = H - timedelta(hours=2)
    ev._apply_trip(_trip(), now)

    assert _drain_ahead(ev, now) == pytest.approx(RET_KM * KWH_PER_KM)


def test_whole_trip_ahead_counts_both_legs() -> None:
    ev = _module()
    trip = Trip(
        label="Babcia",
        location="Kraków",
        event_start=H + timedelta(hours=2),
        event_end=H + timedelta(hours=6),
        depart=H + timedelta(hours=1),
        return_end=H + timedelta(hours=7),
        outbound_distance_km=OUT_KM,
        return_distance_km=RET_KM,
    )
    ev._apply_trip(trip, H)

    assert _drain_ahead(ev, H) == pytest.approx((OUT_KM + RET_KM) * KWH_PER_KM)


# ---------------------------------------------------------------------------
# Reconciliation against the SoC actually observed since departure. The
# calendar is a forecast of when the car drives; the SoC sensor is the record
# of what it did. When they disagree — home an hour early, left late, traffic —
# only the difference between predicted and already-spent may still be planned.
# ---------------------------------------------------------------------------


def test_early_return_does_not_forecast_the_drive_again() -> None:
    """The reported regression, end to end.

    Predicted 21.8 kWh home between 18:00 and 18:59; the car actually drove it
    during 17:00 and is sitting at 31 %. A re-plan inside the 18:00 hour must
    forecast (almost) nothing more.
    """
    trip = _trip()
    ev = _module(soc=58.0)
    ev._apply_trip(trip, H - timedelta(minutes=30))  # baseline taken en route

    ev._soc = 31.0  # the drive home has happened
    ev._trip_drain = {}
    ev._apply_trip(trip, H + timedelta(minutes=1))

    projected = 31.0 - _drain_ahead(ev, H + timedelta(minutes=1)) / CAPACITY * 100.0
    # Was 3.2 % before the fix — the whole return leg came off a SoC that had
    # already paid for it. The few points left over are the gap between the
    # 21.8 kWh predicted for the drive and the 19.1 kWh it actually took.
    assert projected > 25.0


def test_late_departure_still_forecasts_the_whole_drive() -> None:
    """Nothing has been spent yet, so nothing may be discounted."""
    trip = _trip()
    ev = _module(soc=80.0)
    ev._apply_trip(trip, H - timedelta(hours=5))  # baseline at departure
    ev._trip_drain = {}
    ev._apply_trip(trip, H - timedelta(hours=4, minutes=59))

    assert _drain_ahead(ev, H - timedelta(hours=5)) > 0.0


def test_without_a_baseline_it_falls_back_to_calendar_time() -> None:
    """Restarted mid-trip: no observation to reconcile against."""
    ev = _module(soc=31.0)
    ev._apply_trip(_trip(), H + timedelta(minutes=30))

    assert _drain_ahead(ev, H + timedelta(minutes=30)) == pytest.approx(
        RET_KM * KWH_PER_KM / 2
    )
