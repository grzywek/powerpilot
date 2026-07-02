"""Brand asset contract tests for the Home Assistant integration UI."""

from __future__ import annotations

import struct
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
BRAND_DIR = ROOT / "custom_components" / "powerpilot" / "brand"


def _png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as file:
        signature = file.read(8)
        length = file.read(4)
        chunk_type = file.read(4)
        if (
            signature != b"\x89PNG\r\n\x1a\n"
            or len(length) != 4
            or chunk_type != b"IHDR"
        ):
            raise AssertionError(f"{path} is not a PNG with an IHDR header")
        return struct.unpack(">II", file.read(8))


def test_home_assistant_brand_icons_are_packaged() -> None:
    """Custom integrations expose UI icons from their local brand directory."""
    assert _png_size(BRAND_DIR / "icon.png") == (256, 256)
    assert _png_size(BRAND_DIR / "icon@2x.png") == (512, 512)
