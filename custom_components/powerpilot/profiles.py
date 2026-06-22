"""Reusable weekly (weekday × hour) accumulator.

A small, persistence-friendly building block for learning recurring weekly shapes
(consumption base, per-device loads, ...). Deduplication is by calendar date so a
settled day is folded in exactly once and the stored state stays compact.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime


@dataclass
class WeeklyAccumulator:
    """Running average of a quantity per ``(weekday, hour)``."""

    _sum: dict[tuple[int, int], float] = field(default_factory=dict)
    _count: dict[tuple[int, int], int] = field(default_factory=dict)
    _observed_dates: set[str] = field(default_factory=set)

    def is_date_observed(self, day: date) -> bool:
        return day.isoformat() in self._observed_dates

    def mark_date_observed(self, day: date) -> None:
        self._observed_dates.add(day.isoformat())

    def observe(self, hour_start: datetime, value: float) -> None:
        key = (hour_start.weekday(), hour_start.hour)
        self._sum[key] = self._sum.get(key, 0.0) + value
        self._count[key] = self._count.get(key, 0) + 1

    def value(self, weekday: int, hour: int) -> float | None:
        key = (weekday, hour)
        count = self._count.get(key, 0)
        if count == 0:
            return None
        return self._sum[key] / count

    @property
    def samples(self) -> int:
        return sum(self._count.values())

    @property
    def observed_days(self) -> int:
        return len(self._observed_dates)

    def as_matrix(self) -> dict[str, list[float | None]]:
        days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
        return {
            day: [self.value(weekday, hour) for hour in range(24)]
            for weekday, day in enumerate(days)
        }

    def to_dict(self) -> dict:
        return {
            "sum": {f"{wd}-{hr}": v for (wd, hr), v in self._sum.items()},
            "count": {f"{wd}-{hr}": c for (wd, hr), c in self._count.items()},
            "observed_dates": sorted(self._observed_dates),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "WeeklyAccumulator":
        acc = cls()
        if not data:
            return acc
        for key, value in (data.get("sum") or {}).items():
            wd, hr = (int(x) for x in key.split("-"))
            acc._sum[(wd, hr)] = float(value)
        for key, value in (data.get("count") or {}).items():
            wd, hr = (int(x) for x in key.split("-"))
            acc._count[(wd, hr)] = int(value)
        acc._observed_dates = set(data.get("observed_dates") or [])
        return acc
