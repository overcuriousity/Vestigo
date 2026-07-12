"""Tests for the nginx2vestigo Parquet converter script.

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
    Path(__file__).parent.parent / "src" / "vestigo" / "assets" / "converters" / "nginx2vestigo.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("nginx2vestigo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _convert(converter, input_path: Path, out: Path, workers: int = 1) -> pq.ParquetFile:
    rc = converter.convert(str(input_path), str(out), workers=workers, verbose=False)
    assert rc == 0
    return pq.ParquetFile(out)


class TestSpecParity:
    def test_embedded_spec_matches_server_module(self, converter):
        # The standalone script embeds the interchange spec; it must never
        # drift from ingestion/parquet_format.py.
        assert converter.FORMAT_VERSION == parquet_format.FORMAT_VERSION
        assert converter.META_FORMAT_VERSION == parquet_format.META_FORMAT_VERSION
        assert converter.META_CONVERTER_NAME == parquet_format.META_CONVERTER_NAME
        assert converter.META_CONVERTER_VERSION == parquet_format.META_CONVERTER_VERSION
        assert converter.META_ORIGINAL_FILES == parquet_format.META_ORIGINAL_FILES
        assert converter.PARQUET_EVENT_SCHEMA == parquet_format.PARQUET_EVENT_SCHEMA

    def test_output_validates_against_server_spec(self, converter, tmp_path):
        pf = _convert(converter, DATA / "nginx_access.log", tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "nginx2vestigo"
        assert meta.converter_version == converter.CONVERTER_VERSION

    def test_rejects_non_parquet_output_extension(self, converter, tmp_path):
        with pytest.raises(SystemExit, match=r"\.parquet"):
            converter.convert(str(DATA / "nginx_access.log"), str(tmp_path / "out.csv"), 1, False)


class TestAccessLog:
    def test_golden_lines(self, converter, tmp_path):
        pf = _convert(converter, DATA / "nginx_access.log", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3  # malformed line skipped

        first = rows[0]
        assert first["message"].startswith("192.168.1.10 - alice")
        assert first["artifact"] == "nginx:access"
        assert first["artifact_long"] == "web:access:request"
        assert first["timestamp_desc"] == "HTTP Request Time"
        # 09:46:41 +0200 == 07:46:41 UTC
        assert first["timestamp"].hour == 7
        attrs = dict(first["attributes"])
        assert attrs["src_ip"] == "192.168.1.10"
        assert attrs["remote_user"] == "alice"
        assert attrs["http_method"] == "GET"
        assert attrs["http_uri"] == "/index.html"
        assert attrs["status_code"] == "200"
        assert attrs["user_agent"] == "Mozilla/5.0 (X11; Linux x86_64)"
        # Empty/None attributes are dropped.
        assert "remote_ident" not in attrs

        ipv6 = rows[2]
        assert dict(ipv6["attributes"])["src_ip"] == "2001:db8::42"
        assert dict(ipv6["attributes"])["additional_field"] == "extra"

    def test_byte_offsets_resolve_to_lines(self, converter, tmp_path):
        raw = (DATA / "nginx_access.log").read_bytes()
        pf = _convert(converter, DATA / "nginx_access.log", tmp_path / "out.parquet")
        for row in pf.read().to_pylist():
            offset = row["byte_offset"]
            line = raw[offset:].split(b"\n", 1)[0].decode()
            assert line == row["message"]
            expected_hash = hashlib.sha256(line.encode()).hexdigest()
            assert row["content_hash"] == expected_hash

    def test_file_provenance(self, converter, tmp_path):
        src = DATA / "nginx_access.log"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        expected = hashlib.sha256(src.read_bytes()).hexdigest()
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        originals = json.loads(meta[parquet_format.META_ORIGINAL_FILES])
        assert originals == [
            {"name": "nginx_access.log", "sha256": expected, "size_bytes": src.stat().st_size}
        ]
        for row in pf.read().to_pylist():
            assert row["file_hash"] == expected
            assert row["source_file"] == "nginx_access.log"


class TestErrorLog:
    def test_golden_lines(self, converter, tmp_path):
        pf = _convert(converter, DATA / "nginx_error.log", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 2

        first = rows[0]
        assert first["artifact"] == "nginx:error"
        assert first["artifact_long"] == "web:error:log"
        attrs = dict(first["attributes"])
        assert attrs["error_level"] == "error"
        assert attrs["worker_pid"] == "1234"
        assert attrs["src_ip"] == "192.168.1.10"  # extracted from "client: ..."
        assert first["timestamp"].year == 2026


class TestGzip:
    def test_gz_offsets_are_decompressed_stream_offsets(self, converter, tmp_path):
        src = DATA / "nginx_access_gz.log.gz"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        decompressed = gzip.decompress(src.read_bytes())
        assert len(rows) == 3
        for row in rows:
            line = decompressed[row["byte_offset"] :].split(b"\n", 1)[0].decode()
            assert line == row["message"]
        # file_hash covers the compressed evidence bytes, not the stream.
        expected = hashlib.sha256(src.read_bytes()).hexdigest()
        assert rows[0]["file_hash"] == expected


class TestDirectoryInput:
    def test_directory_of_rotated_logs(self, converter, tmp_path):
        logs = tmp_path / "nginx"
        logs.mkdir()
        (logs / "access.log").write_bytes((DATA / "nginx_access.log").read_bytes())
        (logs / "error.log").write_bytes((DATA / "nginx_error.log").read_bytes())
        pf = _convert(converter, logs, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 5
        by_file = {row["source_file"] for row in rows}
        assert by_file == {"access.log", "error.log"}
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        originals = json.loads(meta[parquet_format.META_ORIGINAL_FILES])
        assert {o["name"] for o in originals} == {"access.log", "error.log"}
        # Per-row file_hash matches the per-file provenance entry.
        hash_by_name = {o["name"]: o["sha256"] for o in originals}
        for row in rows:
            assert row["file_hash"] == hash_by_name[row["source_file"]]


class TestParallel:
    def test_chunk_boundaries_cover_file_without_overlap(self, converter, tmp_path):
        big = tmp_path / "access.log"
        line = '1.2.3.4 - - [25/Jun/2026:09:46:41 +0200] "GET /x HTTP/1.1" 200 10 "-" "ua"\n'
        big.write_text(line * 500)
        chunks = converter.find_chunk_boundaries(big, target_chunks=8)
        assert chunks[0][0] == 0
        assert chunks[-1][1] == big.stat().st_size
        for (_, end_a), (start_b, _) in zip(chunks, chunks[1:], strict=False):
            assert end_a == start_b
        raw = big.read_bytes()
        for start, _end in chunks[1:]:
            assert raw[start - 1 : start] == b"\n"  # newline-aligned

    def test_parallel_equals_sequential(self, converter, tmp_path):
        # Parallel mode spawns worker processes that re-import the script as
        # __main__ — only possible when it runs as a real CLI process, so the
        # parallel run goes through subprocess.
        import os
        import subprocess
        import sys

        big = tmp_path / "access.log"
        lines = [
            f"10.0.{i % 256}.{i % 100} - - [25/Jun/2026:09:{i % 60:02d}:41 +0200] "
            f'"GET /page/{i} HTTP/1.1" 200 {i} "-" "ua-{i}"\n'
            for i in range(2000)
        ]
        big.write_text("".join(lines))

        pf_seq = _convert(converter, big, tmp_path / "seq.parquet", workers=1)
        env = dict(os.environ, NGINX2TS_PARALLEL_MIN_BYTES="0")
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
        pf1 = _convert(converter, DATA / "nginx_access.log", tmp_path / "a.parquet")
        pf2 = _convert(converter, DATA / "nginx_access.log", tmp_path / "b.parquet")
        assert pf1.read().to_pylist() == pf2.read().to_pylist()
