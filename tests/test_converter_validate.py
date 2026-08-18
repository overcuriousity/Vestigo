"""Each validator check on a crafted Parquet file."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq

from vestigo.converters.validate import validate_output
from vestigo.ingestion.parquet_format import (
    META_CONVERTER_NAME,
    META_CONVERTER_VERSION,
    META_FORMAT_VERSION,
    META_ORIGINAL_FILES,
    PARQUET_EVENT_SCHEMA,
)

RAW = "a" * 64


def _write(
    path: Path,
    rows: list[dict],
    *,
    version: str = "1.0.0",
    raw: str = RAW,
    drop_footer: bool = False,
) -> Path:
    cols: dict[str, list] = {f.name: [] for f in PARQUET_EVENT_SCHEMA}
    for r in rows:
        base = {
            "source_file": "x.log",
            "file_hash": raw,
            "byte_offset": 0,
            "content_hash": "c",
            "message": "m",
            "timestamp": datetime(2026, 1, 1, tzinfo=UTC),
            "timestamp_desc": "",
            "artifact": "",
            "artifact_long": "",
            "display_name": "",
            "tags": [],
            "attributes": {},
        }
        base.update(r)
        for k in cols:
            cols[k].append(base[k])
    meta = (
        {}
        if drop_footer
        else {
            META_FORMAT_VERSION: "1",
            META_CONVERTER_NAME: "t2vestigo",
            META_CONVERTER_VERSION: version,
            META_ORIGINAL_FILES: json.dumps([{"name": "x.log", "sha256": raw, "size_bytes": 1}]),
        }
    )
    table = pa.Table.from_pydict(cols, schema=PARQUET_EVENT_SCHEMA).replace_schema_metadata(meta)
    pq.write_table(table, path)
    return path


def _names(report, ok: bool) -> set[str]:
    return {c.name for c in report.checks if c.ok is ok and c.enforced}


def test_good_file_passes(tmp_path):
    p = _write(tmp_path / "o.parquet", [{}, {}])
    r = validate_output(p, raw_sha256=RAW, expected_version=1)
    assert r.ok and r.rows == 2 and r.converter_name == "t2vestigo"


def test_footer_missing(tmp_path):
    p = _write(tmp_path / "o.parquet", [{}], drop_footer=True)
    r = validate_output(p, raw_sha256=RAW, expected_version=1)
    assert not r.ok and "footer" in _names(r, False)


def test_unreadable_file(tmp_path):
    p = tmp_path / "o.parquet"
    p.write_bytes(b"not parquet")
    r = validate_output(p, raw_sha256=RAW, expected_version=1)
    assert not r.ok and "footer" in _names(r, False)


def test_wrong_version_and_hash(tmp_path):
    p = _write(tmp_path / "o.parquet", [{}], version="1.0.0", raw="b" * 64)
    r = validate_output(p, raw_sha256=RAW, expected_version=2)
    assert {"converter_version", "original_file_hash"} <= _names(r, False)


def test_no_rows(tmp_path):
    p = _write(tmp_path / "o.parquet", [])
    assert "rows" in _names(validate_output(p, raw_sha256=RAW, expected_version=1), False)


def test_provenance_nulls(tmp_path):
    p = _write(tmp_path / "o.parquet", [{"content_hash": None}])
    r = validate_output(p, raw_sha256=RAW, expected_version=1)
    assert "provenance_nulls" in _names(r, False)


def test_parse_rate_and_timestamps(tmp_path):
    rows = [{"attributes": {"parse_status": "unparsed"}, "timestamp": None}] * 3 + [{}]
    r = validate_output(_write(tmp_path / "o.parquet", rows), raw_sha256=RAW, expected_version=1)
    assert {"parse_rate", "timestamps"} <= _names(r, False)
    detail = next(c.detail for c in r.checks if c.name == "parse_rate")
    assert "1/4" in detail


def test_reported_checks_do_not_fail(tmp_path):
    rows = [{"byte_offset": 10}, {"byte_offset": 5}]
    r = validate_output(_write(tmp_path / "o.parquet", rows), raw_sha256=RAW, expected_version=1)
    assert r.ok
    mono = next(c for c in r.checks if c.name == "offsets_monotonic")
    assert mono.enforced is False and mono.ok is False
    assert any(c.name == "time_range" for c in r.checks)
    assert isinstance(r.to_dict()["checks"], list)
