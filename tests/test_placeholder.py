"""Placeholder test to verify pytest is wired."""

import tomllib
from pathlib import Path

from vestigo import __version__


def test_version_matches_pyproject() -> None:
    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with pyproject.open("rb") as fh:
        assert __version__ == tomllib.load(fh)["project"]["version"]
