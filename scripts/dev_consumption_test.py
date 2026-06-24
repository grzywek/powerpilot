"""Standalone test for the consumption learner (no recorder / no network).

Validates energy/power statistics parsing, base = main − devices subtraction, and
the learned-vs-default fallback. Run: python3 scripts/dev_consumption_test.py
"""

from __future__ import annotations

import sys
import types
from datetime import date, datetime, timedelta
from pathlib import Path

# --- Minimal homeassistant stubs ---
ha = types.ModuleType("homeassistant")
ha_util = types.ModuleType("homeassistant.util")
ha_dt = types.ModuleType("homeassistant.util.dt")
ha_core = types.ModuleType("homeassistant.core")
ha_helpers = types.ModuleType("homeassistant.helpers")
ha_storage = types.ModuleType("homeassistant.helpers.storage")
ha_components = types.ModuleType("homeassistant.components")
ha_recorder = types.ModuleType("homeassistant.components.recorder")
ha_rec_stats = types.ModuleType("homeassistant.components.recorder.statistics")

ha_dt.now = lambda: datetime(2026, 6, 22, 12, 0, 0)
ha_dt.as_local = lambda d: d
ha_dt.utc_from_timestamp = lambda ts: datetime.fromtimestamp(ts)
ha_dt.start_of_local_day = lambda d: datetime(d.year, d.month, d.day)
ha_core.HomeAssistant = type("HomeAssistant", (), {})
ha_storage.Store = type("Store", (), {})
ha_recorder.get_instance = lambda hass: None
ha_rec_stats.statistics_during_period = lambda *a, **k: {}

sys.modules.update(
    {
        "homeassistant": ha,
        "homeassistant.util": ha_util,
        "homeassistant.util.dt": ha_dt,
        "homeassistant.core": ha_core,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.storage": ha_storage,
        "homeassistant.components": ha_components,
        "homeassistant.components.recorder": ha_recorder,
        "homeassistant.components.recorder.statistics": ha_rec_stats,
    }
)

ROOT = Path(__file__).resolve().parents[1]
import importlib.util  # noqa: E402

pkg = types.ModuleType("powerpilot")
pkg.__path__ = [str(ROOT / "custom_components" / "powerpilot")]
sys.modules["powerpilot"] = pkg
mods = types.ModuleType("powerpilot.modules")
mods.__path__ = [str(ROOT / "custom_components" / "powerpilot" / "modules")]
sys.modules["powerpilot.modules"] = mods


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "custom_components" / "powerpilot" / rel
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("powerpilot.const", "const.py")
_load("powerpilot.models", "models.py")
_load("powerpilot.profiles", "profiles.py")
_load("powerpilot.modules.base", "modules/base.py")
cons = _load("powerpilot.modules.consumption", "modules/consumption.py")


class _Coord:
    def __init__(self):
        self.config = {}
        self.entry = types.SimpleNamespace(entry_id="test")


module = cons.ConsumptionModule(hass=None, coordinator=_Coord())

# --- Power series: 1500 W mean → 1.5 kWh ---
power_rows = [{"start": datetime(2026, 6, 15, 8).timestamp(), "mean": 1500.0}]
pser = module._power_series(power_rows, "W")
assert abs(list(pser.values())[0] - 1.5) < 1e-9, pser

# --- Energy series: cumulative sum deltas (kWh), with a reset ---
energy_rows = [
    {"start": datetime(2026, 6, 15, 8).timestamp(), "sum": 100.0},
    {"start": datetime(2026, 6, 15, 9).timestamp(), "sum": 101.2},  # +1.2
    {"start": datetime(2026, 6, 15, 10).timestamp(), "sum": 0.5},   # reset → ignored
    {"start": datetime(2026, 6, 15, 11).timestamp(), "sum": 1.0},   # +0.5
]
eser = module._energy_series(energy_rows, "kWh")
vals = [round(v, 3) for v in eser.values()]
assert vals == [1.2, 0.5], vals

# --- Fold a full day from exclusive series: base = main − devices ---
day = date(2026, 6, 15)
base_day = datetime(2026, 6, 15)
# Exclusive root (base) = 0.7, washer exclusive = 0.3 → total main 1.0.
base_excl = {base_day + timedelta(hours=h): 0.7 for h in range(24)}
device_excl = {
    "sensor.washer": {base_day + timedelta(hours=h): 0.3 for h in range(24)},
}
ok = module._fold_day(day, base_excl, device_excl)
assert ok, "full day should fold"
assert module.base.is_date_observed(day)
# base = 1.0 − 0.3 = 0.7 for Monday (weekday 0)
assert abs(module.base_value(0, 8) - 0.7) < 1e-9, module.base_value(0, 8)
assert abs(module.device_value(0, 8) - 0.3) < 1e-9

# --- Incomplete day rejected ---
sparse = {base_day + timedelta(hours=h): 1.0 for h in range(10)}
assert not module._fold_day(date(2026, 6, 16), sparse, {})

# --- Fallback to default before any learning ---
fresh = cons.ConsumptionModule(hass=None, coordinator=_Coord())
assert fresh.base.observed_days == 0
assert fresh.base_value(2, 19) > 0  # default evening shape

print("CONSUMPTION_LEARN_OK")
