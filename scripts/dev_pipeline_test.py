"""Standalone pipeline test using minimal Home Assistant stubs.

Validates the forecast → optimizer → plan flow without a running HA instance.
Run: python3 scripts/dev_pipeline_test.py
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

# --- Minimal homeassistant stubs (only what the imported modules touch) ---
ha = types.ModuleType("homeassistant")
ha_util = types.ModuleType("homeassistant.util")
ha_dt = types.ModuleType("homeassistant.util.dt")
ha_core = types.ModuleType("homeassistant.core")

_NOW = datetime(2026, 6, 22, 12, 0, 0)
ha_dt.now = lambda: _NOW
ha_dt.parse_datetime = lambda s: None
ha_dt.as_local = lambda d: d


class HomeAssistant:  # noqa: D401 - stub
    pass


ha_core.HomeAssistant = HomeAssistant
ha_core.callback = lambda f: f

sys.modules["homeassistant"] = ha
sys.modules["homeassistant.util"] = ha_util
sys.modules["homeassistant.util.dt"] = ha_dt
sys.modules["homeassistant.core"] = ha_core

# Make the package importable.
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "custom_components"))

# Import leaf modules directly (avoid package __init__ which pulls full HA).
import importlib.util


def _load(name: str, rel: str):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "custom_components" / "powerpilot" / rel
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Register a stub package so relative imports resolve.
pkg = types.ModuleType("powerpilot")
pkg.__path__ = [str(ROOT / "custom_components" / "powerpilot")]
sys.modules["powerpilot"] = pkg
mods_pkg = types.ModuleType("powerpilot.modules")
mods_pkg.__path__ = [str(ROOT / "custom_components" / "powerpilot" / "modules")]
sys.modules["powerpilot.modules"] = mods_pkg

_load("powerpilot.const", "const.py")
_load("powerpilot.models", "models.py")
_load("powerpilot.battery", "battery.py")
_load("powerpilot.modules.base", "modules/base.py")
_load("powerpilot.modules.ev", "modules/ev.py")
optimizer = _load("powerpilot.optimizer", "optimizer.py")

from powerpilot.battery import BatteryModel  # noqa: E402
from powerpilot.models import Forecast, HourSlot  # noqa: E402
from powerpilot.modules.ev import EVRequest  # noqa: E402
from powerpilot.optimizer import ChargeCurve, Optimizer, OptimizerConfig  # noqa: E402

# --- Build a synthetic 24h forecast: cheap nights, expensive evenings ---
slots = []
for i in range(24):
    start = _NOW + timedelta(hours=i)
    hour = start.hour
    price = 0.3 if hour < 6 or 13 <= hour <= 16 else (1.2 if 18 <= hour <= 21 else 0.7)
    slot = HourSlot(start=start, buy_price=price, price_confirmed=True)
    slot.base_consumption_kwh = 0.5
    slots.append(slot)
forecast = Forecast(slots=slots)

battery = BatteryModel(capacity_kwh=10, soc=30, energy_cost=0.0, min_soc=10, max_soc=100)
ev = EVRequest(
    enabled=True,
    required_kwh=20.0,
    charger_kw=3.5,
    phase=1,
    available_hours={s.start for s in slots},
)
opt = Optimizer(
    OptimizerConfig(
        inverter_max_charge_kw=3.0,
        inverter_max_discharge_kw=3.0,
        grid_disconnect_soc=15,
        charge_curve=ChargeCurve(default_kw=3.0),
    )
)
plan = opt.optimize(forecast, battery, ev, reminders=["test reminder"])

print(f"horizon hours: {len(plan.decisions)}  total cost: {plan.total_cost:.2f} PLN")
modes = {}
ev_hours = sum(1 for d in plan.decisions if d.ev_charge)
for d in plan.decisions:
    modes[d.inverter_mode] = modes.get(d.inverter_mode, 0) + 1
print("modes:", modes, "| EV charging hours:", ev_hours)
print("first hour:", plan.current.inverter_mode, "soc", round(plan.current.battery_soc, 1),
      "battery_cost", round(plan.current.battery_energy_cost, 4))
# Spot-check: charging should occur in cheap hours, discharge in expensive ones.
charge_prices = [s.buy_price for s, d in zip(slots, plan.decisions) if d.inverter_mode == "charge"]
discharge_prices = [s.buy_price for s, d in zip(slots, plan.decisions) if d.inverter_mode == "discharge"]
print("charge during prices:", sorted(set(charge_prices)))
print("discharge during prices:", sorted(set(discharge_prices)))
assert all(p <= 0.3 for p in charge_prices), "charging should only happen in cheap hours"
assert all(p >= 0.7 for p in discharge_prices), "discharge only at/above the expensive threshold"
assert plan.current.reminders == ["test reminder"]
print("PIPELINE_OK")
