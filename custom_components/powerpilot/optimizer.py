"""Heuristic optimization engine.

Transforms the hourly :class:`Forecast` plus the live battery state and EV request
into a per-hour schedule of :class:`Decision` objects.

This first implementation is a transparent, SoC-aware, price-percentile heuristic.
Its inputs and outputs are deliberately shaped so the heuristic can later be
swapped for an LP/MILP cost-minimizer (ROADMAP Stage 5) without touching the
modules, coordinator or entities.

Key modelling choices:
* The household demand of each slot (``total_consumption_kwh``) can be served from
  the grid (passthrough) or from the battery (discharge).
* The EV charger sits *before* the inverter, so EV energy is always drawn from the
  grid. When the EV charges, the inverter charge power is forced to ``LIMITED``
  because the EV occupies the shared phase.
* The battery is only discharged to cover load when the stored energy cost
  (after losses) is below the current grid price — otherwise it is cheaper to use
  the grid and keep the stored energy for a more expensive hour.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from datetime import datetime

from homeassistant.util import dt as dt_util

from .battery import BatteryModel
from .const import ChargePower, InverterMode
from .models import Decision, Forecast, Plan
from .modules.ev import EVRequest

_LOGGER = logging.getLogger(__name__)


@dataclass
class ChargeCurve:
    """Maps SoC to the maximum allowed charge power."""

    default_kw: float
    segments: list[dict] = field(default_factory=list)  # {soc_from, soc_to, max_kw}

    def max_kw(self, soc: float) -> float:
        for seg in self.segments:
            if seg["soc_from"] <= soc < seg["soc_to"]:
                return float(seg["max_kw"])
        return self.default_kw


@dataclass
class OptimizerConfig:
    """Tunable parameters for the heuristic."""

    inverter_max_charge_kw: float
    inverter_max_discharge_kw: float
    grid_disconnect_soc: float
    charge_curve: ChargeCurve
    cheap_percentile: float = 0.33
    expensive_percentile: float = 0.66


def _percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(pct * (len(ordered) - 1)))))
    return ordered[index]


class Optimizer:
    """Produces a :class:`Plan` from a forecast and the current battery state."""

    def __init__(self, config: OptimizerConfig) -> None:
        self.config = config

    def optimize(
        self,
        forecast: Forecast,
        battery: BatteryModel,
        ev_request: EVRequest | None = None,
        reminders: list[str] | None = None,
    ) -> Plan:
        battery = battery.copy()  # never mutate the live state
        ev_hours = self._plan_ev(forecast, ev_request)

        # Decisions (cheap/expensive thresholds, "battery vs grid" comparisons)
        # all use *total* price = energy + distribution, because that is what
        # the household actually pays per kWh imported from the grid.
        energy_prices = forecast.buy_prices
        median_energy = statistics.median(energy_prices) if energy_prices else 0.0
        total_prices = [
            (slot.buy_price or 0.0) + (slot.distribution_price_kwh or 0.0)
            for slot in forecast.slots
            if slot.buy_price is not None
        ]
        cheap = _percentile(total_prices, self.config.cheap_percentile)
        expensive = _percentile(total_prices, self.config.expensive_percentile)

        decisions: list[Decision] = []
        for index, slot in enumerate(forecast.slots):
            energy_price = (
                slot.buy_price if slot.buy_price is not None else median_energy
            )
            distribution = slot.distribution_price_kwh or 0.0
            total_price = energy_price + distribution
            demand = slot.total_consumption_kwh
            ev_kwh = ev_hours.get(slot.start, 0.0)

            decision = Decision(start=slot.start)
            decision.ev_charge = ev_kwh > 0
            decision.ev_charge_kwh = ev_kwh
            if decision.ev_charge:
                decision.charge_power = ChargePower.LIMITED

            grid_buy = ev_kwh  # EV is always grid-fed

            if total_price <= cheap and battery.usable_charge_headroom_kwh > 0:
                # Cheap hour: charge the battery and serve the house from grid.
                max_kw = self.config.charge_curve.max_kw(battery.soc)
                if decision.ev_charge:
                    max_kw = max(0.0, max_kw - ev_request.charger_kw) if ev_request else max_kw
                charge_grid_kwh = min(
                    max_kw,
                    battery.usable_charge_headroom_kwh / max(battery.charge_efficiency, 0.01),
                )
                # Battery energy cost tracking includes distribution: the
                # household paid full grid price for every kWh that ends up
                # stored. Without this, discharge decisions would prefer the
                # battery even on hours where the stored energy is actually
                # more expensive than the grid.
                stored = battery.charge_from_grid(charge_grid_kwh, total_price)
                decision.inverter_mode = InverterMode.CHARGE
                decision.battery_charge_kwh = stored
                grid_buy += charge_grid_kwh + demand
            elif (
                total_price >= expensive
                and battery.energy_cost < total_price
                and battery.usable_discharge_kwh > 0
            ):
                # Expensive hour and stored energy is cheaper than the grid: discharge.
                delivered, _cost = battery.discharge_to_load(demand)
                decision.inverter_mode = InverterMode.DISCHARGE
                decision.battery_discharge_kwh = delivered
                grid_buy += max(0.0, demand - delivered)
            else:
                # Passthrough: house served from grid, battery idle.
                decision.inverter_mode = InverterMode.PASSTHROUGH
                grid_buy += demand

            decision.grid_buy_kwh = grid_buy
            decision.battery_soc = battery.soc
            decision.battery_energy_cost = battery.energy_cost
            decision.grid_connected = battery.soc >= self.config.grid_disconnect_soc
            decision.energy_cost = grid_buy * energy_price
            decision.distribution_cost = grid_buy * distribution
            decision.hour_cost = decision.energy_cost + decision.distribution_cost

            if index == 0 and reminders:
                decision.reminders = list(reminders)

            decisions.append(decision)

        return Plan(forecast=forecast, decisions=decisions, created_at=dt_util.now())

    def _plan_ev(self, forecast: Forecast, ev_request: EVRequest | None) -> dict[datetime, float]:
        """Allocate EV charging into the cheapest available hours."""
        if ev_request is None or not ev_request.is_actionable:
            return {}

        candidates = [
            slot
            for slot in forecast.slots
            if slot.start in ev_request.available_hours and slot.buy_price is not None
        ]
        candidates.sort(key=lambda s: s.buy_price)

        remaining = ev_request.required_kwh
        per_hour = max(ev_request.charger_kw, 0.1)
        allocation: dict[datetime, float] = {}
        for slot in candidates:
            if remaining <= 0:
                break
            take = min(per_hour, remaining)
            allocation[slot.start] = take
            remaining -= take
        return allocation
