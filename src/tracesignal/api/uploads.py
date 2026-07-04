"""Shared upload plumbing for API endpoints that receive files.

Lives in the API layer (not ``ingestion/``) because it maps
``UploadTooLargeError`` to an HTTP 413 — ``ingestion/files.py`` stays
framework-free.
"""

from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path

from fastapi import HTTPException, UploadFile

from tracesignal.ingestion.files import UploadTooLargeError, copy_and_hash


async def receive_upload_to_tmp(
    file: UploadFile,
    *,
    max_bytes: int | None,
    suffix: str,
) -> tuple[Path, str, int]:
    """Stream an upload into a temp file off the event loop, hashing as it copies.

    Returns ``(tmp_path, sha256_hex, size_bytes)``. Raises ``HTTPException``
    413 when the stream exceeds ``max_bytes``; the temp file is unlinked on
    every failure path. On success the caller owns ``tmp_path`` and must
    unlink or move it on each of its own subsequent branches.
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=suffix)  # noqa: SIM115
    tmp_path = Path(tmp.name)
    try:
        with tmp:
            sha256, size_bytes = await asyncio.to_thread(
                copy_and_hash, file.file, tmp, chunk_size=1024 * 1024, max_bytes=max_bytes
            )
    except UploadTooLargeError as exc:
        tmp_path.unlink(missing_ok=True)
        raise HTTPException(
            status_code=413,
            detail=(
                f"{exc}. Raise TS_MAX_UPLOAD_BYTES (0 disables the limit) or ingest "
                "the file server-side with 'tsig ingest', which avoids the HTTP "
                "upload and its temp copy entirely."
            ),
        ) from exc
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return tmp_path, sha256, size_bytes
