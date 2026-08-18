"""Checks a converter's Parquet must pass before the harness trusts it.

Enforced checks fail the attempt and are fed back to the model verbatim;
reported checks (``enforced=False``) only inform the repair prompt. The report
is JSON-serialisable and is what ``converter_scripts.attempts[].validation``
stores.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from vestigo.converters.generator import sanitize_name
from vestigo.ingestion.parquet_format import (
    META_CONVERTER_NAME,
    META_CONVERTER_VERSION,
    validate_parquet_source,
)

_PROVENANCE = ("source_file", "file_hash", "byte_offset", "content_hash")
_MAX_EXAMPLES = 3


@dataclass
class Check:
    """One named check; ``enforced=False`` means "reported, never fails the attempt"."""

    name: str
    ok: bool
    detail: str
    enforced: bool = True


@dataclass
class ValidationReport:
    """The structured verdict; ``ok`` considers enforced checks only."""

    ok: bool
    checks: list[Check] = field(default_factory=list)
    rows: int = 0
    converter_name: str | None = None
    converter_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "rows": self.rows,
            "converter_name": self.converter_name,
            "converter_version": self.converter_version,
            "checks": [asdict(c) for c in self.checks],
        }


#: Rows per Arrow batch while validating. The Parquet may be as large as
#: ``converter_run_output_mb`` allows (gigabytes); the validator runs in the API
#: process, so it must stay O(batch), never O(file).
_BATCH_ROWS = 65_536


def _fmt_examples(msgs: list[Any]) -> str:
    return " ".join(f"e.g. {str(m)[:200]!r}" for m in msgs)


def _take_examples(batch: pa.RecordBatch, mask: pa.Array, have: list[Any]) -> None:
    """Append up to ``_MAX_EXAMPLES`` messages selected by ``mask`` (a courtesy)."""
    if len(have) >= _MAX_EXAMPLES:
        return
    try:
        if pc.sum(mask).as_py() in (None, 0):
            return  # nothing selected — skip the whole-batch filter
        sub = batch.filter(mask).column("message").slice(0, _MAX_EXAMPLES - len(have))
        have.extend(sub.to_pylist())
    except Exception:  # noqa: BLE001 — examples are a courtesy
        pass


def _unparsed_mask(batch: pa.RecordBatch) -> pa.Array:
    """True where ``attributes.parse_status == "unparsed"``; vectorised, no Python per row."""
    status = pc.map_lookup(batch.column("attributes"), query_key="parse_status", occurrence="first")
    return pc.fill_null(pc.equal(status, "unparsed"), False)


class _OffsetOrder:
    """Tracks whether ``byte_offset`` is non-decreasing per ``source_file`` across batches."""

    def __init__(self) -> None:
        self.ok = True
        self._last: dict[str, int] = {}

    def feed(self, batch: pa.RecordBatch) -> None:
        if not self.ok:
            return
        files = batch.column("source_file")
        offs = batch.column("byte_offset")
        uniq = pc.unique(files)
        if len(uniq) == 1 and offs.null_count == 0 and files.null_count == 0:
            # The common shape — one input file — checks in Arrow: first offset
            # against what the previous batch ended on, then pairwise diffs.
            f = uniq[0].as_py()
            first, last = offs[0].as_py(), offs[len(offs) - 1].as_py()
            if first < self._last.get(f, -1):
                self.ok = False
                return
            # byte_offset is uint64: cast before differencing or a decrease wraps positive.
            diffs = pc.pairwise_diff(pc.cast(offs, pa.int64())) if len(offs) > 1 else None
            if diffs is not None and (pc.min(diffs).as_py() or 0) < 0:
                self.ok = False
                return
            self._last[f] = last
            return
        for f, o in zip(files.to_pylist(), offs.to_pylist(), strict=True):
            if o is None or f is None:
                continue
            if o < self._last.get(f, -1):
                self.ok = False
                return
            self._last[f] = o


def validate_output(
    parquet_path: Path,
    *,
    raw_sha256: str,
    expected_version: int,
    expected_name: str | None = None,
    parse_floor: float = 0.5,
    timestamp_floor: float = 0.5,
) -> ValidationReport:
    """Validate ``parquet_path`` against the contract and the run's own facts.

    Streams the file batch by batch: memory stays bounded by ``_BATCH_ROWS``
    however large the converter's output is. ``expected_name`` (when the
    harness already knows the converter's name — every attempt after the
    first, and every regeneration) is enforced so the footer identity that
    becomes ``Source.parser`` cannot drift from the script row.
    """
    checks: list[Check] = []
    rep = ValidationReport(ok=False)
    try:
        pf = pq.ParquetFile(parquet_path)
    except Exception as exc:  # noqa: BLE001 — anything unreadable is one failed check
        checks.append(Check("footer", False, f"not a readable Parquet file: {exc}"))
        rep.checks = checks
        return rep
    try:
        try:
            meta = validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            checks.append(Check("footer", True, "schema and required footer keys present"))
        except ValueError as exc:
            checks.append(Check("footer", False, str(exc)))
            rep.checks = checks
            return rep
        rep.converter_name, rep.converter_version = meta.converter_name, meta.converter_version
        want = f"{expected_version}.0.0"
        checks.append(
            Check(
                "converter_version",
                meta.converter_version == want,
                f"footer {META_CONVERTER_VERSION}={meta.converter_version!r}, harness expects {want!r}",
            )
        )
        if expected_name is not None:
            checks.append(
                Check(
                    "converter_name",
                    sanitize_name(meta.converter_name or "") == expected_name,
                    f"footer {META_CONVERTER_NAME}={meta.converter_name!r}, harness expects {expected_name!r}",
                )
            )
        got_hash = meta.original_files[0].sha256 if meta.original_files else None
        checks.append(
            Check(
                "original_file_hash",
                got_hash == raw_sha256,
                f"original_files[0].sha256={got_hash!r}, input file sha256={raw_sha256!r}",
            )
        )

        n = 0
        null_counts = dict.fromkeys(_PROVENANCE, 0)
        unparsed = 0
        unparsed_examples: list[Any] = []
        ts_nulls = 0
        ts_examples: list[Any] = []
        ts_min: Any = None
        ts_max: Any = None
        order = _OffsetOrder()
        for batch in pf.iter_batches(batch_size=_BATCH_ROWS):
            n += batch.num_rows
            for c in _PROVENANCE:
                null_counts[c] += batch.column(c).null_count
            mask = _unparsed_mask(batch)
            unparsed += pc.sum(mask).as_py() or 0
            _take_examples(batch, mask, unparsed_examples)
            ts = batch.column("timestamp")
            ts_nulls += ts.null_count
            _take_examples(batch, pc.is_null(ts), ts_examples)
            if ts.null_count < len(ts):
                mm = pc.min_max(ts).as_py()
                ts_min = mm["min"] if ts_min is None else min(ts_min, mm["min"])
                ts_max = mm["max"] if ts_max is None else max(ts_max, mm["max"])
            order.feed(batch)
    finally:
        pf.close()

    rep.rows = n
    checks.append(Check("rows", n >= 1, f"{n} rows"))
    if n == 0:
        rep.checks = checks
        return rep

    bad = {c: k for c, k in null_counts.items() if k}
    checks.append(
        Check("provenance_nulls", not bad, "no nulls" if not bad else f"nulls per column: {bad}")
    )

    parsed = n - unparsed
    checks.append(
        Check(
            "parse_rate",
            parsed / n >= parse_floor,
            f"{parsed}/{n} rows parsed ({parsed / n:.0%}); floor {parse_floor:.0%}. "
            + _fmt_examples(unparsed_examples),
        )
    )

    with_ts = n - ts_nulls
    checks.append(
        Check(
            "timestamps",
            with_ts / n >= timestamp_floor,
            f"{with_ts}/{n} rows have a timestamp ({with_ts / n:.0%}); floor {timestamp_floor:.0%}. "
            + _fmt_examples(ts_examples),
        )
    )
    if with_ts:
        checks.append(Check("time_range", True, f"{ts_min} → {ts_max}", enforced=False))

    checks.append(
        Check(
            "offsets_monotonic",
            order.ok,
            "byte_offset non-decreasing per source_file"
            if order.ok
            else "byte_offset decreases within a source_file — offsets may be wrong",
            enforced=False,
        )
    )

    rep.checks = checks
    rep.ok = all(c.ok for c in checks if c.enforced)
    return rep
