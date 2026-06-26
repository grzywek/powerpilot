#!/usr/bin/env python3
"""Policz kolejną wersję integracji i wpisz ją do manifest.json.

Reguły (conventional commits, liczone z commitów od ostatniego tagu vX.Y.Z):
  - BREAKING CHANGE w treści lub `typ!: ...` w nagłówku -> major
  - `feat: ...` / `feat(scope): ...`                    -> minor
  - cokolwiek innego                                    -> patch

Jeśli tag dla bieżącej wersji z manifestu jeszcze nie istnieje, wydajemy tę
wersję bez bumpa (obsługuje pierwszy release oraz ręczne podbicie w manifeście).

Skrypt wypisuje na stdout (do wczytania przez workflow):
  VERSION=<nowa-wersja>
  CHANGED=<true|false>
"""
from __future__ import annotations

import json
import re
import subprocess
import sys

MANIFEST = "custom_components/powerpilot/manifest.json"

BANG_RE = re.compile(r"^[a-z]+(\([^)]+\))?!:", re.MULTILINE)
FEAT_RE = re.compile(r"^feat(\([^)]+\))?:", re.MULTILINE)
BREAKING_RE = re.compile(r"^BREAKING CHANGE", re.MULTILINE)


def git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], capture_output=True, text=True, check=True
    ).stdout


def tag_exists(tag: str) -> bool:
    return (
        subprocess.run(
            ["git", "rev-parse", "-q", "--verify", f"refs/tags/{tag}"],
            capture_output=True,
        ).returncode
        == 0
    )


def bump_kind(log: str) -> str:
    if BREAKING_RE.search(log) or BANG_RE.search(log):
        return "major"
    if FEAT_RE.search(log):
        return "minor"
    return "patch"


def next_version(current: str, kind: str) -> str:
    major, minor, patch = (int(p) for p in current.split("."))
    if kind == "major":
        return f"{major + 1}.0.0"
    if kind == "minor":
        return f"{major}.{minor + 1}.0"
    return f"{major}.{minor}.{patch + 1}"


def main() -> int:
    manifest_path = sys.argv[1] if len(sys.argv) > 1 else MANIFEST
    with open(manifest_path) as f:
        data = json.load(f)
    current = data["version"]

    tag = f"v{current}"
    if not tag_exists(tag):
        new = current
        print(f"Brak tagu {tag} — wydaję bieżącą wersję bez bumpa.", file=sys.stderr)
    else:
        log = git("log", f"{tag}..HEAD", "--format=%B")
        kind = bump_kind(log)
        new = next_version(current, kind)
        print(f"Bump [{kind}]: {current} -> {new}", file=sys.stderr)

    changed = new != current
    if changed:
        data["version"] = new
        with open(manifest_path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")

    print(f"VERSION={new}")
    print(f"CHANGED={'true' if changed else 'false'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
