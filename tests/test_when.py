"""Moments in text written for later always name their day.

The apartment card once read "Następne przełączenie: 15:00" next to
"17.10 17:59 → 15:00": a bare clock time leaves the day to guess, and a
notification or log line is read after the day it was written in, so relative
words would go stale too.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from homeassistant.util import dt as dt_util

from custom_components.powerpilot.when import moment_label

WARSAW = ZoneInfo("Europe/Warsaw")


def test_a_moment_today_still_carries_its_date() -> None:
    dt_util.set_default_time_zone(WARSAW)
    with freeze_time("2026-10-24T13:46:00+00:00"):
        assert moment_label(datetime(2026, 10, 24, 16, 0, tzinfo=WARSAW)) == "sob 24.10 16:00"


def test_the_label_is_local_time_whatever_zone_the_moment_is_in() -> None:
    dt_util.set_default_time_zone(WARSAW)
    with freeze_time("2026-10-24T13:46:00+00:00"):
        utc = datetime(2026, 10, 26, 20, 0, tzinfo=ZoneInfo("UTC"))  # after the clocks go back
        assert moment_label(utc) == "pon 26.10 21:00"


def test_another_year_adds_the_year() -> None:
    dt_util.set_default_time_zone(WARSAW)
    with freeze_time("2026-10-24T13:46:00+00:00"):
        assert moment_label(datetime(2025, 12, 31, 8, 0, tzinfo=WARSAW)) == "śr 31.12.2025 08:00"
