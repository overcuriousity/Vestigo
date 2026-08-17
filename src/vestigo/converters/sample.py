"""Bounded excerpt of a raw text log for the model, plus text/binary detection.

Head takes 70% of the budget, a middle window 15%, the tail 15% — formats change
mid-file and the tail shows the newest timestamps. Whole lines only, absolute
line numbers, so the model can cite them. ``.gz`` is read transparently.
"""

from __future__ import annotations

import bisect
import gzip
import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

_PROBE_BYTES = 8192


class NotTextError(ValueError):
    """The file does not look like text (empty, NUL bytes, or mostly non-printable)."""


@dataclass(frozen=True)
class Sample:
    """The excerpt sent to the model and the facts the task header states."""

    blocks: list[tuple[str, int, str]]
    text: str
    size_bytes: int
    line_count: int
    mtime_iso: str
    sha256: str


def _open(path: Path) -> BinaryIO:
    if path.suffix == ".gz":
        return gzip.open(path, "rb")  # type: ignore[return-value]
    return path.open("rb")


def _assert_text(head: bytes) -> None:
    if not head:
        raise NotTextError("file is empty")
    if b"\x00" in head:
        raise NotTextError("file contains NUL bytes")
    printable = sum(1 for b in head if b in (9, 10, 13) or 32 <= b < 127 or b >= 128)
    if printable / len(head) < 0.7:
        raise NotTextError("file is mostly non-printable")


def assert_text_file(path: Path) -> None:
    """Cheap request-time check: only the first 8 KiB are read."""
    with _open(path) as fh:
        _assert_text(fh.read(_PROBE_BYTES))


def _index_at_byte(offsets: list[int], byte: int) -> int:
    return max(bisect.bisect_right(offsets, byte) - 1, 0)


def build_sample(path: Path, budget_bytes: int) -> Sample:
    """Return the excerpt sent to the model; raises :class:`NotTextError` for binary."""
    assert_text_file(path)
    # Pass 1: line offsets only (bounded memory).
    offsets: list[int] = []
    pos = 0
    with _open(path) as fh:
        for line in fh:
            offsets.append(pos)
            pos += len(line)
    total = pos
    line_count = len(offsets)
    mtime_iso = datetime.fromtimestamp(path.stat().st_mtime, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

    def read_lines(start_idx: int, byte_budget: int) -> str:
        with _open(path) as fh:
            fh.seek(offsets[start_idx])
            chunk = fh.read(byte_budget)
        text = chunk.decode("utf-8", errors="replace")
        # Whole lines only: drop a trailing partial line unless it is the file's last.
        if not text.endswith("\n") and offsets[start_idx] + len(chunk) < total and "\n" in text:
            text = text[: text.rfind("\n") + 1]
        return text.rstrip("\n")

    if total <= budget_bytes:
        blocks = [("head", 1, read_lines(0, total) if line_count else "")]
    else:
        head_b = int(budget_bytes * 0.70)
        mid_b = int(budget_bytes * 0.15)
        tail_b = budget_bytes - head_b - mid_b
        head_text = read_lines(0, head_b)
        head_lines = head_text.count("\n") + 1
        mid_idx = max(_index_at_byte(offsets, total // 2), head_lines)
        mid_text = read_lines(mid_idx, mid_b)
        mid_lines = mid_text.count("\n") + 1
        tail_idx = max(_index_at_byte(offsets, max(total - tail_b, 0)), mid_idx + mid_lines)
        tail_idx = min(tail_idx, line_count - 1)
        tail_text = read_lines(tail_idx, total - offsets[tail_idx])
        blocks = [
            ("head", 1, head_text),
            ("middle", mid_idx + 1, mid_text),
            ("tail", tail_idx + 1, tail_text),
        ]
    text = "\n".join(b[2] for b in blocks)
    return Sample(
        blocks=blocks,
        text=text,
        size_bytes=path.stat().st_size,
        line_count=line_count,
        mtime_iso=mtime_iso,
        sha256=hashlib.sha256(text.encode()).hexdigest(),
    )


def sample_as_file(sample: Sample, dest_dir: Path, filename: str) -> Path:
    """Write the head block as ``dest_dir/filename`` (the sample-phase input file)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / filename
    out.write_text(sample.blocks[0][2] + "\n", encoding="utf-8")
    return out
