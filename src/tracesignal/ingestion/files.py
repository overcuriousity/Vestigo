"""File-level hashing helpers for forensic integrity.

These functions compute hashes over whole source files so uploads can be
idempotently detected and event identities can be made deterministic regardless
of where a file is temporarily stored during ingestion.
"""

from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO

DEFAULT_HASH_ALGORITHM = "sha256"
CHUNK_SIZE = 8192


def _hash_algorithm(algorithm: str) -> hashlib._Hash:
    """Return a fresh hashlib object for ``algorithm``."""
    return hashlib.new(algorithm)


def hash_bytes_iter(
    data: Iterator[bytes],
    algorithm: str = DEFAULT_HASH_ALGORITHM,
) -> str:
    """Return a hex digest of the bytes yielded by ``data``.

    Args:
        data: Iterator yielding byte chunks.
        algorithm: Hash algorithm name accepted by :py:func:`hashlib.new`.

    Returns:
        Lowercase hexadecimal digest string.
    """
    hasher = _hash_algorithm(algorithm)
    for chunk in data:
        hasher.update(chunk)
    return hasher.hexdigest()


def hash_file(
    file: BinaryIO | Path | str,
    algorithm: str = DEFAULT_HASH_ALGORITHM,
    chunk_size: int = CHUNK_SIZE,
) -> str:
    """Return a hex digest of ``file``.

    When ``file`` is a file-like object, the stream is read from the current
    position and then rewound back to the start so the caller can continue to
    consume it (e.g. FastAPI's ``UploadFile.file``).

    Args:
        file: Binary file object, :py:class:`~pathlib.Path`, or path string.
        algorithm: Hash algorithm name accepted by :py:func:`hashlib.new`.
        chunk_size: Number of bytes to read per chunk.

    Returns:
        Lowercase hexadecimal digest string.
    """
    if isinstance(file, (str, Path)):
        path = Path(file)
        hasher = _hash_algorithm(algorithm)
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(chunk_size)
                if not chunk:
                    break
                hasher.update(chunk)
        return hasher.hexdigest()

    # File-like object: read, hash, and rewind.
    hasher = _hash_algorithm(algorithm)
    while True:
        chunk = file.read(chunk_size)
        if not chunk:
            break
        hasher.update(chunk)
    # Some streams do not support seeking; the caller is responsible.
    with contextlib.suppress(OSError, AttributeError):
        file.seek(0)
    return hasher.hexdigest()


def hash_string(content: str, algorithm: str = DEFAULT_HASH_ALGORITHM) -> str:
    """Return a hex digest of ``content``.

    Convenience wrapper around :py:func:`hashlib.new`.
    """
    return hashlib.new(algorithm, content.encode("utf-8")).hexdigest()
