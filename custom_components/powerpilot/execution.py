"""Plan execution watchdog: does the inverter actually do what the plan says?

PowerPilot only advises — a user automation switches the grid connection and
the inverter mode. When that automation fails the plan keeps saying "charge"
while the pack drains into a blackout (2026-09-14: charge planned 13–19, zero
grid import all day, ESS at 0 % by 17:15). The watchdog compares the planned
mode with the battery's measured flows over a short window and names the two
mismatches that matter:

* energy leaving the pack while the plan needs the grid (charge / passthrough),
* a planned charge that is not reaching the pack.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from .const import InverterMode

# How often the coordinator checks, aligned with the recorder's 5-minute
# statistics the flows are read from.
EXECUTION_CHECK_INTERVAL = timedelta(minutes=5)
# Statistics window read per check. An energy counter is differenced across
# its 5-minute buckets, so the measured span is one bucket shorter — the
# charge expectation below is sized on that shorter span.
EXECUTION_WINDOW = timedelta(minutes=20)
_MEASURED_HOURS = (EXECUTION_WINDOW - timedelta(minutes=5)).total_seconds() / 3600.0
# Battery → house energy that cannot be noise: 0.05 kWh over ~15 min is a
# 200 W average, below any real household base load.
DISCHARGE_ALARM_KWH = 0.05
# A planned charge counts as missing when the pack took in less than this share
# of the planned grid energy — losses and ramp-up stay far above it.
CHARGE_MISSING_SHARE = 0.25

GRID_MODES = (InverterMode.CHARGE, InverterMode.PASSTHROUGH)


@dataclass(frozen=True)
class ExecutionVerdict:
    ok: bool
    reason: str | None = None


def assess_execution(
    mode: str | None,
    charge_power_kw: float,
    discharged_kwh: float | None,
    charged_kwh: float | None,
) -> ExecutionVerdict | None:
    """Judge one window of measured flows against the planned mode.

    ``None`` when the discharge flow could not be measured — "cannot say" is
    not "all good". ``charged_kwh`` of ``None`` (no charge counter) only skips
    the missing-charge check; draining against the plan is still caught.
    """
    if mode not in GRID_MODES:
        return ExecutionVerdict(ok=True)
    if discharged_kwh is None:
        return None
    minutes = round(_MEASURED_HOURS * 60)
    if discharged_kwh >= DISCHARGE_ALARM_KWH:
        return ExecutionVerdict(
            ok=False,
            reason=(
                f"Plan: {mode}, a ESS oddał {discharged_kwh:.2f} kWh w ostatnich "
                f"~{minutes} min — sieć lub falownik nie wykonują planu."
            ),
        )
    if mode == InverterMode.CHARGE and charge_power_kw > 0 and charged_kwh is not None:
        expected = charge_power_kw * _MEASURED_HOURS
        if charged_kwh < expected * CHARGE_MISSING_SHARE:
            return ExecutionVerdict(
                ok=False,
                reason=(
                    f"Plan: ładowanie {charge_power_kw:.1f} kW, a ESS przyjął "
                    f"{charged_kwh:.2f} kWh z ~{expected:.2f} kWh w ostatnich "
                    f"~{minutes} min — ładowanie nie działa."
                ),
            )
    return ExecutionVerdict(ok=True)


class ExecutionWatch:
    """Tracks how long the planned mode has held, so a window is judged only
    once the automation had the whole window to act on it."""

    def __init__(self) -> None:
        self._mode: str | None = None
        self._since: datetime | None = None

    def observe(self, now: datetime, mode: str | None) -> datetime | None:
        """Record the planned mode; return the window start to judge, or None."""
        if mode != self._mode or self._since is None:
            self._mode, self._since = mode, now
        if mode not in GRID_MODES or now - self._since < EXECUTION_WINDOW:
            return None
        return now - EXECUTION_WINDOW
