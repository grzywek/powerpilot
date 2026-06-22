"""Core domain models for PowerPilot.

These are framework-agnostic dataclasses shared by the modules, the optimizer and
the Home Assistant glue. Keeping them free of HA imports makes them trivial to
unit-test.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

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
    sell_price: float | None = None
    price_confirmed: bool = False

    # Energy (kWh) expected during this hour.
    base_consumption_kwh: float = 0.0  # learned household profile
    extra_load_kwh: float = 0.0  # EV + scheduled appliances + climate
    pv_kwh: float = 0.0  # optional self-production

    temperature: float | None = None  # °C, for climate modelling

    # Free-form notes attached by modules (e.g. "EV charging window").
    tags: list[str] = field(default_factory=list)

    @property
    def total_consumption_kwh(self) -> float:
        """Total demand for the hour, net of PV."""
        return max(0.0, self.base_consumption_kwh + self.extra_load_kwh - self.pv_kwh)


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
    grid_sell_kwh: float = 0.0
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0

    # Cost incurred during the hour (PLN), negative = earned.
    hour_cost: float = 0.0

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
            "grid_sell_kwh": round(self.grid_sell_kwh, 3),
            "battery_charge_kwh": round(self.battery_charge_kwh, 3),
            "battery_discharge_kwh": round(self.battery_discharge_kwh, 3),
            "hour_cost": round(self.hour_cost, 4),
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
        }
