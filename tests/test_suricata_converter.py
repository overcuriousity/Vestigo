"""Tests for the suricata2vestigo Parquet converter script.

The converter is a standalone download (not an importable package module);
tests load it from its asset path via importlib.
"""

from __future__ import annotations

import gzip
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
    / "suricata2vestigo.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("suricata2vestigo", _SCRIPT)
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
        pf = _convert(converter, DATA / "suricata.log", tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "suricata2vestigo"
        assert meta.converter_version == converter.CONVERTER_VERSION

    def test_rejects_non_parquet_output_extension(self, converter, tmp_path):
        with pytest.raises(SystemExit, match=r"\.parquet"):
            converter.convert(str(DATA / "suricata.log"), str(tmp_path / "out.csv"), 1, False)


class TestParsing:
    def test_golden_lines(self, converter, tmp_path):
        pf = _convert(converter, DATA / "suricata.log", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 5  # the trailing garbage line is skipped

        eve_alert = rows[0]
        assert eve_alert["artifact"] == "ids:alert:suricata"
        assert eve_alert["artifact_long"] == "ids:suricata:event"
        assert eve_alert["timestamp"].hour == 9
        attrs = dict(eve_alert["attributes"])
        assert attrs["src_ip"] == "192.168.1.10"
        assert attrs["dst_ip"] == "8.8.8.8"
        assert attrs["alert_signature"] == "ET POLICY Test Signature"
        assert attrs["alert_severity"] == "2"

        eve_http = rows[1]
        assert eve_http["artifact"] == "ids:event:suricata"
        attrs = dict(eve_http["attributes"])
        assert attrs["event_type"] == "http"
        assert attrs["url"] == "/index.html"
        assert attrs["user_agent"] == "curl/8.0"
        assert "http.hostname" in attrs

        fastlog = rows[2]
        assert fastlog["artifact"] == "ids:alert:suricata"
        attrs = dict(fastlog["attributes"])
        assert attrs["src_ip"] == "192.168.10.14"
        assert attrs["src_port"] == "48820"
        assert attrs["alert_signature"] == "ET POLICY Test Alert"

        opnsense_alert = rows[3]
        attrs = dict(opnsense_alert["attributes"])
        assert attrs["drop_marker"] == "wDrop"
        assert attrs["alert_action"] == "drop"
        assert attrs["src_ip"] == "10.0.0.5"

        notice = rows[4]
        assert notice["artifact"] == "ids:notice:suricata"
        assert notice["message"] == "rule reload complete"

    def test_byte_offsets_resolve_to_lines(self, converter, tmp_path):
        raw = (DATA / "suricata.log").read_bytes()
        pf = _convert(converter, DATA / "suricata.log", tmp_path / "out.parquet")
        for row in pf.read().to_pylist():
            offset = row["byte_offset"]
            line = raw[offset:].split(b"\n", 1)[0].decode()
            expected_hash = hashlib.sha256(line.encode()).hexdigest()
            assert row["content_hash"] == expected_hash

    def test_file_provenance(self, converter, tmp_path):
        src = DATA / "suricata.log"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        expected = hashlib.sha256(src.read_bytes()).hexdigest()
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        originals = json.loads(meta[parquet_format.META_ORIGINAL_FILES])
        assert originals == [
            {"name": "suricata.log", "sha256": expected, "size_bytes": src.stat().st_size}
        ]
        for row in pf.read().to_pylist():
            assert row["file_hash"] == expected
            assert row["source_file"] == "suricata.log"


class TestGzip:
    def test_gz_offsets_are_decompressed_stream_offsets(self, converter, tmp_path):
        src = DATA / "suricata_gz.log.gz"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        decompressed = gzip.decompress(src.read_bytes())
        assert len(rows) == 5
        for row in rows:
            line = decompressed[row["byte_offset"] :].split(b"\n", 1)[0].decode()
            expected_hash = hashlib.sha256(line.encode()).hexdigest()
            assert row["content_hash"] == expected_hash


class TestDirectoryInput:
    def test_directory_of_logs(self, converter, tmp_path):
        logs = tmp_path / "suricata"
        logs.mkdir()
        (logs / "eve.json").write_bytes((DATA / "suricata.log").read_bytes())
        (logs / "eve_2.json").write_bytes((DATA / "suricata.log").read_bytes())
        pf = _convert(converter, logs, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 10
        by_file = {row["source_file"] for row in rows}
        assert by_file == {"eve.json", "eve_2.json"}


class TestParallel:
    def test_parallel_equals_sequential(self, converter, tmp_path):
        import os
        import subprocess
        import sys

        big = tmp_path / "eve.json"
        lines = [
            f'{{"timestamp":"2026-07-08T09:46:{i % 60:02d}.000000+00:00","event_type":"alert",'
            f'"src_ip":"10.0.{i % 256}.{i % 100}","src_port":{20000 + i},"dest_ip":"1.1.1.1",'
            f'"dest_port":443,"proto":"TCP","alert":{{"action":"allowed","gid":1,'
            f'"signature_id":{i},"rev":1,"signature":"Test {i}","category":"Test",'
            f'"severity":1}}}}\n'
            for i in range(2000)
        ]
        big.write_text("".join(lines))

        pf_seq = _convert(converter, big, tmp_path / "seq.parquet", workers=1)
        env = dict(os.environ, SURICATA2TS_PARALLEL_MIN_BYTES="0")
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
        pf1 = _convert(converter, DATA / "suricata.log", tmp_path / "a.parquet")
        pf2 = _convert(converter, DATA / "suricata.log", tmp_path / "b.parquet")
        assert pf1.read().to_pylist() == pf2.read().to_pylist()


class TestSplit:
    def test_parts_mode_smoke(self, converter, tmp_path):
        out = tmp_path / "out.parquet"
        rc = converter.convert(str(DATA / "suricata.log"), str(out), 1, False, split="2")
        assert rc == 0
        assert not out.exists()
        parts = sorted(tmp_path.glob("out.part*.parquet"))
        assert len(parts) == 2
        rows = [r for p in parts for r in pq.ParquetFile(p).read().to_pylist()]
        ref = _convert(converter, DATA / "suricata.log", tmp_path / "ref.parquet")
        assert rows == ref.read().to_pylist()
        for p in parts:
            pf = pq.ParquetFile(p)
            meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            assert meta.converter_name == "suricata2vestigo"
