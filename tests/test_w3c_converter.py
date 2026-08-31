"""Tests for the w3c2vestigo Parquet converter script.

The converter is a standalone download (not an importable package module);
tests load it from its asset path via importlib.
"""

from __future__ import annotations

import gzip
import hashlib
import importlib.util
import json
import re
from pathlib import Path

import pyarrow.parquet as pq
import pytest

from vestigo.ingestion import parquet_format

_SCRIPT = (
    Path(__file__).parent.parent / "src" / "vestigo" / "assets" / "converters" / "w3c2vestigo.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("w3c2vestigo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _convert(converter, input_path: Path, out: Path, workers: int = 1, **kwargs) -> pq.ParquetFile:
    rc = converter.convert(str(input_path), str(out), workers=workers, verbose=False, **kwargs)
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
        assert converter.META_CONVERTED_AT == parquet_format.META_CONVERTED_AT
        assert converter.META_ROW_COUNTS == parquet_format.META_ROW_COUNTS
        assert converter.META_TIMEZONE_ASSUMPTION == parquet_format.META_TIMEZONE_ASSUMPTION
        assert converter.META_PARSE_DECISIONS == parquet_format.META_PARSE_DECISIONS
        assert converter.PARQUET_EVENT_SCHEMA == parquet_format.PARQUET_EVENT_SCHEMA

    def test_output_validates_against_server_spec(self, converter, tmp_path):
        pf = _convert(converter, DATA / "w3c_iis.log", tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "w3c2vestigo"
        assert meta.converter_version == converter.CONVERTER_VERSION

    def test_rejects_non_parquet_output_extension(self, converter, tmp_path):
        with pytest.raises(SystemExit, match=r"\.parquet"):
            converter.convert(str(DATA / "w3c_iis.log"), str(tmp_path / "out.csv"), 1, False)


class TestIISLog:
    def test_golden_lines(self, converter, tmp_path):
        pf = _convert(converter, DATA / "w3c_iis.log", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 3  # the non-record line is skipped

        first = rows[0]
        assert first["artifact"] == "iis:access"
        assert first["artifact_long"] == "iis:access:entry"
        assert first["timestamp_desc"] == "HTTP Request Time"
        attrs = dict(first["attributes"])
        assert attrs["log_type"] == "iis"
        assert attrs["software"] == "Microsoft Internet Information Services 10.0"
        assert attrs["src_ip"] == "198.51.100.7"
        assert attrs["dst_ip"] == "192.0.2.10"
        assert attrs["http_method"] == "GET"
        assert attrs["http_uri"] == "/owa/auth/logon.aspx"
        assert attrs["http_query"] == "url=https://mail.example.test/owa/"
        assert attrs["status_code"] == "200"
        assert attrs["time_taken_ms"] == "42"
        # IIS writes '+' for a space in the User-Agent; that substitution is
        # reversed, and only there.
        assert attrs["user_agent"] == "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"
        # '-' is the format's "not recorded" placeholder, not a value.
        assert "username" not in attrs

    def test_ipv6_scope_zone_is_split_out(self, converter, tmp_path):
        # "fe80::1%12" is not a valid address to ipaddress; keeping the zone
        # in its own column is what stops the whole row being dropped.
        pf = _convert(converter, DATA / "w3c_iis.log", tmp_path / "out.parquet")
        attrs = dict(pf.read().to_pylist()[1]["attributes"])
        assert attrs["src_ip"] == "fe80::1c2d:3e4f:5a6b:7c8d"
        assert attrs["src_ip_zone"] == "12"
        assert attrs["dst_ip_zone"] == "12"
        assert attrs["username"] == "EXAMPLE\\SRV01$"

    def test_field_set_change_mid_file(self, converter, tmp_path):
        # IIS rewrites '#Fields:' on a restart or config change. The second
        # block has a different, shorter column set; parsing it against the
        # first would shift every value.
        pf = _convert(converter, DATA / "w3c_iis.log", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        attrs = dict(rows[2]["attributes"])
        assert attrs["src_ip"] == "203.0.113.44"
        assert attrs["http_method"] == "POST"
        assert attrs["http_uri"] == "/ecp/DDI/DDIService.svc/SetObject"
        assert attrs["status_code"] == "500"
        assert attrs["response_size"] == "8192"
        assert "time_taken_ms" not in attrs
        meta = {
            k.decode(): v.decode()
            for k, v in pq.ParquetFile(tmp_path / "out.parquet").metadata.metadata.items()
            if k != b"ARROW:schema"
        }
        assert json.loads(meta[parquet_format.META_PARSE_DECISIONS])["field_directives"] == 2

    def test_byte_offsets_resolve_to_lines(self, converter, tmp_path):
        raw = (DATA / "w3c_iis.log").read_bytes()
        pf = _convert(converter, DATA / "w3c_iis.log", tmp_path / "out.parquet")
        for row in pf.read().to_pylist():
            line = raw[row["byte_offset"] :].split(b"\n", 1)[0].decode()
            assert line == row["message"]
            assert row["content_hash"] == hashlib.sha256(line.encode()).hexdigest()

    def test_file_provenance(self, converter, tmp_path):
        src = DATA / "w3c_iis.log"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        expected = hashlib.sha256(src.read_bytes()).hexdigest()
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        originals = json.loads(meta[parquet_format.META_ORIGINAL_FILES])
        assert len(originals) == 1
        assert originals[0]["name"] == "w3c_iis.log"
        assert originals[0]["sha256"] == expected
        assert originals[0]["size_bytes"] == src.stat().st_size
        for row in pf.read().to_pylist():
            assert row["file_hash"] == expected
            assert row["source_file"] == "w3c_iis.log"

    def test_row_counts_report_the_skipped_line(self, converter, tmp_path):
        pf = _convert(converter, DATA / "w3c_iis.log", tmp_path / "out.parquet")
        meta = {
            k.decode(): v.decode() for k, v in pf.metadata.metadata.items() if k != b"ARROW:schema"
        }
        counts = json.loads(meta[parquet_format.META_ROW_COUNTS])
        assert counts == {"parsed": 3, "skipped_malformed": 1, "skipped_by_time": 0}


class TestHTTPERRLog:
    def test_golden_lines(self, converter, tmp_path):
        pf = _convert(converter, DATA / "w3c_httperr.log", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 2

        first = rows[0]
        assert first["artifact"] == "httperr:error"
        assert first["timestamp_desc"] == "HTTP Error Time"
        attrs = dict(first["attributes"])
        assert attrs["log_type"] == "httperr"
        assert attrs["src_ip"] == "198.51.100.7"
        assert attrs["src_port"] == "32650"
        assert attrs["dst_ip"] == "192.0.2.10"
        assert attrs["dst_port"] == "444"
        assert attrs["reason"] == "BadRequest"
        assert attrs["queue_name"] == "MSExchangeRpcProxyAppPool"
        assert attrs["site_id"] == "2"

        # A connection torn down before any request: almost every column is
        # the "-" placeholder, but the addresses and the reason are real.
        idle = dict(rows[1]["attributes"])
        assert idle["reason"] == "Timer_ConnectionIdle"
        assert idle["src_ip"] == "203.0.113.44"
        assert "http_method" not in idle
        assert "status_code" not in idle


class TestGenericW3C:
    def test_unmodelled_dialect_keeps_its_own_columns(self, converter, tmp_path):
        pf = _convert(converter, DATA / "w3c_generic.log", tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 2
        assert rows[0]["artifact"] == "w3c:extended"
        attrs = dict(rows[0]["attributes"])
        assert attrs["x_custom_field"] == "alpha"
        assert attrs["http_method"] == "GET"
        assert attrs["status_code"] == "204"

    def test_time_only_rows_take_the_day_from_the_date_directive(self, converter, tmp_path):
        pf = _convert(converter, DATA / "w3c_generic.log", tmp_path / "out.parquet")
        first = pf.read().to_pylist()[0]["timestamp"]
        assert (first.year, first.month, first.day, first.hour) == (2026, 3, 4, 8)


class TestTimezone:
    def test_assume_tz_shifts_naive_fields(self, converter, tmp_path):
        pf = _convert(
            converter, DATA / "w3c_httperr.log", tmp_path / "out.parquet", assume_tz="+02:00"
        )
        # 11:11:32 local at +02:00 == 09:11:32 UTC
        assert pf.read().to_pylist()[0]["timestamp"].hour == 9
        meta = {k.decode(): v.decode() for k, v in pf.schema_arrow.metadata.items()}
        assert "+02:00" in meta[parquet_format.META_TIMEZONE_ASSUMPTION]

    def test_default_is_utc(self, converter, tmp_path):
        pf = _convert(converter, DATA / "w3c_httperr.log", tmp_path / "out.parquet")
        assert pf.read().to_pylist()[0]["timestamp"].hour == 11


class TestSinceUntil:
    def test_range_filters_rows_and_is_recorded(self, converter, tmp_path):
        pf = _convert(
            converter,
            DATA / "w3c_iis.log",
            tmp_path / "out.parquet",
            since="2026-01-01T01:00:00Z",
        )
        rows = pf.read().to_pylist()
        assert len(rows) == 1
        assert dict(rows[0]["attributes"])["src_ip"] == "203.0.113.44"
        meta = {
            k.decode(): v.decode() for k, v in pf.metadata.metadata.items() if k != b"ARROW:schema"
        }
        assert json.loads(meta[parquet_format.META_ROW_COUNTS])["skipped_by_time"] == 2


class TestGzip:
    def test_gz_offsets_are_decompressed_stream_offsets(self, converter, tmp_path):
        src = DATA / "w3c_iis_gz.log.gz"
        pf = _convert(converter, src, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        decompressed = gzip.decompress(src.read_bytes())
        assert len(rows) == 3
        for row in rows:
            line = decompressed[row["byte_offset"] :].split(b"\n", 1)[0].decode()
            assert line == row["message"]
        # file_hash covers the compressed evidence bytes, not the stream.
        assert rows[0]["file_hash"] == hashlib.sha256(src.read_bytes()).hexdigest()


class TestDirectoryInput:
    def test_directory_is_searched_recursively(self, converter, tmp_path):
        # IIS keeps one directory per site under a single log root, with
        # HTTPERR beside them; the root is what an analyst points at.
        root = tmp_path / "LogFiles"
        (root / "W3SVC2").mkdir(parents=True)
        (root / "HTTPERR").mkdir()
        (root / "W3SVC2" / "u_ex260101.log").write_bytes((DATA / "w3c_iis.log").read_bytes())
        (root / "HTTPERR" / "httperr1.log").write_bytes((DATA / "w3c_httperr.log").read_bytes())
        pf = _convert(converter, root, tmp_path / "out.parquet")
        rows = pf.read().to_pylist()
        assert len(rows) == 5
        assert {row["source_file"] for row in rows} == {"u_ex260101.log", "httperr1.log"}
        assert {row["artifact"] for row in rows} == {"iis:access", "httperr:error"}

    def test_flavor_comes_from_the_filename_before_the_content(self, converter, tmp_path):
        # A file named httperr*.log is classified as HTTPERR even though its
        # own #Software directive would sniff to IIS.
        root = tmp_path / "logs"
        root.mkdir()
        (root / "httperr9.log").write_bytes((DATA / "w3c_iis.log").read_bytes())
        pf = _convert(converter, root, tmp_path / "out.parquet")
        assert {row["artifact"] for row in pf.read().to_pylist()} == {"httperr:error"}

    def test_rejects_a_directory_with_no_w3c_logs(self, converter, tmp_path):
        empty = tmp_path / "empty"
        empty.mkdir()
        (empty / "notes.log").write_text("just some text\n")
        with pytest.raises(SystemExit, match="no W3C Extended log files"):
            converter.convert(str(empty), str(tmp_path / "out.parquet"), 1, False)


class TestParallelParity:
    """A chunk worker starts mid-file and must be handed the directive state.

    Without the pre-scan a worker whose chunk begins after a '#Fields:'
    change would parse those rows against no column set at all (dropping
    them) or, worse, against a stale one. This is the property that keeps
    parallel output identical to serial output.
    """

    def _big_log(self, path: Path) -> None:
        lines = [
            "#Software: Microsoft Internet Information Services 10.0",
            "#Date: 2026-01-01 00:00:00",
            "#Fields: date time c-ip cs-method cs-uri-stem sc-status",
        ]
        lines += [
            f"2026-01-01 00:{i // 60 % 60:02d}:{i % 60:02d} 198.51.100.{i % 254 + 1} GET /a/{i} 200"
            for i in range(3000)
        ]
        lines += [
            "#Software: Microsoft Internet Information Services 10.0",
            "#Date: 2026-01-01 02:00:00",
            "#Fields: date time s-ip cs-method cs-uri-stem s-port c-ip cs(User-Agent) sc-status time-taken",
        ]
        lines += [
            f"2026-01-01 02:{i // 60 % 60:02d}:{i % 60:02d} 192.0.2.9 POST /b/{i} 443 "
            f"203.0.113.{i % 254 + 1} Mozilla/5.0+(X11) 500 {i}"
            for i in range(3000)
        ]
        path.write_text("\n".join(lines) + "\n")

    def test_parallel_matches_serial_across_a_field_set_change(self, converter, tmp_path):
        # Parallel mode spawns worker processes that re-import the script as
        # __main__ — only possible when it runs as a real CLI process, so the
        # parallel run goes through subprocess.
        import os
        import subprocess
        import sys

        src = tmp_path / "u_ex260101.log"
        self._big_log(src)

        serial = tmp_path / "serial.parquet"
        _convert(converter, src, serial, workers=1)

        # Chunk small enough that several chunks start after the mid-file
        # directive change.
        env = dict(
            os.environ,
            W3C2VESTIGO_PARALLEL_MIN_BYTES="0",
            W3C2VESTIGO_MAX_CHUNK_BYTES="20000",
        )
        parallel = tmp_path / "parallel.parquet"
        proc = subprocess.run(
            [sys.executable, str(_SCRIPT), "-i", str(src), "-o", str(parallel), "-w", "4", "-v"],
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stderr
        # Guard against a vacuous pass: the run must actually have chunked,
        # into more chunks than there are directive blocks.
        chunks = int(re.search(r"parallel: (\d+) chunks", proc.stderr).group(1))
        assert chunks > 4, proc.stderr

        assert pq.read_table(serial).equals(pq.read_table(parallel))
        assert pq.ParquetFile(serial).metadata.num_rows == 6000

    def test_directive_scan_finds_every_header_line(self, converter, tmp_path):
        src = tmp_path / "u_ex260101.log"
        self._big_log(src)
        directives = converter.scan_directives(src)
        assert [line.split(":")[0] for line in dict(directives).values()] == [
            "#Software",
            "#Date",
            "#Fields",
            "#Software",
            "#Date",
            "#Fields",
        ]
        raw = src.read_bytes()
        for offset, line in directives:
            assert raw[offset:].split(b"\n", 1)[0].decode() == line

    def test_state_at_offset_reconstructs_the_active_field_set(self, converter, tmp_path):
        src = tmp_path / "u_ex260101.log"
        self._big_log(src)
        directives = converter.scan_directives(src)
        second_fields_offset = [o for o, line in directives if line.startswith("#Fields")][1]
        before = converter.state_at_offset(directives, second_fields_offset, "iis")
        after = converter.state_at_offset(directives, len(src.read_bytes()), "iis")
        assert before.fields[:3] == ["date", "time", "c-ip"]
        assert after.fields[:3] == ["date", "time", "s-ip"]
        assert after.field_sets == 2


class TestSplit:
    def test_parts_carry_the_full_provenance(self, converter, tmp_path):
        out = tmp_path / "out.parquet"
        rc = converter.convert(str(DATA / "w3c_iis.log"), str(out), 1, False, split="3")
        assert rc == 0
        parts = sorted(tmp_path.glob("out.part*.parquet"))
        assert len(parts) == 3
        assert not out.exists()
        total = 0
        for part in parts:
            pf = pq.ParquetFile(part)
            total += pf.metadata.num_rows
            meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            assert meta.converter_name == "w3c2vestigo"
        assert total == 3
