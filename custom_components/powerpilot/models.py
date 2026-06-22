"""Core domain models for PowerPilot.

These are framework-agnostic dataclasses shared by the modules, the optimizer and
the Home Assistant glue. Keeping them free of HA imports makes them trivial to
unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any
from uuid import uuid4

from .const import ChargePower, InverterMode


@dataclass
class HourSlot:
    """Everything known about a single hour of the horizon.

    Modules *add* information to a slot; they never mutate another module's data.
    """

    start: datetime

    # Prices (PLN/kWh). ``price_confirmed`` is True once the operator has
    # published binding prices (typically D+1), otherwise the value is a forecast.
    buy_price: float | None = None
    price_confirmed: bool = False

    # Distribution tariff price for this hour (PLN/kWh). ``None`` means no tariff
    # has been resolved yet — modules and the optimizer should treat this as a
    # missing-configuration signal rather than zero.
    distribution_price_kwh: float | None = None

    # Energy (kWh) expected during this hour.
    base_consumption_kwh: float = 0.0  # learned household profile
    extra_load_kwh: float = 0.0  # EV + scheduled appliances + climate

    temperature: float | None = None  # °C, for climate modelling

    # Free-form notes attached by modules (e.g. "EV charging window").
    tags: list[str] = field(default_factory=list)

    @property
    def total_consumption_kwh(self) -> float:
        """Total demand for the hour."""
        return max(0.0, self.base_consumption_kwh + self.extra_load_kwh)

    @property
    def total_price_kwh(self) -> float | None:
        """Energy price + distribution price (PLN/kWh), or ``None`` if either is missing."""
        if self.buy_price is None or self.distribution_price_kwh is None:
            return None
        return self.buy_price + self.distribution_price_kwh


@dataclass
class Decision:
    """The optimizer's verdict for one hour, plus the resulting battery state."""

    start: datetime
    inverter_mode: str = InverterMode.PASSTHROUGH
    charge_power: str = ChargePower.FULL
    grid_connected: bool = True
    ev_charge: bool = False
    ev_charge_kwh: float = 0.0

    # Resulting battery state at the *end* of the hour.
    battery_soc: float = 0.0  # %
    battery_energy_cost: float = 0.0  # PLN/kWh stored, after losses

    # Energy flows during the hour (kWh).
    grid_buy_kwh: float = 0.0
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0

    # Cost incurred during the hour (PLN), negative = earned.
    # ``hour_cost`` is the *total* cost for the hour (energy + distribution).
    # ``energy_cost`` and ``distribution_cost`` break it down for the chart.
    hour_cost: float = 0.0
    energy_cost: float = 0.0
    distribution_cost: float = 0.0

    reminders: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        """Serialise for sensor attributes / charts."""
        return {
            "start": self.start.isoformat(),
            "inverter_mode": self.inverter_mode,
            "charge_power": self.charge_power,
            "grid_connected": self.grid_connected,
            "ev_charge": self.ev_charge,
            "ev_charge_kwh": round(self.ev_charge_kwh, 3),
            "battery_soc": round(self.battery_soc, 1),
            "battery_energy_cost": round(self.battery_energy_cost, 4),
            "grid_buy_kwh": round(self.grid_buy_kwh, 3),
            "battery_charge_kwh": round(self.battery_charge_kwh, 3),
            "battery_discharge_kwh": round(self.battery_discharge_kwh, 3),
            "hour_cost": round(self.hour_cost, 4),
            "energy_cost": round(self.energy_cost, 4),
            "distribution_cost": round(self.distribution_cost, 4),
            "reminders": list(self.reminders),
        }


@dataclass
class Forecast:
    """Ordered hourly horizon shared across the pipeline."""

    slots: list[HourSlot] = field(default_factory=list)

    def __iter__(self):
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.slots)

    @property
    def buy_prices(self) -> list[float]:
        return [s.buy_price for s in self.slots if s.buy_price is not None]


@dataclass
class Plan:
    """The optimizer output: a forecast paired with per-hour decisions."""

    forecast: Forecast
    decisions: list[Decision] = field(default_factory=list)
    created_at: datetime | None = None

    @property
    def current(self) -> Decision | None:
        """The decision for the hour we are in right now (first slot)."""
        return self.decisions[0] if self.decisions else None

    @property
    def total_cost(self) -> float:
        return sum(d.hour_cost for d in self.decisions)

    def as_dict(self) -> dict:
        return {
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "total_cost": round(self.total_cost, 4),
            "hours": [d.as_dict() for d in self.decisions],
            "forecast": [
                {
                    "start": s.start.isoformat(),
                    "buy_price": s.buy_price,
                    "distribution_price_kwh": s.distribution_price_kwh,
                    "total_price_kwh": s.total_price_kwh,
                    "price_confirmed": s.price_confirmed,
                    "consumption_kwh": round(s.total_consumption_kwh, 3),
                    "temperature": s.temperature,
                }
                for s in self.forecast.slots
            ],
        }


# ---------------------------------------------------------------------------
# Distribution tariffs
# ---------------------------------------------------------------------------


def _parse_date(value: str | date | None) -> date | None:
    """Accept ISO dates from storage or already-parsed ``date`` instances."""
    if value is None or value == "":
        return None
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


@dataclass
class TariffPeriod:
    """One time-of-use band inside a distribution tariff.

    A period matches an hour H when:
      * H's local-time hour-of-day is in ``[hour_from, hour_to)``
      * if ``day_sensor`` is set, the binary_sensor evaluates to ``on`` for the
        calendar day of H (the module is responsible for picking the right sensor
        for today / D+1..D+7 / historical)
    """

    name: str
    hour_from: int  # inclusive, 0..23
    hour_to: int  # exclusive, 1..24
    price_kwh: float  # PLN/kWh
    day_sensor: str | None = None  # entity_id, ``None`` means "every day"
    id: str = field(default_factory=lambda: uuid4().hex)

    def matches_hour(self, hour: int) -> bool:
        """Whether this period covers the given hour-of-day (0..23)."""
        if self.hour_from <= self.hour_to:
            return self.hour_from <= hour < self.hour_to
        # Wrap-around (e.g. 22 → 6): covers [22,24) and [0,6).
        return hour >= self.hour_from or hour < self.hour_to

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "hour_from": self.hour_from,
            "hour_to": self.hour_to,
            "price_kwh": self.price_kwh,
            "day_sensor": self.day_sensor,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TariffPeriod":
        return cls(
            id=str(data.get("id") or uuid4().hex),
            name=str(data.get("name") or ""),
            hour_from=int(data.get("hour_from", 0)),
            hour_to=int(data.get("hour_to", 24)),
            price_kwh=float(data.get("price_kwh", 0.0)),
            day_sensor=data.get("day_sensor") or None,
        )


@dataclass
class Tariff:
    """A distribution tariff, valid on a date range, composed of TOU periods.

    Periods are evaluated *in order* — the first matching period wins. If no
    period matches, ``fallback_price_kwh`` is used.
    """

    name: str
    fallback_price_kwh: float  # PLN/kWh, used when no period matches
    valid_from: date | None = None  # inclusive, ``None`` = open-ended past
    valid_to: date | None = None  # inclusive, ``None`` = open-ended future
    periods: list[TariffPeriod] = field(default_factory=list)
    id: str = field(default_factory=lambda: uuid4().hex)

    def is_active_on(self, day: date) -> bool:
        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "fallback_price_kwh": self.fallback_price_kwh,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
            "periods": [p.to_dict() for p in self.periods],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tariff":
        return cls(
            id=str(data.get("id") or uuid4().hex),
            name=str(data.get("name") or ""),
            fallback_price_kwh=float(data.get("fallback_price_kwh", 0.0)),
            valid_from=_parse_date(data.get("valid_from")),
            valid_to=_parse_date(data.get("valid_to")),
            periods=[TariffPeriod.from_dict(p) for p in (data.get("periods") or [])],
        )


def tariff_for_day(tariffs: list[Tariff], day: date) -> Tariff | None:
    """Pick the tariff active on ``day``.

    When multiple tariffs overlap (shouldn't happen but is possible if the user
    misconfigured ranges), the one with the *most specific* ``valid_from`` wins,
    falling back to declaration order.
    """
    candidates = [t for t in tariffs if t.is_active_on(day)]
    if not candidates:
        return None
    candidates.sort(
        key=lambda t: (
            t.valid_from is None,  # bounded ranges sort first
            -(t.valid_from.toordinal() if t.valid_from else 0),
        )
    )
    return candidates[0]

