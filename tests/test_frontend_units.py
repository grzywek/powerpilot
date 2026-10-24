"""The panel's own unit tests (``frontend/test``), run as part of this suite.

The panel's display rules — every moment names its day, for one — live in
TypeScript; running their tests here keeps "the full suite is green" true for
them too instead of depending on someone remembering ``npm test``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"


def test_panel_unit_tests_pass() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.fail("node is not on PATH: the panel's unit tests (frontend/test) cannot run")
    result = subprocess.run(
        [node, "--test", "test/**/*.test.ts"],
        cwd=FRONTEND,
        env={**os.environ, "TZ": "Europe/Warsaw"},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stdout + result.stderr
