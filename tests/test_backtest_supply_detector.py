"""The supply backtest script: confusion matrix over a tiny hourly export."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

SCRIPT = Path(__file__).parent.parent / "scripts" / "backtest_supply_detector.py"


def _load():
    spec = importlib.util.spec_from_file_location("backtest_supply_detector", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _hour(apartment, victron, others, ppe2):
    return {
        "start": "2026-06-01T10:00:00+02:00",
        "apartment_w": apartment,
        "victron_w": victron,
        "others_w": others,
        "ppe2_kwh": ppe2,
    }


def test_main_prints_the_confusion_matrix(tmp_path, capsys) -> None:
    export = {
        "min_apartment_w": 300,
        "margin_w": 100,
        "hours": [
            _hour(800, 900, [50], 0.0),  # net 850 >= 700: victron, truth victron
            _hour(800, 900, [50], 0.0),
            _hour(800, 100, [50], 0.0),  # net 50: meter_2, truth victron (false)
            _hour(800, 100, [50], 0.6),  # meter_2, truth meter_2
            _hour(800, 900, [50], 0.6),  # victron, truth meter_2 (false)
            _hour(100, 100, [50], 0.6),  # undecided, truth meter_2
            _hour(100, 100, [50], 0.0),  # undecided, truth victron
            _hour(100, 100, [50], 0.0),
            _hour(None, 100, [50], 0.0),  # missing
            _hour(800, 100, [None], 0.0),  # missing
        ],
    }
    path = tmp_path / "export.json"
    path.write_text(json.dumps(export))
    module = _load()

    assert module.backtest(export) == {
        "true_meter_2": 1,
        "false_victron": 1,
        "true_victron": 2,
        "false_meter_2": 1,
        "undecided_truth_meter_2": 1,
        "undecided_truth_victron": 2,
        "missing_data": 2,
    }
    assert module.main([str(path)]) == 0
    assert "missing data: 2" in capsys.readouterr().out
