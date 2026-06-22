"""Battery model with cost-after-losses tracking.

Central requirement: PowerPilot must *always* know the price of the energy
currently stored in the battery, after charge/discharge losses and wear.

The battery is modelled as a reservoir holding ``energy_kwh`` at a weighted
average ``energy_cost`` (PLN/kWh). The cost reflects what it effectively cost to
put usable energy into the pack:

* Charging ``g`` kWh from the grid at price ``p``:
    stored      = g * charge_efficiency
    cost added  = g * p              (grid spend)
                + stored * wear_cost (cycling wear, applied to throughput)
    new reservoir cost = (old_energy*old_cost + cost_added) / (old_energy+stored)

* Discharging to deliver ``d`` kWh to the house:
    drawn       = d / discharge_efficiency      (taken from reservoir)
    delivered cost/kWh = reservoir_cost / discharge_efficiency + wear_cost
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class BatteryModel:
    """Stateful battery reservoir tracking SoC and energy cost."""

    capacity_kwh: float
    charge_efficiency: float = 0.95
    discharge_efficiency: float = 0.95
    wear_cost: float = 0.10  # PLN per kWh of usable throughput
    min_soc: float = 10.0  # %
    max_soc: float = 100.0  # %

    # Mutable state.
    soc: float = 50.0  # %
    energy_cost: float = 0.0  # PLN/kWh currently stored

    @property
    def energy_kwh(self) -> float:
        return self.capacity_kwh * self.soc / 100.0

    @property
    def usable_charge_headroom_kwh(self) -> float:
        """Usable energy that can still be stored before hitting max SoC."""
        return max(0.0, self.capacity_kwh * (self.max_soc - self.soc) / 100.0)

    @property
    def usable_discharge_kwh(self) -> float:
        """Usable energy that can still be drawn before hitting min SoC."""
        return max(0.0, self.capacity_kwh * (self.soc - self.min_soc) / 100.0)

    # ------------------------------------------------------------------
    # Operations
    # ------------------------------------------------------------------
    def charge_from_grid(self, grid_kwh: float, grid_price: float) -> float:
        """Charge using ``grid_kwh`` drawn from the grid at ``grid_price``.

        Returns the energy actually stored (kWh), respecting the max-SoC ceiling.
        """
        if grid_kwh <= 0:
            return 0.0

        stored = grid_kwh * self.charge_efficiency
        headroom = self.usable_charge_headroom_kwh
        if stored > headroom:
            stored = headroom
            grid_kwh = stored / self.charge_efficiency if self.charge_efficiency else 0.0

        if stored <= 0:
            return 0.0

        cost_added = grid_kwh * grid_price + stored * self.wear_cost
        old_energy = self.energy_kwh
        new_energy = old_energy + stored
        self.energy_cost = (old_energy * self.energy_cost + cost_added) / new_energy
        self.soc = new_energy / self.capacity_kwh * 100.0
        return stored

    def discharge_to_load(self, demand_kwh: float) -> tuple[float, float]:
        """Discharge to cover up to ``demand_kwh`` of household load.

        Returns ``(delivered_kwh, cost_per_kwh)`` where ``cost_per_kwh`` is the
        effective price of the delivered energy after discharge losses + wear.
        """
        if demand_kwh <= 0:
            return 0.0, 0.0

        deliverable = self.usable_discharge_kwh * self.discharge_efficiency
        delivered = min(demand_kwh, deliverable)
        if delivered <= 0:
            return 0.0, 0.0

        drawn = delivered / self.discharge_efficiency
        new_energy = self.energy_kwh - drawn
        # Reservoir cost per stored kWh is unchanged by discharge; the *delivered*
        # energy simply costs more because of losses + wear.
        cost_per_kwh = self.energy_cost / self.discharge_efficiency + self.wear_cost
        self.soc = max(self.min_soc, new_energy / self.capacity_kwh * 100.0)
        return delivered, cost_per_kwh

    def copy(self) -> "BatteryModel":
        """Return an independent copy for what-if simulation."""
        return BatteryModel(
            capacity_kwh=self.capacity_kwh,
            charge_efficiency=self.charge_efficiency,
            discharge_efficiency=self.discharge_efficiency,
            wear_cost=self.wear_cost,
            min_soc=self.min_soc,
            max_soc=self.max_soc,
            soc=self.soc,
            energy_cost=self.energy_cost,
        )
