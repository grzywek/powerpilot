"""Standalone test for pricing.assemble vs a real seller bill (no network).

Reproduces the seller's per-hour full price to the grosz for 7 sampled hours of
2026-06-26 (Tauron G13 lato + Pstryk, markup 0.08, akcyza 0.005, VAT 23%).
Run: python3 scripts/dev_pricing_test.py
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
pkg = types.ModuleType("powerpilot")
pkg.__path__ = [str(ROOT / "custom_components" / "powerpilot")]
sys.modules["powerpilot"] = pkg


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(
        name, ROOT / "custom_components" / "powerpilot" / rel
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_load("powerpilot.const", "const.py")
pricing = _load("powerpilot.pricing", "pricing.py")

MARKUP = 0.08
EXCISE = 0.005
VAT = 0.23
BASE = 0.0435  # składnik bazowy (jakościowa/kogen/OZE)
OFF, PRZED, POPOL = 0.0392, 0.2203, 0.3898  # strefa: pozaszczyt / szczyt przed-/popołudniowy

# (hour, TGE price_kwh, distribution variable component, expected seller buckets)
#   expected = (prąd, dystrybucja, podatki, RAZEM)
CASES = [
    (0, 0.616, OFF, (0.70, 0.08, 0.19, 0.97)),
    (1, 0.5736, OFF, (0.65, 0.08, 0.18, 0.91)),
    (2, 0.5484, OFF, (0.63, 0.08, 0.17, 0.88)),
    (3, 0.5411, OFF, (0.62, 0.08, 0.17, 0.87)),
    (4, 0.5332, OFF, (0.61, 0.08, 0.17, 0.86)),
    (11, 0.2148, PRZED, (0.29, 0.26, 0.14, 0.69)),
    (21, 1.3522, POPOL, (1.43, 0.43, 0.44, 2.30)),
]

ok = True
for hour, tge, zone, (e_exp, d_exp, t_exp, razem_exp) in CASES:
    dist_net = BASE + zone
    bd = pricing.assemble(
        tge=tge,
        markup=MARKUP,
        dist_net=dist_net,
        excise=EXCISE,
        vat_rate=VAT,
        rounding=pricing.PRICE_ROUNDING_PER_BUCKET,
    )
    energy_bucket = round(bd["energy_net"], 2)
    dist_bucket = round(bd["distribution_net"], 2)
    taxes = round(bd["taxes"], 2)
    total = round(bd["total"], 2)
    match = (energy_bucket, dist_bucket, taxes, total) == (e_exp, d_exp, t_exp, razem_exp)
    ok = ok and match
    flag = "OK " if match else "!! "
    print(
        f"{flag}{hour:02d}:00  prąd {energy_bucket:.2f}  dyst {dist_bucket:.2f}  "
        f"podatki {taxes:.2f}  RAZEM {total:.2f}  "
        f"(sprzedawca {e_exp:.2f}/{d_exp:.2f}/{t_exp:.2f}/{razem_exp:.2f})"
    )

assert ok, "per-bucket pricing does not match the seller bill"

# 'total' rounding differs from the seller on the borderline hour (00:00) but
# stays within a grosz; 'none' returns full precision.
t00 = pricing.assemble(
    tge=0.616, markup=MARKUP, dist_net=BASE + OFF, excise=EXCISE,
    vat_rate=VAT, rounding=pricing.PRICE_ROUNDING_TOTAL,
)
print(f"\n[total mode]  00:00 RAZEM = {t00['total']:.2f} (seller 0.97; off by ≤1 gr by design)")
assert round(abs(t00["total"] - 0.97), 2) <= 0.01

# distribution missing → no invented total
none = pricing.assemble(
    tge=0.5, markup=MARKUP, dist_net=None, excise=EXCISE,
    vat_rate=VAT, rounding=pricing.PRICE_ROUNDING_PER_BUCKET,
)
assert none["total"] is None and none["taxes"] is None

print("\nPRICING_SELLER_PARITY_OK")
