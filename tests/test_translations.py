"""Every entity key and options step has a name in both translation files."""

from __future__ import annotations

import json
from pathlib import Path

from custom_components.powerpilot import const

ROOT = Path(__file__).resolve().parents[1] / "custom_components" / "powerpilot"


def _load(name: str) -> dict:
    return json.loads((ROOT / "translations" / name).read_text(encoding="utf-8"))


def _keys(prefix: str) -> list[str]:
    return [
        getattr(const, attr)
        for attr in dir(const)
        if attr.startswith(prefix) and isinstance(getattr(const, attr), str)
    ]


def test_entity_keys_are_translated_in_pl_and_en() -> None:
    expected = {
        "sensor": _keys("SENSOR_"),
        "binary_sensor": _keys("BINARY_"),
        "number": _keys("NUMBER_"),
    }
    for lang in ("pl.json", "en.json"):
        entity = _load(lang)["entity"]
        for platform, keys in expected.items():
            missing = [k for k in keys if k not in entity.get(platform, {})]
            assert not missing, f"{lang}: {platform} names missing for {missing}"


def test_options_menu_entries_have_a_step_in_pl_and_en() -> None:
    for lang in ("pl.json", "en.json"):
        steps = _load(lang)["options"]["step"]
        menu = steps["init"]["menu_options"]
        for entry in menu:
            if entry == "finish":
                continue
            assert entry in steps, f"{lang}: menu entry {entry!r} has no step"
