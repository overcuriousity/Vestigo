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

from vestigo.ingestion.parquet_format import META_CONVERTER_VERSION, validate_parquet_source

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


def _examples(table: pa.Table, mask: pa.Array, n: int = _MAX_EXAMPLES) -> str:
    try:
        sub = table.filter(mask).slice(0, n)
        msgs = [str(m)[:200] for m in sub.column("message").to_pylist()]
        return " ".join(f"e.g. {m!r}" for m in msgs)
    except Exception:  # noqa: BLE001 — examples are a courtesy
        return ""


def _unparsed_mask(table: pa.Table) -> pa.Array:
    """True where ``attributes.parse_status == "unparsed"``."""
    attrs = table.column("attributes").combine_chunks()
    flags = []
    for entry in attrs.to_pylist():
        status = None
        if entry:
            for k, v in entry:
                if k == "parse_status":
                    status = v
                    break
        flags.append(status == "unparsed")
    return pa.array(flags, type=pa.bool_())


def validate_output(
    parquet_path: Path,
    *,
    raw_sha256: str,
    expected_version: int,
    parse_floor: float = 0.5,
    timestamp_floor: float = 0.5,
) -> ValidationReport:
    """Validate ``parquet_path`` against the contract and the run's own facts."""
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
        got_hash = meta.original_files[0].sha256 if meta.original_files else None
        checks.append(
            Check(
                "original_file_hash",
                got_hash == raw_sha256,
                f"original_files[0].sha256={got_hash!r}, input file sha256={raw_sha256!r}",
            )
        )
        table = pf.read()
    finally:
        pf.close()

    n = table.num_rows
    rep.rows = n
    checks.append(Check("rows", n >= 1, f"{n} rows"))
    if n == 0:
        rep.checks = checks
        return rep

    null_counts = {c: table.column(c).null_count for c in _PROVENANCE}
    bad = {c: k for c, k in null_counts.items() if k}
    checks.append(
        Check("provenance_nulls", not bad, "no nulls" if not bad else f"nulls per column: {bad}")
    )

    unparsed_mask = _unparsed_mask(table)
    unparsed = pc.sum(unparsed_mask).as_py() or 0
    parsed = n - unparsed
    checks.append(
        Check(
            "parse_rate",
            parsed / n >= parse_floor,
            f"{parsed}/{n} rows parsed ({parsed / n:.0%}); floor {parse_floor:.0%}. "
            + _examples(table, unparsed_mask),
        )
    )

    ts = table.column("timestamp")
    with_ts = n - ts.null_count
    ts_null_mask = pc.is_null(ts)
    checks.append(
        Check(
            "timestamps",
            with_ts / n >= timestamp_floor,
            f"{with_ts}/{n} rows have a timestamp ({with_ts / n:.0%}); floor {timestamp_floor:.0%}. "
            + _examples(table, ts_null_mask),
        )
    )
    if with_ts:
        checks.append(
            Check(
                "time_range",
                True,
                f"{pc.min(ts).as_py()} → {pc.max(ts).as_py()}",
                enforced=False,
            )
        )

    offs = table.column("byte_offset").to_pylist()
    files = table.column("source_file").to_pylist()
    last: dict[str, int] = {}
    mono = True
    for f, o in zip(files, offs, strict=True):
        if o is None:
            continue
        if o < last.get(f, -1):
            mono = False
            break
        last[f] = o
    checks.append(
        Check(
            "offsets_monotonic",
            mono,
            "byte_offset non-decreasing per source_file"
            if mono
            else "byte_offset decreases within a source_file — offsets may be wrong",
            enforced=False,
        )
    )

    rep.checks = checks
    rep.ok = all(c.ok for c in checks if c.enforced)
    return rep
