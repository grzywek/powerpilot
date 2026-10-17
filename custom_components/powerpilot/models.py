"""Core domain models for PowerPilot.

These are framework-agnostic dataclasses shared by the modules, the optimizer and
the Home Assistant glue. Keeping them free of HA imports makes them trivial to
unit-test.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from .const import (
    SUPPLY_METER_2,
    SUPPLY_SOURCE_LIVE,
    SUPPLY_SOURCE_REPLAY,
    SUPPLY_VICTRON,
    ChargePower,
    InverterMode,
)


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
    # Gross price of this hour's kWh as the seller's monthly cap ("Tarcza")
    # averages it, under the edition in force that month (see
    # ``PriceCapTerms.cap_price``: energy, plus the handling fee when the
    # edition counts it; excise and distribution never). ``None`` when no
    # edition covers the hour, or its components are unknown (sensor price
    # source) — then no cap can be computed for it.
    cap_price_kwh: float | None = None

    # Distribution tariff price for this hour (PLN/kWh). ``None`` means no tariff
    # has been resolved yet — modules and the optimizer should treat this as a
    # missing-configuration signal rather than zero.
    distribution_price_kwh: float | None = None

    # Flat fixed distribution charge for this hour (PLN, gross), i.e. the monthly
    # standing charge spread across the month's hours. Independent of kWh — it is
    # a per-hour amount, not a per-kWh price. ``None`` when no tariff resolved.
    distribution_fixed_hourly: float | None = None

    # Energy (kWh) expected during this hour.
    base_consumption_kwh: float = 0.0  # learned household profile
    extra_load_kwh: float = 0.0  # sub-metered devices + EV + scheduled loads + climate
    # The apartment's own forecast (its meter subtree's device profiles plus the
    # climate loads inside it) while supply detection is active, in every
    # state. It is NOT part of ``total_consumption_kwh``: the optimizer places
    # it on the Victron or on meter 2 per the hour's supply. 0 without
    # detection, when the apartment stays in the demand as any other device.
    apartment_kwh: float = 0.0

    temperature: float | None = None  # °C, for climate modelling

    # Free-form notes attached by modules (e.g. "EV charging window").
    tags: list[str] = field(default_factory=list)

    @property
    def total_consumption_kwh(self) -> float:
        """Demand on the Victron for the hour, the apartment's share left out."""
        return max(0.0, self.base_consumption_kwh + self.extra_load_kwh)

    def victron_consumption_kwh(self, supply: str | None) -> float:
        """Demand the Victron carries with the apartment on ``supply``.

        ``supply`` is the hour's planned supply (``Decision.supply``): on meter
        2 the apartment is not Victron demand; on the Victron, and without
        supply detection (``None``, ``apartment_kwh`` is 0 then), it is.
        """
        if supply == SUPPLY_METER_2:
            return self.total_consumption_kwh
        return self.total_consumption_kwh + self.apartment_kwh

    def ppe2_import_kwh(self, supply: str | None) -> float:
        """What meter 2 imports for the apartment with it on ``supply``.

        The counterpart of :meth:`victron_consumption_kwh`: the apartment's
        forecast on meter 2, nothing in any other state.
        """
        return self.apartment_kwh if supply == SUPPLY_METER_2 else 0.0

    @property
    def total_price_kwh(self) -> float | None:
        """Full gross price per kWh the user sees: energy + distribution, or
        ``None`` if a component is missing.

        Both ``buy_price`` (energy side, incl. seller markup, akcyza and VAT) and
        ``distribution_price_kwh`` are gross. The flat fixed monthly charge is
        **not** folded in — sellers bill it as a separate monthly position, so it
        is excluded from the per-kWh price (``distribution_fixed_hourly`` is still
        tracked for total-cost accounting by the optimizer).
        """
        if self.buy_price is None or self.distribution_price_kwh is None:
            return None
        return self.buy_price + self.distribution_price_kwh


@dataclass
class Decision:
    """The optimizer's verdict for one hour, plus the resulting battery state."""

    start: datetime
    inverter_mode: str = InverterMode.PASSTHROUGH
    charge_power: str = ChargePower.FULL
    ev_charge: bool = False
    # Pack-side energy planned into the car this hour (what the energy-added
    # sensor will report). The grid-side draw — charging losses included — is
    # ``ev_grid_kwh``; with the EV charge efficiency at 1.0 the two are equal.
    ev_charge_kwh: float = 0.0
    ev_grid_kwh: float = 0.0
    # Planned charging duration within the hour (minutes at full charger
    # power); ``None`` when no charging is planned for the hour.
    ev_charge_minutes: int | None = None
    # Projected EV state-of-charge (%) at the end of the hour. ``None`` when the
    # EV SoC sensor is unset so the trajectory can't be projected.
    ev_soc: float | None = None
    # Planned supply of the apartment this hour (``victron`` / ``meter_2``);
    # ``None`` without supply detection. ``grid_buy_kwh`` and the hour's costs
    # are PPE 1's: on meter 2 the apartment's energy is PPE 2's import.
    supply: str | None = None

    # Resulting battery state at the *end* of the hour.
    battery_soc: float = 0.0  # %
    battery_energy_cost: float = 0.0  # PLN/kWh stored, after losses

    # Energy flows during the hour (kWh).
    grid_buy_kwh: float = 0.0
    battery_charge_kwh: float = 0.0
    battery_discharge_kwh: float = 0.0
    # Grid-side charge power (kW) drawn this hour — the "force charge X kW"
    # setpoint for the inverter. This is the grid draw (≈ battery_charge_kwh /
    # η_ch) spread over the slot's plannable window: a full hour for future
    # slots, the remaining minutes for the in-progress hour (whose kWh figures
    # cover only that remainder).
    charge_power_kw: float = 0.0

    # Cost incurred during the hour (PLN), negative = earned.
    # ``hour_cost`` is the *grid* cost for the hour (energy + distribution from
    # the grid). ``energy_cost`` and ``distribution_cost`` break it down.
    # ``battery_use_cost`` is the value of energy drawn from the battery during
    # the hour (battery_discharge_kwh × battery_energy_cost) — exposed so the
    # chart can show "cost served from grid" vs "cost served from battery".
    hour_cost: float = 0.0
    energy_cost: float = 0.0
    distribution_cost: float = 0.0
    battery_use_cost: float = 0.0
    # Flat fixed distribution charge for this hour (PLN, gross). Incurred every
    # hour regardless of grid import; kept separate from ``hour_cost`` (which is
    # the consumption-driven grid cost) and added into ``Plan.total_cost``.
    fixed_cost: float = 0.0

    reminders: list[str] = field(default_factory=list)

    # Debug trace: the inputs the optimizer compared when picking the mode for
    # this hour (thresholds, demand, battery state before the action, branch
    # reason). Populated by the optimizer; surfaced via the debug export.
    trace: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        """Serialise for sensor attributes / charts."""
        return {
            "start": self.start.isoformat(),
            "inverter_mode": self.inverter_mode,
            "charge_power": self.charge_power,
            "ev_charge": self.ev_charge,
            "ev_charge_kwh": round(self.ev_charge_kwh, 3),
            "ev_grid_kwh": round(self.ev_grid_kwh, 3),
            "ev_charge_minutes": self.ev_charge_minutes,
            "ev_soc": round(self.ev_soc, 1) if self.ev_soc is not None else None,
            "supply": self.supply,
            "battery_soc": round(self.battery_soc, 1),
            "battery_energy_cost": round(self.battery_energy_cost, 4),
            "grid_buy_kwh": round(self.grid_buy_kwh, 3),
            "battery_charge_kwh": round(self.battery_charge_kwh, 3),
            "battery_discharge_kwh": round(self.battery_discharge_kwh, 3),
            "charge_power_kw": round(self.charge_power_kw, 3),
            "hour_cost": round(self.hour_cost, 4),
            "energy_cost": round(self.energy_cost, 4),
            "distribution_cost": round(self.distribution_cost, 4),
            "battery_use_cost": round(self.battery_use_cost, 4),
            "fixed_cost": round(self.fixed_cost, 4),
            "reminders": list(self.reminders),
            "trace": dict(self.trace),
        }


@dataclass
class ESSRequest:
    """Calendar-driven instructions for the HOUSE battery (``#ess`` tags).

    ``targets`` are ``(deadline, SoC %, label)`` — be at that level by then.
    ``forced_hours`` are hours the user asked to charge in regardless of price.
    Both are clamped/softened in the optimizer: the pack's own SoC band always
    wins, because an instruction that contradicts physics must degrade into
    "as much as fits", never into an infeasible model that takes the whole
    house plan down with it.
    """

    targets: list[tuple[datetime, float, str]] = field(default_factory=list)
    forced_hours: set[datetime] = field(default_factory=set)


@dataclass(frozen=True)
class BillingPeriod:
    """One point of delivery's month-to-date position under a monthly price cap.

    The seller settles the cap over the WHOLE month: at month end the energy
    bill is ``min(cost, limit × energy)``. ``energy_kwh`` / ``cost_pln`` are the
    observed part (completed hours plus the elapsed part of the running hour);
    the optimizer adds its planned import on top.

    A period is only trustworthy when every hour since the month started is
    covered by both an import reading and a cap price. Missing hours are
    unknown, never zero — ``complete`` is False and the reasons are listed.
    """

    ppe: str
    month_start: datetime  # first hour of the month (local, tz-aware)
    month_end: datetime  # first hour of the next month (exclusive)
    # PLN/kWh GROSS the monthly average is capped at — the terms' own limit,
    # grossed up when they state it net (see ``PriceCapTerms.gross_limit``).
    limit_kwh: float
    energy_kwh: float = 0.0
    cost_pln: float = 0.0
    hours_expected: int = 0
    hours_covered: int = 0
    missing: tuple[str, ...] = ()
    terms_name: str = ""  # the price-cap edition this month is settled under
    # Statistics the recorder is still compiling (the hour that just ended,
    # the running hour's first minutes): counted as soon as they land, never
    # a gap — see ``modules/billing.py``.
    pending: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        return self.hours_covered >= self.hours_expected and not self.missing

    @property
    def average_kwh(self) -> float | None:
        """Observed weighted-average price so far, ``None`` without import."""
        if self.energy_kwh <= 0:
            return None
        return self.cost_pln / self.energy_kwh

    def as_dict(self) -> dict[str, Any]:
        return {
            "ppe": self.ppe,
            "terms": self.terms_name,
            "month_start": self.month_start.isoformat(),
            "month_end": self.month_end.isoformat(),
            "limit_kwh": self.limit_kwh,
            "energy_kwh": round(self.energy_kwh, 3),
            "cost_pln": round(self.cost_pln, 4),
            "average_kwh": (
                round(self.average_kwh, 4) if self.average_kwh is not None else None
            ),
            "hours_expected": self.hours_expected,
            "hours_covered": self.hours_covered,
            "complete": self.complete,
            "missing": list(self.missing),
            "pending": list(self.pending),
        }


@dataclass
class BillingOutcome:
    """The optimizer's month forecast for one :class:`BillingPeriod`.

    Observed month-to-date import plus the import the plan schedules inside the
    horizon plus the estimated import of the month's tail beyond it. The cap
    regime (``cap_binds``) is what the optimizer planned the battery under.
    """

    period: BillingPeriod
    energy_kwh: float
    cost_pln: float  # pre-cap energy cost over the whole month
    # ``None`` when the cap could not be modelled (see ``unavailable_reason``):
    # the numbers above then cover only what is known and the battery was
    # planned without the cap.
    cap_binds: bool | None
    settled_cost_pln: float  # min(cost, limit × energy)
    planned_energy_kwh: float  # scheduled inside the planning horizon
    tail_energy_kwh: float  # estimated beyond the horizon
    unavailable_reason: str | None = None

    @property
    def available(self) -> bool:
        return self.cap_binds is not None

    @property
    def discount_pln(self) -> float:
        return max(0.0, self.cost_pln - self.settled_cost_pln)

    @property
    def average_kwh(self) -> float | None:
        if self.energy_kwh <= 0:
            return None
        return self.cost_pln / self.energy_kwh

    def as_dict(self) -> dict[str, Any]:
        return {
            "period": self.period.as_dict(),
            "energy_kwh": round(self.energy_kwh, 3),
            "cost_pln": round(self.cost_pln, 4),
            "average_kwh": (
                round(self.average_kwh, 4) if self.average_kwh is not None else None
            ),
            "cap_binds": self.cap_binds,
            "available": self.available,
            "unavailable_reason": self.unavailable_reason,
            "settled_cost_pln": round(self.settled_cost_pln, 4),
            "discount_pln": round(self.discount_pln, 4),
            "planned_energy_kwh": round(self.planned_energy_kwh, 3),
            "tail_energy_kwh": round(self.tail_energy_kwh, 3),
        }


@dataclass
class SupplyPlanning:
    """What the optimizer needs to place the apartment between the two meters.

    Given only while supply detection is active (``SupplyModule.active``).
    ``actual`` is the confirmed supply now (``None``: unknown, the apartment is
    planned on the Victron, as stage 2 decided). Switches are modelled only
    when ``actual`` is known and ``switching_reason`` is ``None``; otherwise
    every hour keeps ``actual`` and the reason travels to the plan.

    ``ppe2_periods`` are PPE 2's billing months (the same months as PPE 1's,
    under the same editions), each with its observed month-to-date position;
    empty when PPE 2 cannot be settled. ``unavailable`` lists the windows the
    user is away (calendar trips with their travel margins);
    ``calendar_error`` (a calendar could not be read) makes the user
    unavailable all the time. Switches happen on hour boundaries outside the
    quiet window ``[quiet_start_hour, quiet_end_hour)`` (local hours, wrapping
    past midnight; equal hours = no window), at least ``min_stay_hours`` after
    the previous one, ``last_transition_at`` included; each opened cycle (the
    apartment moved to meter 2) costs ``cycle_threshold_pln``.
    """

    actual: str | None
    switching_reason: str | None = None
    ppe2_periods: list[BillingPeriod] = field(default_factory=list)
    last_transition_at: datetime | None = None
    cycle_threshold_pln: float = 2.0
    min_stay_hours: int = 2
    quiet_start_hour: int = 0
    quiet_end_hour: int = 9
    unavailable: list[tuple[datetime, datetime]] = field(default_factory=list)
    calendar_error: str | None = None
    # Hours (slot starts) the apartment must spend on the Victron: how the
    # recommendations re-solve a plan without one of its cycles
    # (``supply_cycles.py``). Hours between two allowed switch boundaries
    # share one supply, so a listed hour keeps its whole stretch there.
    victron_hours: frozenset[datetime] = frozenset()

    @property
    def switching(self) -> bool:
        """Whether the plan may move the apartment between the meters."""
        return self.actual is not None and self.switching_reason is None

    def in_quiet_window(self, local_hour: int) -> bool:
        start, end = self.quiet_start_hour, self.quiet_end_hour
        if start == end:
            return False
        if start < end:
            return start <= local_hour < end
        return local_hour >= start or local_hour < end

    def user_available(self, moment: datetime) -> bool:
        """Whether the user can operate the switch at ``moment``."""
        if self.calendar_error is not None:
            return False
        return not any(start <= moment < end for start, end in self.unavailable)


@dataclass
class MonthTail:
    """Accounting-only hours past the planning horizon, to the billing month's end.

    The monthly cap depends on the whole month, so the optimizer prices the
    battery against these estimated hours too — but reports decisions only for
    the planning horizon. ``ev_grid_kwh`` is the expected EV import over the
    tail (from the learned driving profile); the LP places it in the cheapest
    tail hours, bounded per hour by ``ev_charger_kw``.
    """

    slots: list[HourSlot] = field(default_factory=list)
    ev_grid_kwh: float = 0.0
    ev_charger_kw: float = 0.0


@dataclass
class Forecast:
    """Ordered hourly horizon shared across the pipeline."""

    slots: list[HourSlot] = field(default_factory=list)

    def __iter__(self):
        return iter(self.slots)

    def __len__(self) -> int:
        return len(self.slots)


@dataclass
class SupplyCycle:
    """A run of planned meter-2 hours of the apartment (stage 3).

    ``id`` is the planned start (ISO) — or, for the cycle already running, the
    actual transition's ``at`` from the supply log. ``planned_return`` is the
    boundary back to the Victron, ``None`` when the cycle lasts past the
    horizon (the month's tail keeps it). ``hours`` spans ``start`` to the
    return (or the horizon's end); ``apartment_kwh`` is the apartment's planned
    energy it moves to PPE 2 within the horizon. ``victron_hours`` are its
    planned meter-2 hours: forcing them back on the Victron is the plan
    without this cycle. ``saving_pln`` is its marginal value by re-solve,
    thresholds excluded (``None`` when it cannot be measured, see
    ``saving_note``).
    """

    id: str
    start: datetime
    planned_return: datetime | None
    hours: float
    apartment_kwh: float
    running: bool
    victron_hours: frozenset[datetime] = frozenset()
    saving_pln: float | None = None
    saving_note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "start": self.start.isoformat(),
            "return": self.planned_return.isoformat() if self.planned_return else None,
            "hours": round(self.hours, 2),
            "apartment_kwh": round(self.apartment_kwh, 3),
            "running": self.running,
            "saving_pln": round(self.saving_pln, 2) if self.saving_pln is not None else None,
            "saving_note": self.saving_note,
        }


@dataclass
class Plan:
    """The optimizer output: a forecast paired with per-hour decisions."""

    forecast: Forecast
    decisions: list[Decision] = field(default_factory=list)
    created_at: datetime | None = None
    # Month forecasts under the seller's price cap, one per billing period the
    # plan touches (empty when no cap is configured / active): PPE 1's months
    # first, then PPE 2's (``outcome.period.ppe``) while supply detection is on.
    billing: list[BillingOutcome] = field(default_factory=list)
    # Whether the plan was free to move the apartment between the meters, and
    # why not (``None`` while modelled, or without supply detection).
    supply_switching: bool = False
    supply_reason: str | None = None
    # The solved objective without the cycle thresholds (PLN): what two plans
    # of the same inputs are compared by (``supply_cycles.py``). ``None`` for
    # a plan that was not solved (an empty horizon).
    comparable_cost_pln: float | None = None
    # Hour boundaries (slot starts) at which the apartment may switch, in
    # order; empty unless switching was modelled.
    supply_boundaries: list[datetime] = field(default_factory=list)
    # The plan's apartment cycles with their savings, and how the
    # recommendations got there: re-solves run, their wall time (s) and the
    # ids of the cycles dropped below the threshold (``supply_cycles.py``).
    supply_cycles: list[SupplyCycle] = field(default_factory=list)
    supply_resolves: int = 0
    supply_resolve_seconds: float = 0.0
    supply_dropped: list[str] = field(default_factory=list)

    @property
    def current(self) -> Decision | None:
        """The decision for the hour we are in right now (first slot)."""
        return self.decisions[0] if self.decisions else None

    def decision_at(self, moment: datetime) -> Decision | None:
        """Decision covering ``moment`` on the hourly plan grid."""
        for decision in self.decisions:
            if decision.start <= moment < decision.start + timedelta(hours=1):
                return decision
        return None

    def victron_consumption_kwh(self, index: int) -> float:
        """Victron demand of forecast slot ``index`` under the planned supply."""
        slot = self.forecast.slots[index]
        decision = self.decisions[index] if index < len(self.decisions) else None
        return slot.victron_consumption_kwh(decision.supply if decision else None)

    def ppe2_import_kwh(self, index: int) -> float:
        """Meter 2's import for forecast slot ``index`` under the planned supply."""
        slot = self.forecast.slots[index]
        decision = self.decisions[index] if index < len(self.decisions) else None
        return slot.ppe2_import_kwh(decision.supply if decision else None)

    @property
    def total_cost(self) -> float:
        # Consumption-driven grid cost plus the flat fixed distribution charge
        # incurred every hour regardless of import.
        return sum(d.hour_cost + d.fixed_cost for d in self.decisions)

    def as_dict(self) -> dict:
        return {
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "total_cost": round(self.total_cost, 4),
            "billing": [b.as_dict() for b in self.billing],
            "supply_switching": self.supply_switching,
            "supply_reason": self.supply_reason,
            "hours": [d.as_dict() for d in self.decisions],
            "forecast": [
                {
                    "start": s.start.isoformat(),
                    "buy_price": s.buy_price,
                    "cap_price_kwh": s.cap_price_kwh,
                    "distribution_price_kwh": s.distribution_price_kwh,
                    "total_price_kwh": s.total_price_kwh,
                    "price_confirmed": s.price_confirmed,
                    "consumption_kwh": round(self.victron_consumption_kwh(i), 3),
                    "apartment_kwh": round(s.apartment_kwh, 3),
                    "temperature": s.temperature,
                }
                for i, s in enumerate(self.forecast.slots)
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
class ValidityRange:
    """A closed/half-open calendar range when a tariff (season) is in force.

    ``valid_from`` / ``valid_to`` are inclusive dates. ``None`` means open-ended.
    A tariff may have several disjoint ranges to model recurring seasons
    (e.g. winter 2024/25 + winter 2025/26 share one definition).
    """

    valid_from: date | None = None
    valid_to: date | None = None
    id: str = field(default_factory=lambda: uuid4().hex)

    def contains(self, day: date) -> bool:
        if self.valid_from is not None and day < self.valid_from:
            return False
        if self.valid_to is not None and day > self.valid_to:
            return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "valid_from": self.valid_from.isoformat() if self.valid_from else None,
            "valid_to": self.valid_to.isoformat() if self.valid_to else None,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ValidityRange":
        return cls(
            id=str(data.get("id") or uuid4().hex),
            valid_from=_parse_date(data.get("valid_from")),
            valid_to=_parse_date(data.get("valid_to")),
        )


@dataclass
class Tariff:
    """A distribution tariff with TOU periods and one or more validity ranges.

    Pricing model: ``base_component_kwh`` is a flat surcharge added to the
    matching period's ``price_kwh`` for every hour. The user is responsible for
    adding a catch-all "pozaszczyt" period (``day_sensor=None``, hours 0-24) so
    that *some* period always matches.

    ``vat_rate`` (e.g. 0.23 for 23%) is applied multiplicatively to the
    distribution price (``base + period_price``). The energy price already
    carries its own VAT from the prices module (``CONF_PRICE_VAT``); this VAT
    only covers the distribution side so the chart can show a single fully
    gross total.
    """

    name: str
    base_component_kwh: float  # PLN/kWh, flat surcharge added to every hour
    periods: list[TariffPeriod] = field(default_factory=list)
    validity_ranges: list[ValidityRange] = field(default_factory=list)
    vat_rate: float = 0.23  # 0.23 = 23% VAT applied to (base + period_price)
    # Fixed monthly charge (net PLN/month, e.g. opłata stała sieciowa +
    # abonamentowa). Spread evenly across the hours of each month — see
    # ``TariffModule.fixed_hourly_for`` — and added as a flat per-hour cost,
    # independent of consumption. VAT (``vat_rate``) is applied on read.
    fixed_monthly_cost: float = 0.0
    id: str = field(default_factory=lambda: uuid4().hex)

    def is_active_on(self, day: date) -> bool:
        if not self.validity_ranges:
            return True  # No ranges declared → always active.
        return any(r.contains(day) for r in self.validity_ranges)

    def earliest_active_start(self, day: date) -> date | None:
        """Return the ``valid_from`` of the range covering ``day`` (or None)."""
        for r in self.validity_ranges:
            if r.contains(day):
                return r.valid_from
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "base_component_kwh": self.base_component_kwh,
            "vat_rate": self.vat_rate,
            "fixed_monthly_cost": self.fixed_monthly_cost,
            "validity_ranges": [r.to_dict() for r in self.validity_ranges],
            "periods": [p.to_dict() for p in self.periods],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Tariff":
        # Back-compat: pre-C.1 stored a single (valid_from, valid_to) + fallback_price_kwh.
        validity_raw = data.get("validity_ranges")
        if not validity_raw:
            legacy_from = data.get("valid_from")
            legacy_to = data.get("valid_to")
            if legacy_from or legacy_to:
                validity_raw = [{"valid_from": legacy_from, "valid_to": legacy_to}]
            else:
                validity_raw = []
        base = data.get("base_component_kwh")
        if base is None:
            base = data.get("fallback_price_kwh", 0.0)
        return cls(
            id=str(data.get("id") or uuid4().hex),
            name=str(data.get("name") or ""),
            base_component_kwh=float(base),
            # Pre-D.1 stored tariffs had no vat_rate → keep them VAT-free until
            # the user re-saves the tariff in the OptionsFlow.
            vat_rate=float(data.get("vat_rate", 0.0)),
            fixed_monthly_cost=float(data.get("fixed_monthly_cost", 0.0)),
            validity_ranges=[ValidityRange.from_dict(r) for r in validity_raw],
            periods=[TariffPeriod.from_dict(p) for p in (data.get("periods") or [])],
        )


def tariff_for_day(tariffs: list[Tariff], day: date) -> Tariff | None:
    """Pick the tariff active on ``day``.

    When multiple tariffs overlap, the one whose covering range has the latest
    ``valid_from`` wins; declaration order breaks final ties.
    """
    candidates = [t for t in tariffs if t.is_active_on(day)]
    if not candidates:
        return None

    def _sort_key(t: Tariff) -> tuple[bool, int]:
        start = t.earliest_active_start(day)
        return (start is None, -(start.toordinal() if start else 0))

    candidates.sort(key=_sort_key)
    return candidates[0]


# ---------------------------------------------------------------------------
# Seller's monthly price cap ("Tarcza")
# ---------------------------------------------------------------------------

# Why a set of terms is unusable — the config flow maps each code to a form
# error, the billing module to a reason on its entities.
CAP_TERMS_DATES_ORDER: str = "cap_terms_dates_order"
CAP_TERMS_WHOLE_MONTHS: str = "cap_terms_whole_months"
CAP_TERMS_LIMIT: str = "cap_terms_limit"
CAP_TERMS_OVERLAP: str = "cap_terms_overlap"


def _is_last_day_of_month(day: date) -> bool:
    return (day + timedelta(days=1)).day == 1


@dataclass
class PriceCapTerms:
    """One edition of the seller's monthly price cap, in force over whole months.

    The seller settles the cap per point of delivery and calendar month as
    ``min(C, limit × E)``, ``C`` being every hour's import at the price the
    terms average. What that price includes, and whether the limit is net or
    gross, changes between editions (Pstryk 2026: energy + handling fee, limit
    0.61 gross; 2027: "energy cost", limit 0.50 net), so each edition carries
    its own dates and basis. Prices and limits leave this class in gross PLN —
    the basis the optimizer plans in. Excise and distribution are billed
    outside every edition so far.
    """

    name: str
    valid_from: date  # first day of a month
    valid_to: date  # last day of a month, inclusive
    limit_kwh: float  # PLN/kWh exactly as the terms state it
    limit_includes_vat: bool = True
    handling_fee_included: bool = True
    id: str = field(default_factory=lambda: uuid4().hex)

    def contains(self, day: date) -> bool:
        return self.valid_from <= day <= self.valid_to

    def cap_price(self, tge: float, markup: float, vat_rate: float) -> float:
        """Gross price of one kWh as these terms average it."""
        energy = tge + (markup if self.handling_fee_included else 0.0)
        return energy * (1.0 + vat_rate)

    def gross_limit(self, vat_rate: float) -> float:
        """The limit in gross PLN/kWh (a net limit is grossed up by ``vat_rate``)."""
        if self.limit_includes_vat:
            return self.limit_kwh
        return self.limit_kwh * (1.0 + vat_rate)

    def problems(self) -> list[str]:
        """Codes of what makes these terms unusable on their own (empty = fine).

        The seller settles whole calendar months, and no edition defines what
        a month only partly under the cap would cost — so the dates must span
        whole months rather than be guessed into one.
        """
        found: list[str] = []
        if self.valid_from > self.valid_to:
            found.append(CAP_TERMS_DATES_ORDER)
        if self.valid_from.day != 1 or not _is_last_day_of_month(self.valid_to):
            found.append(CAP_TERMS_WHOLE_MONTHS)
        if self.limit_kwh <= 0:
            found.append(CAP_TERMS_LIMIT)
        return found

    def overlaps(self, other: "PriceCapTerms") -> bool:
        return self.valid_from <= other.valid_to and other.valid_from <= self.valid_to

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "valid_from": self.valid_from.isoformat(),
            "valid_to": self.valid_to.isoformat(),
            "limit_kwh": self.limit_kwh,
            "limit_includes_vat": self.limit_includes_vat,
            "handling_fee_included": self.handling_fee_included,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PriceCapTerms":
        """Decode stored terms; ``ValueError`` when a required field is unusable."""
        valid_from = _parse_date(data.get("valid_from"))
        valid_to = _parse_date(data.get("valid_to"))
        if valid_from is None or valid_to is None:
            raise ValueError("price cap terms need both validity dates")
        limit = data.get("limit_kwh")
        if limit is None:
            raise ValueError("price cap terms need a limit")
        return cls(
            id=str(data.get("id") or uuid4().hex),
            name=str(data.get("name") or ""),
            valid_from=valid_from,
            valid_to=valid_to,
            limit_kwh=float(limit),
            limit_includes_vat=bool(data.get("limit_includes_vat", True)),
            handling_fee_included=bool(data.get("handling_fee_included", True)),
        )


def price_cap_problems(terms: list[PriceCapTerms]) -> list[str]:
    """Codes of what makes a set of terms unusable (each edition + overlaps)."""
    found: list[str] = []
    for i, current in enumerate(terms):
        for code in current.problems():
            if code not in found:
                found.append(code)
        if any(current.overlaps(other) for other in terms[i + 1 :]):
            if CAP_TERMS_OVERLAP not in found:
                found.append(CAP_TERMS_OVERLAP)
    return found


def price_cap_terms_for_day(
    terms: list[PriceCapTerms], day: date
) -> PriceCapTerms | None:
    """The edition in force on ``day`` (terms never overlap once validated)."""
    for current in terms:
        if current.contains(day):
            return current
    return None


# ---------------------------------------------------------------------------
# Apartment supply
# ---------------------------------------------------------------------------

_SUPPLY_STATES = (SUPPLY_VICTRON, SUPPLY_METER_2)
_SUPPLY_SOURCES = (SUPPLY_SOURCE_LIVE, SUPPLY_SOURCE_REPLAY)


def _parse_utc(value: Any, name: str) -> datetime:
    """An aware ISO timestamp from storage, in UTC; ``ValueError`` otherwise."""
    if not isinstance(value, str):
        raise ValueError(f"supply transition needs {name!r} as an ISO timestamp")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError(f"supply transition {name!r} has no time zone: {value!r}")
    return parsed.astimezone(timezone.utc)


def _parse_watts(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"supply transition needs {name!r} as a number")
    if not math.isfinite(value):
        raise ValueError(f"supply transition {name!r} is not finite")
    return float(value)


@dataclass(frozen=True)
class SupplyTransition:
    """A confirmed change of the apartment's supply (see ``modules/supply.py``).

    The switch happened inside ``[not_before, at]``: ``at`` is the first of the
    N agreeing samples, ``not_before`` the last decisive sample of the previous
    state (``None`` for the first confirmation ever, when there was none).
    """

    state: str  # SUPPLY_VICTRON | SUPPLY_METER_2
    at: datetime  # aware, UTC
    not_before: datetime | None
    apartment_w: float  # evidence at confirmation
    victron_net_w: float  # Victron output − the other sub-meters behind it
    source: str  # SUPPLY_SOURCE_LIVE | SUPPLY_SOURCE_REPLAY

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "at": self.at.isoformat(),
            "not_before": self.not_before.isoformat() if self.not_before else None,
            "apartment_w": self.apartment_w,
            "victron_net_w": self.victron_net_w,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SupplyTransition":
        """Decode a stored transition; ``ValueError`` when it is unusable.

        Only ``not_before`` is optional (absent = the first confirmation); a
        transition without its state, time, evidence or source is not guessed.
        """
        if not isinstance(data, Mapping):
            raise ValueError("supply transition must be a mapping")
        state = data.get("state")
        if state not in _SUPPLY_STATES:
            raise ValueError(f"unknown supply state {state!r}")
        source = data.get("source")
        if source not in _SUPPLY_SOURCES:
            raise ValueError(f"unknown supply transition source {source!r}")
        not_before = data.get("not_before")
        return cls(
            state=state,
            at=_parse_utc(data.get("at"), "at"),
            not_before=None if not_before is None else _parse_utc(not_before, "not_before"),
            apartment_w=_parse_watts(data.get("apartment_w"), "apartment_w"),
            victron_net_w=_parse_watts(data.get("victron_net_w"), "victron_net_w"),
            source=source,
        )
