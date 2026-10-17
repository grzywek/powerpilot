"""Backtest the apartment-supply rule on exported hourly statistics.

Checks the rule of ``custom_components/powerpilot/modules/supply.py`` against
the second point of delivery's own import statistic: an hour in which meter 2
imported energy is the ground truth "the apartment was on meter 2".

Usage::

    python3 scripts/backtest_supply_detector.py export.json

Producing ``export.json`` from a Home Assistant instance: call the
``recorder/statistics_during_period`` websocket command (Developer tools
cannot do it; use any websocket client with a long-lived token) once with
``period: "hour"`` and ``types: ["mean"]`` for the apartment power sensor, the
Victron output power sensor and each other sub-meter's power sensor behind the
Victron, and once with ``types: ["change"]`` for the long-term statistic of the
second point of delivery's hourly import (kWh). Align the answers by hour
start and write::

    {"min_apartment_w": 300, "margin_w": 100,
     "hours": [{"start": "2026-06-01T10:00:00+02:00",
                "apartment_w": 850.0,      # hourly mean, W (null = no data)
                "victron_w": 1020.0,       # hourly mean of the Victron output, W
                "others_w": [40.0, 15.5],  # hourly means of the other sub-meters, W
                "ppe2_kwh": 0.0},          # meter 2 hourly import, kWh
               ...]}

Power in kW must be converted to W before export. Standard library only: the
script does not import Home Assistant.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Hourly import above this (kWh) counts as "the apartment was on meter 2".
PPE2_TRUTH_KWH = 0.05

VICTRON = "victron"
METER_2 = "meter_2"


def classify_sample(apartment_w, victron_net_w, min_apartment_w, margin_w):
    """Same rule as ``classify_sample`` in ``modules/supply.py`` (keep in sync)."""
    if apartment_w < min_apartment_w:
        return None
    return VICTRON if victron_net_w >= apartment_w - margin_w else METER_2


def backtest(data: dict) -> dict[str, int]:
    """Count the rule's verdicts against the PPE 2 truth for every hour."""
    min_w = float(data["min_apartment_w"])
    margin_w = float(data["margin_w"])
    counts = {
        "true_meter_2": 0,  # truth meter_2, decided meter_2
        "false_victron": 0,  # truth meter_2, decided victron
        "true_victron": 0,  # truth victron, decided victron
        "false_meter_2": 0,  # truth victron, decided meter_2
        "undecided_truth_meter_2": 0,
        "undecided_truth_victron": 0,
        "missing_data": 0,
    }
    for hour in data["hours"]:
        apartment = hour.get("apartment_w")
        victron = hour.get("victron_w")
        others = hour.get("others_w") or []
        ppe2 = hour.get("ppe2_kwh")
        if (
            apartment is None
            or victron is None
            or ppe2 is None
            or not others
            or any(o is None for o in others)
        ):
            counts["missing_data"] += 1
            continue
        truth = METER_2 if ppe2 > PPE2_TRUTH_KWH else VICTRON
        decided = classify_sample(apartment, victron - sum(others), min_w, margin_w)
        if decided is None:
            counts[f"undecided_truth_{truth}"] += 1
        elif truth == METER_2:
            counts["true_meter_2" if decided == METER_2 else "false_victron"] += 1
        else:
            counts["true_victron" if decided == VICTRON else "false_meter_2"] += 1
    return counts


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__)
        return 2
    counts = backtest(json.loads(Path(args[0]).read_text()))
    total = sum(counts.values())
    print(f"hours: {total}")
    print("                  decided meter_2   decided victron   undecided")
    print(
        f"truth meter_2     {counts['true_meter_2']:>15}   {counts['false_victron']:>15}"
        f"   {counts['undecided_truth_meter_2']:>9}"
    )
    print(
        f"truth victron     {counts['false_meter_2']:>15}   {counts['true_victron']:>15}"
        f"   {counts['undecided_truth_victron']:>9}"
    )
    print(f"missing data: {counts['missing_data']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
