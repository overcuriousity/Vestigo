"""Tests for the timesketch2parquet Parquet converter script.

The converter is a standalone download (not an importable package module);
tests load it from its asset path via importlib. Column requirements follow
upstream google/timesketch's own CSV/JSONL import spec, not Vestigo's
server-side generic-CSV parser conventions (see the module docstring).
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from vestigo.ingestion import parquet_format

_SCRIPT = (
    Path(__file__).parent.parent
    / "src"
    / "vestigo"
    / "assets"
    / "converters"
    / "timesketch2parquet.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("timesketch2parquet", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _convert(converter, input_path: Path, out: Path, workers: int = 1) -> pq.ParquetFile:
    rc = converter.convert(str(input_path), str(out), workers=workers, verbose=False)
    assert rc == 0
    return pq.ParquetFile(out)


class TestSpecParity:
    def test_embedded_spec_matches_server_module(self, converter):
        assert converter.FORMAT_VERSION == parquet_format.FORMAT_VERSION
        assert converter.META_FORMAT_VERSION == parquet_format.META_FORMAT_VERSION
        assert converter.META_CONVERTER_NAME == parquet_format.META_CONVERTER_NAME
        assert converter.META_CONVERTER_VERSION == parquet_format.META_CONVERTER_VERSION
        assert converter.META_ORIGINAL_FILES == parquet_format.META_ORIGINAL_FILES
        assert converter.META_CONVERTED_AT == parquet_format.META_CONVERTED_AT
        assert converter.META_ROW_COUNTS == parquet_format.META_ROW_COUNTS
        assert converter.META_TIMEZONE_ASSUMPTION == parquet_format.META_TIMEZONE_ASSUMPTION
        assert converter.META_PARSE_DECISIONS == parquet_format.META_PARSE_DECISIONS
        assert converter.PARQUET_EVENT_SCHEMA == parquet_format.PARQUET_EVENT_SCHEMA

    def test_output_validates_against_server_spec(self, converter, tmp_path):
        pf = _convert(converter, DATA / "timesketch_generic.csv", tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "timesketch2parquet"
        assert meta.converter_version == converter.CONVERTER_VERSION

    def test_rejects_non_parquet_output_extension(self, converter, tmp_path):
        with pytest.raises(SystemExit, match=r"\.parquet"):
            converter.convert(
                str(DATA / "timesketch_generic.csv"), str(tmp_path / "out.csv"), 1, False
            )


class TestCsv:
    def test_golden_rows(self, converter, tmp_path):
        pf = _convert(converter, DATA / "timesketch_generic.csv", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3

        first = rows[0]
        assert first["message"] == "A message"
        assert first["timestamp_desc"] == "Write time"
        assert first["artifact"] == "generic:timesketch:event"
        assert first["artifact_long"] == "timesketch:generic:event"
        assert first["timestamp"].year == 2015
        assert first["tags"] == ["a", "b"]
        attrs = dict(first["attributes"])
        assert attrs["extra_field_1"] == "foo"
        assert attrs["extra_field_2"] == "bar"
        # Recognized columns never also appear in attributes.
        assert "message" not in attrs
        assert "timestamp_desc" not in attrs
        assert "datetime" not in attrs
        assert "timestamp" not in attrs
        assert "tag" not in attrs

        multiline = rows[1]
        assert multiline["message"] == "Multi\nline message"
        assert multiline["tags"] == ["x", "y"]

        no_tag = rows[2]
        assert no_tag["tags"] == []
        assert "extra_field_2" not in dict(no_tag["attributes"])  # empty value dropped

    def test_datetime_substituted_by_numeric_timestamp(self, converter, tmp_path):
        # Upstream allows CSV to omit `datetime` when a numeric `timestamp`
        # column is present; apply the exact magnitude heuristic.
        csv_text = (
            "message,timestamp,timestamp_desc\n"
            "seconds,1700000000,Write time\n"
            "millis,1700000000000,Write time\n"
            "micros,1700000000000000,Write time\n"
            "nanos,1700000000000000000,Write time\n"
        )
        src = tmp_path / "no_datetime.csv"
        src.write_text(csv_text)
        pf = _convert(converter, src, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 4
        years = {r["message"]: r["timestamp"].year for r in rows}
        assert years == {"seconds": 2023, "millis": 2023, "micros": 2023, "nanos": 2023}

    def test_byte_offsets_resolve_to_records(self, converter, tmp_path):
        import csv
        import io

        src = DATA / "timesketch_generic.csv"
        raw = src.read_bytes()
        pf = _convert(converter, src, tmp_path / "out.parquet")

        header_line = raw.split(b"\n", 1)[0].decode()
        headers = next(csv.reader([header_line]))

        rows = pf.read().to_pylist()
        for row in rows:
            offset = row["byte_offset"]
            # Re-parse a single logical record starting exactly at byte_offset
            # using a fresh csv.reader — independent of the converter's own
            # byte-tracking implementation. This correctly spans multiple
            # physical lines for the quoted multi-line message row too.
            tail = io.StringIO(raw[offset:].decode())
            record = next(csv.DictReader(tail, fieldnames=headers))
            assert record["message"] == row["message"]

        # Offsets are unique, non-overlapping, and every content_hash is a
        # well-formed sha256 hex digest.
        offsets = [r["byte_offset"] for r in rows]
        assert offsets == sorted(set(offsets))
        assert all(len(r["content_hash"]) == 64 for r in rows)

    def test_file_provenance(self, converter, tmp_path):
        src = DATA / "timesketch_generic.csv"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        expected = hashlib.sha256(src.read_bytes()).hexdigest()
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        originals = json.loads(meta[parquet_format.META_ORIGINAL_FILES])
        assert len(originals) == 1
        entry = originals[0]
        assert entry["name"] == "timesketch_generic.csv"
        assert entry["sha256"] == expected
        assert entry["size_bytes"] == src.stat().st_size
        assert entry["path"] == str(src.resolve())
        assert entry["mtime"]  # ISO-8601 file mtime present (converter >= 1.3.0)
        for row in pf.read().to_pylist():
            assert row["file_hash"] == expected
            assert row["source_file"] == "timesketch_generic.csv"

    def test_missing_mandatory_columns_rejected(self, converter, tmp_path):
        src = tmp_path / "bad.csv"
        src.write_text("foo,bar\n1,2\n")
        with pytest.raises(SystemExit, match="mandatory column"):
            converter.convert(str(src), str(tmp_path / "out.parquet"), 1, False)


class TestJsonl:
    def test_golden_rows(self, converter, tmp_path):
        pf = _convert(converter, DATA / "timesketch_generic.jsonl", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3

        comma_tag = rows[0]
        assert comma_tag["tags"] == ["a", "b"]
        attrs = dict(comma_tag["attributes"])
        assert attrs["extra_field_1"] == "foo"

        list_tag = rows[1]
        assert list_tag["tags"] == ["x", "y"]

        bare_tag = rows[2]
        assert bare_tag["tags"] == ["solo"]

    def test_missing_mandatory_columns_rejected(self, converter, tmp_path):
        src = tmp_path / "bad.jsonl"
        src.write_text('{"foo": "bar"}\n')
        with pytest.raises(SystemExit, match="mandatory column"):
            converter.convert(str(src), str(tmp_path / "out.parquet"), 1, False)


class TestGzip:
    def test_csv_gz(self, converter, tmp_path):
        pf = _convert(converter, DATA / "timesketch_generic.csv.gz", tmp_path / "out.parquet")
        assert len(pf.read().to_pylist()) == 3

    def test_jsonl_gz(self, converter, tmp_path):
        pf = _convert(converter, DATA / "timesketch_generic.jsonl.gz", tmp_path / "out.parquet")
        assert len(pf.read().to_pylist()) == 3


class TestDirectoryInput:
    def test_mixed_csv_and_jsonl(self, converter, tmp_path):
        d = tmp_path / "timelines"
        d.mkdir()
        (d / "a.csv").write_bytes((DATA / "timesketch_generic.csv").read_bytes())
        (d / "b.jsonl").write_bytes((DATA / "timesketch_generic.jsonl").read_bytes())
        pf = _convert(converter, d, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 6
        by_file = {row["source_file"] for row in rows}
        assert by_file == {"a.csv", "b.jsonl"}


class TestParallel:
    def test_jsonl_parallel_equals_sequential(self, converter, tmp_path):
        # Parallel mode spawns worker processes that re-import the script as
        # __main__ — only possible when it runs as a real CLI process, so the
        # parallel run goes through subprocess.
        import os
        import subprocess
        import sys

        big = tmp_path / "big.jsonl"
        lines = [
            f'{{"message": "msg {i}", "timestamp": {1700000000 + i}, '
            f'"timestamp_desc": "Write time", "extra_field_1": "v{i}"}}\n'
            for i in range(2000)
        ]
        big.write_text("".join(lines))

        pf_seq = _convert(converter, big, tmp_path / "seq.parquet", workers=1)
        env = dict(os.environ, TIMESKETCH2PARQUET_PARALLEL_MIN_BYTES="0")
        proc = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "-i",
                str(big),
                "-o",
                str(tmp_path / "par.parquet"),
                "-w",
                "2",
            ],
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        pf_par = pq.ParquetFile(tmp_path / "par.parquet")

        def row_set(pf):
            return {
                (r["byte_offset"], r["content_hash"], r["message"]) for r in pf.read().to_pylist()
            }

        seq_rows = row_set(pf_seq)
        assert len(seq_rows) == 2000
        assert row_set(pf_par) == seq_rows


class TestDeterminism:
    def test_two_runs_identical_rows(self, converter, tmp_path):
        pf1 = _convert(converter, DATA / "timesketch_generic.csv", tmp_path / "a.parquet")
        pf2 = _convert(converter, DATA / "timesketch_generic.csv", tmp_path / "b.parquet")
        assert pf1.read().to_pylist() == pf2.read().to_pylist()


class TestSplit:
    def test_parts_mode_smoke(self, converter, tmp_path):
        out = tmp_path / "out.parquet"
        rc = converter.convert(str(DATA / "timesketch_generic.csv"), str(out), 1, False, split="2")
        assert rc == 0
        assert not out.exists()
        parts = sorted(tmp_path.glob("out.part*.parquet"))
        assert len(parts) == 2
        rows = [r for p in parts for r in pq.ParquetFile(p).read().to_pylist()]
        ref = _convert(converter, DATA / "timesketch_generic.csv", tmp_path / "ref.parquet")
        assert rows == ref.read().to_pylist()
        for p in parts:
            pf = pq.ParquetFile(p)
            meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            assert meta.converter_name == "timesketch2parquet"


@pytest.mark.parametrize("data", ["timesketch_generic.csv", "timesketch_generic.jsonl"])
def test_time_window_filter(converter, tmp_path, data):
    """--since/--until drop out-of-window rows on both the CSV and JSONL hooks."""
    src = DATA / data

    dropped = tmp_path / "dropped.parquet"
    converter.convert(str(src), str(dropped), 1, False, since="2099-01-01T00:00:00Z")
    pf = pq.ParquetFile(dropped)
    footer = {
        k.decode(): v.decode() for k, v in pf.metadata.metadata.items() if k != b"ARROW:schema"
    }
    counts = json.loads(footer["vestigo.row_counts"])
    assert counts["skipped_by_time"] > 0
    assert counts["parsed"] == pf.metadata.num_rows
    assert footer["vestigo.converted_at"]

    ref = _convert(converter, src, tmp_path / "all.parquet")
    wide = tmp_path / "wide.parquet"
    converter.convert(
        str(src),
        str(wide),
        1,
        False,
        since="1970-01-01T00:00:00Z",
        until="2099-01-01T00:00:00Z",
    )
    assert pq.ParquetFile(wide).metadata.num_rows == ref.metadata.num_rows
