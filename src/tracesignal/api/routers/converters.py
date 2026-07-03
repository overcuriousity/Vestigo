"""API routes for the vendored log-normalization converter downloads.

The converters are self-contained, stdlib-only Python scripts vendored from
https://github.com/overcuriousity/2timesketch (see ``scripts/vendor_converters.py``).
They are shipped as package data so downloads work fully offline.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse

from tracesignal.api.deps import get_current_user
from tracesignal.db.postgres import User

router = APIRouter(prefix="/api/converters", tags=["converters"])

ASSETS_DIR = Path(__file__).resolve().parent.parent.parent / "assets" / "converters"


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    return json.loads((ASSETS_DIR / "manifest.json").read_text(encoding="utf-8"))


@router.get("")
async def list_converters(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """List the available converter scripts with upstream provenance metadata."""
    return _manifest()


@router.get("/{name}")
async def download_converter(name: str, user: User = Depends(get_current_user)) -> FileResponse:
    """Download one converter script by manifest name."""
    entry = next((c for c in _manifest()["converters"] if c["name"] == name), None)
    if entry is None:
        raise HTTPException(status_code=404, detail="Converter not found")
    return FileResponse(
        ASSETS_DIR / entry["filename"],
        media_type="text/x-python",
        filename=entry["filename"],
    )
