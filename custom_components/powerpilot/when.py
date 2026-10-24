"""How text written for later names a moment: always with its day.

Notifications, log lines and gap lists are read later than they are written,
so relative words ("dziś") would go stale and a bare "16:00" leaves the day
to guess. Every moment is spelled out the way the panel writes days that are
not today or tomorrow (``frontend/src/when.ts``): "pt 24.10 16:00", with the
year when it is not the current one.
"""

from __future__ import annotations

from datetime import datetime

from homeassistant.util import dt as dt_util

# datetime.weekday(): Monday is 0.
_WEEKDAY = ("pon", "wt", "śr", "czw", "pt", "sob", "nd")


def moment_label(moment: datetime) -> str:
    """``moment`` in local time with its day: "pt 24.10 16:00"."""
    local = dt_util.as_local(moment)
    year = f".{local.year}" if local.year != dt_util.now().year else ""
    return f"{_WEEKDAY[local.weekday()]} {local:%d.%m}{year} {local:%H:%M}"
