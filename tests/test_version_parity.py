"""Every file that carries the release version agrees with `pyproject.toml`.

A release bump touches six places by hand (or through
``scripts/bump_version.py``): the Python package, the frontend package and its
lockfile (twice), and `uv.lock`. Only the first two were ever checked, and
`frontend/package.json` is what the UI shows as ``__APP_VERSION__``.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from vestigo import __version__

REPO = Path(__file__).resolve().parents[1]


def _toml(path: Path) -> dict:
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _json(path: Path) -> dict:
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def test_every_version_field_matches_pyproject() -> None:
    expected = _toml(REPO / "pyproject.toml")["project"]["version"]
    lock = _json(REPO / "frontend" / "package-lock.json")
    uv_lock = _toml(REPO / "uv.lock")
    (vestigo_entry,) = [p for p in uv_lock["package"] if p["name"] == "vestigo"]

    assert {
        "src/vestigo/__init__.py": __version__,
        "frontend/package.json": _json(REPO / "frontend" / "package.json")["version"],
        "frontend/package-lock.json": lock["version"],
        'frontend/package-lock.json packages[""]': lock["packages"][""]["version"],
        "uv.lock": vestigo_entry["version"],
    } == dict.fromkeys(
        [
            "src/vestigo/__init__.py",
            "frontend/package.json",
            "frontend/package-lock.json",
            'frontend/package-lock.json packages[""]',
            "uv.lock",
        ],
        expected,
    )
