"""Content-addressed retention of original source files (instance-global)."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from vestigo.core.config import get_settings


def retention_dir() -> Path:
    """Return the directory used for content-addressed source file retention."""
    path = Path(get_settings().source_retention_path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def retention_path(file_hash: str) -> Path:
    """Return the content-addressed path for a retained source file."""
    return retention_dir() / file_hash[:2] / file_hash


def retain_file(tmp_path: Path, retention_path_: Path) -> None:
    if retention_path_.exists():
        return
    retention_path_.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(tmp_path, retention_path_)
    except OSError:
        shutil.copy2(tmp_path, retention_path_)
