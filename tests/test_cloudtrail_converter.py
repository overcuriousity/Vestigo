"""Tests for the cloudtrail2vestigo Parquet converter script.

The converter is a standalone download (not an importable package module);
tests load it from its asset path via importlib.
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
    / "cloudtrail2vestigo.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("cloudtrail2vestigo", _SCRIPT)
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
        assert converter.PARQUET_EVENT_SCHEMA == parquet_format.PARQUET_EVENT_SCHEMA

    def test_output_validates_against_server_spec(self, converter, tmp_path):
        pf = _convert(converter, DATA / "cloudtrail.json", tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "cloudtrail2vestigo"
        assert meta.converter_version == converter.CONVERTER_VERSION

    def test_rejects_non_parquet_output_extension(self, converter, tmp_path):
        with pytest.raises(SystemExit, match=r"\.parquet"):
            converter.convert(str(DATA / "cloudtrail.json"), str(tmp_path / "out.csv"), 1, False)


class TestParsing:
    def test_golden_records(self, converter, tmp_path):
        pf = _convert(converter, DATA / "cloudtrail.json", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3

        get_object = rows[0]
        assert get_object["artifact"] == "cloudtrail:data:event"
        assert get_object["artifact_long"] == "aws:cloudtrail:event"
        assert get_object["timestamp_desc"] == "CloudTrail Event Time"
        assert get_object["timestamp"].hour == 9
        attrs = dict(get_object["attributes"])
        assert attrs["eventName"] == "GetObject"
        assert attrs["eventSource"] == "s3.amazonaws.com"
        assert attrs["src_ip"] == "192.168.1.10"
        assert attrs["userIdentity.userName"] == "alice"
        assert attrs["requestParameters.bucketName"] == "example-bucket"
        assert "resources" in attrs
        assert "alice" in get_object["message"]

        delete_user = rows[1]
        assert delete_user["artifact"] == "cloudtrail:management:event"
        attrs = dict(delete_user["attributes"])
        assert attrs["errorCode"] == "AccessDenied"
        assert attrs["userIdentity.sessionContext.sessionIssuer.userName"] == "AdminRole"
        assert "AdminRole" in delete_user["message"]
        assert "[AccessDenied]" in delete_user["message"]

        config_event = rows[2]
        attrs = dict(config_event["attributes"])
        # sourceIPAddress is a service DNS name here, not a real IP.
        assert attrs["sourceIPAddress"] == "config.amazonaws.com"
        assert "src_ip" not in attrs
        assert attrs["userIdentity.invokedBy"] == "config.amazonaws.com"

    def test_byte_offsets_resolve_to_records(self, converter, tmp_path):
        raw = (DATA / "cloudtrail.json").read_text(encoding="utf-8")
        pf = _convert(converter, DATA / "cloudtrail.json", tmp_path / "out.parquet")
        for row in pf.read().to_pylist():
            offset = row["byte_offset"]
            raw_bytes = raw.encode("utf-8")
            # The record's raw bytes must start exactly at byte_offset and
            # its sha256 must match content_hash.
            span_start = raw_bytes[offset:]
            assert span_start.lstrip()[:1] == b"{"
            # Recover the record by matching against a brace-balanced scan
            # from the offset, then compare against content_hash.
            depth = 0
            end = None
            in_string = False
            escape = False
            for i, byte in enumerate(span_start):
                ch = chr(byte)
                if in_string:
                    if escape:
                        escape = False
                    elif ch == "\\":
                        escape = True
                    elif ch == '"':
                        in_string = False
                    continue
                if ch == '"':
                    in_string = True
                elif ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            assert end is not None
            record_bytes = span_start[:end]
            assert hashlib.sha256(record_bytes).hexdigest() == row["content_hash"]

    def test_file_provenance(self, converter, tmp_path):
        src = DATA / "cloudtrail.json"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        expected = hashlib.sha256(src.read_bytes()).hexdigest()
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        originals = json.loads(meta[parquet_format.META_ORIGINAL_FILES])
        assert originals == [
            {"name": "cloudtrail.json", "sha256": expected, "size_bytes": src.stat().st_size}
        ]
        for row in pf.read().to_pylist():
            assert row["file_hash"] == expected
            assert row["source_file"] == "cloudtrail.json"


class TestGzip:
    def test_gz_records_parse(self, converter, tmp_path):
        pf = _convert(converter, DATA / "cloudtrail_gz.json.gz", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3


class TestDigestExclusion:
    def test_digest_file_excluded_from_directory_scan(self, converter, tmp_path):
        logs = tmp_path / "cloudtrail"
        logs.mkdir()
        (logs / "cloudtrail.json").write_bytes((DATA / "cloudtrail.json").read_bytes())
        (logs / "123_CloudTrail-Digest_20260708.json").write_bytes(
            (DATA / "cloudtrail_CloudTrail-Digest.json").read_bytes()
        )
        pf = _convert(converter, logs, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3
        assert all(row["source_file"] == "cloudtrail.json" for row in rows)


class TestDirectoryInput:
    def test_directory_of_files(self, converter, tmp_path):
        logs = tmp_path / "cloudtrail"
        logs.mkdir()
        (logs / "a.json").write_bytes((DATA / "cloudtrail.json").read_bytes())
        (logs / "b.json").write_bytes((DATA / "cloudtrail.json").read_bytes())
        pf = _convert(converter, logs, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 6
        by_file = {row["source_file"] for row in rows}
        assert by_file == {"a.json", "b.json"}


class TestParallel:
    def test_cross_file_parallel_equals_sequential(self, converter, tmp_path):
        # Parallel mode spawns worker processes that re-import the script as
        # __main__ — only possible when it runs as a real CLI process, so the
        # parallel run goes through subprocess (same pattern as the nginx
        # converter's TestParallel).
        import subprocess
        import sys

        logs = tmp_path / "cloudtrail"
        logs.mkdir()
        for i in range(6):
            (logs / f"file_{i}.json").write_bytes((DATA / "cloudtrail.json").read_bytes())

        pf_seq = _convert(converter, logs, tmp_path / "seq.parquet", workers=1)
        proc = subprocess.run(
            [
                sys.executable,
                str(_SCRIPT),
                "-i",
                str(logs),
                "-o",
                str(tmp_path / "par.parquet"),
                "-w",
                "4",
            ],
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        pf_par = pq.ParquetFile(tmp_path / "par.parquet")

        def row_set(pf):
            return {
                (r["source_file"], r["byte_offset"], r["content_hash"])
                for r in pf.read().to_pylist()
            }

        seq_rows = row_set(pf_seq)
        assert len(seq_rows) == 18
        assert row_set(pf_par) == seq_rows


class TestDeterminism:
    def test_two_runs_identical_rows(self, converter, tmp_path):
        pf1 = _convert(converter, DATA / "cloudtrail.json", tmp_path / "a.parquet")
        pf2 = _convert(converter, DATA / "cloudtrail.json", tmp_path / "b.parquet")
        assert pf1.read().to_pylist() == pf2.read().to_pylist()


class TestSplit:
    def test_parts_mode_smoke(self, converter, tmp_path):
        out = tmp_path / "out.parquet"
        rc = converter.convert(str(DATA / "cloudtrail.json"), str(out), 1, False, split="2")
        assert rc == 0
        assert not out.exists()
        parts = sorted(tmp_path.glob("out.part*.parquet"))
        assert len(parts) == 2
        rows = [r for p in parts for r in pq.ParquetFile(p).read().to_pylist()]
        ref = _convert(converter, DATA / "cloudtrail.json", tmp_path / "ref.parquet")
        assert rows == ref.read().to_pylist()
        for p in parts:
            pf = pq.ParquetFile(p)
            meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            assert meta.converter_name == "cloudtrail2vestigo"
