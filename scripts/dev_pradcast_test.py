"""Standalone test for the Pradcast price parser (no network).

Validates retail conversion and confirmed/forecast detection against the real
PriceResponse schema. Run: python3 scripts/dev_pradcast_test.py
"""

from __future__ import annotations

import sys
import types
from datetime import datetime, timedelta
from pathlib import Path

# --- Minimal homeassistant + aiohttp stubs ---
ha = types.ModuleType("homeassistant")
ha_util = types.ModuleType("homeassistant.util")
ha_dt = types.ModuleType("homeassistant.util.dt")
ha_core = types.ModuleType("homeassistant.core")
ha_helpers = types.ModuleType("homeassistant.helpers")
ha_aiohttp = types.ModuleType("homeassistant.helpers.aiohttp_client")

ha_dt.now = lambda: datetime(2026, 6, 22, 12, 0, 0)
ha_dt.parse_datetime = lambda s: None
ha_dt.as_local = lambda d: d
ha_dt.start_of_local_day = lambda d: datetime(d.year, d.month, d.day)
ha_core.HomeAssistant = type("HomeAssistant", (), {})
ha_aiohttp.async_get_clientsession = lambda hass: None

sys.modules.update(
    {
        "homeassistant": ha,
        "homeassistant.util": ha_util,
        "homeassistant.util.dt": ha_dt,
        "homeassistant.core": ha_core,
        "homeassistant.helpers": ha_helpers,
        "homeassistant.helpers.aiohttp_client": ha_aiohttp,
        "aiohttp": types.ModuleType("aiohttp"),
    }
)
sys.modules["aiohttp"].ClientError = type("ClientError", (Exception,), {})
sys.modules["aiohttp"].ClientSession = type("ClientSession", (), {})

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
ps = _load("powerpilot.modules.price_sources", "modules/price_sources.py")

from datetime import date  # noqa: E402

# Confirmed RDN day (horizon null).
confirmed_payload = {
    "date": "2026-06-22",
    "source": "rdn",
    "horizon": None,
    "prices": [
        {"hour": 0, "price": 300.0, "price_kwh": 0.30, "level": "cheap"},
        {"hour": 18, "price": 1200.0, "price_kwh": 1.20, "level": "expensive"},
    ],
}
# Forecast day D+2 (horizon set).
forecast_payload = {
    "date": "2026-06-24",
    "source": "forecast",
    "horizon": "D+2",
    "prices": [{"hour": 14, "price": 250.0, "price_kwh": 0.25, "level": "cheap"}],
}

data = ps.PriceData()
# markup 0.05 PLN/kWh, VAT 1.23 → retail = (price_kwh + 0.05) * 1.23
ps.PradcastPriceSource._merge_day(data, date(2026, 6, 22), confirmed_payload, 0.05, 1.23)
ps.PradcastPriceSource._merge_day(data, date(2026, 6, 24), forecast_payload, 0.05, 1.23)

h0 = datetime(2026, 6, 22, 0)
h18 = datetime(2026, 6, 22, 18)
h14 = datetime(2026, 6, 24, 14)

print("buy[00:00] =", round(data.buy[h0], 4), "(expect", round((0.30 + 0.05) * 1.23, 4), ")")
print("buy[18:00] =", round(data.buy[h18], 4))
print("buy[D+2 14:00] =", round(data.buy[h14], 4))
print("confirmed hours:", sorted(str(h) for h in data.confirmed_hours))
print("levels:", {str(k): v for k, v in data.levels.items()})

assert abs(data.buy[h0] - (0.30 + 0.05) * 1.23) < 1e-9
assert h0 in data.confirmed_hours and h18 in data.confirmed_hours
assert h14 not in data.confirmed_hours, "forecast day must not be confirmed"
assert data.levels[h0] == "cheap" and data.levels[h18] == "expensive"
print("PRADCAST_PARSE_OK")

# --- PriceProfile persistence round-trip ---
ha_storage = types.ModuleType("homeassistant.helpers.storage")


class _Store:  # minimal stub
    def __init__(self, *a, **k):
        pass


ha_storage.Store = _Store
sys.modules["homeassistant.helpers.storage"] = ha_storage

prices = _load("powerpilot.modules.prices", "modules/prices.py")
PriceProfile = prices.PriceProfile

prof = PriceProfile()
day1 = date(2026, 6, 15)  # a Monday
prof.observe(datetime(2026, 6, 15, 13), 0.20)
prof.observe(datetime(2026, 6, 15, 18), 0.90)
prof.mark_date_observed(day1)
# Second Monday folded in → averaged.
prof.observe(datetime(2026, 6, 22, 13), 0.30)
prof.mark_date_observed(date(2026, 6, 22))

assert prof.is_date_observed(day1)
assert abs(prof.value(0, 13) - 0.25) < 1e-9, "Mon 13:00 average of 0.20 and 0.30"
assert abs(prof.value(0, 18) - 0.90) < 1e-9
assert prof.value(2, 5) is None  # unseen slot

restored = PriceProfile.from_dict(prof.to_dict())
assert abs(restored.value(0, 13) - 0.25) < 1e-9
assert restored.is_date_observed(day1)
assert restored.observed_days == 2
print("PROFILE_PERSIST_OK")

