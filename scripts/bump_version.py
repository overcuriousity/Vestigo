"""Set the release version in every file that carries it.

Usage: ``uv run python scripts/bump_version.py 1.19.7`` — rewrites
`pyproject.toml`, `src/vestigo/__init__.py`, `frontend/package.json`,
`frontend/package-lock.json` (root and ``packages[""]``) and `uv.lock`'s
``vestigo`` entry in place, each through a pattern anchored on the file's own
structure and the current version, so nothing else in those files moves.
Every file must match exactly once before any is written; the change is then
printed per file. `tests/test_version_parity.py` asserts the result.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

_VERSION = r"\d+\.\d+\.\d+(?:[-.][0-9A-Za-z.]+)?"


def _patterns(old: str) -> dict[Path, str]:
    """One anchored pattern per file; group 1 is the text before the version."""
    o = re.escape(old)
    return {
        REPO / "pyproject.toml": rf'(\[project\]\nname = "vestigo"\nversion = "){o}(")',
        REPO / "src" / "vestigo" / "__init__.py": rf'(^__version__ = "){o}(")',
        # Only the root object is indented two spaces; every other "version" sits deeper.
        REPO / "frontend" / "package.json": rf'(^  "version": "){o}(")',
        REPO / "frontend" / "package-lock.json": (
            rf'(^\{{\n  "name": "frontend",\n  "version": "){o}(")'
            r"|"
            rf'("packages": \{{\n    "": \{{\n      "name": "frontend",\n      "version": "){o}(")'
        ),
        REPO / "uv.lock": rf'(\[\[package\]\]\nname = "vestigo"\nversion = "){o}(")',
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2 or not re.fullmatch(_VERSION, argv[1]):
        print(f"usage: {argv[0]} <new-version>   e.g. 1.19.7", file=sys.stderr)
        return 2
    new = argv[1]
    with (REPO / "pyproject.toml").open("rb") as fh:
        old = tomllib.load(fh)["project"]["version"]
    if old == new:
        print(f"already at {new}")
        return 0

    # The lockfile pattern has two alternatives, so it must match twice.
    expected = {REPO / "frontend" / "package-lock.json": 2}
    rewritten: dict[Path, str] = {}
    for path, pattern in _patterns(old).items():
        text = path.read_text(encoding="utf-8")
        want = expected.get(path, 1)
        text, n = re.subn(
            pattern,
            lambda m: f"{m.group(m.lastindex - 1)}{new}{m.group(m.lastindex)}",
            text,
            flags=re.M,
        )
        if n != want:
            print(
                f"{path.relative_to(REPO)}: expected {want} occurrence(s) of version {old}, found {n}; nothing written",
                file=sys.stderr,
            )
            return 1
        rewritten[path] = text

    for path, text in rewritten.items():
        path.write_text(text, encoding="utf-8")
        print(f"{path.relative_to(REPO)}: {old} -> {new}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
