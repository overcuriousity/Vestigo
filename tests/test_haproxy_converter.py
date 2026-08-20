"""Tests for the haproxy2vestigo Parquet converter script.

The converter is a standalone download (not an importable package module);
tests load it from its asset path via importlib.
"""

from __future__ import annotations

import gzip
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
    / "haproxy2vestigo.py"
)
DATA = Path(__file__).parent / "data"


@pytest.fixture(scope="module")
def converter():
    spec = importlib.util.spec_from_file_location("haproxy2vestigo", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _convert(converter, input_path: Path, out: Path, workers: int = 1) -> pq.ParquetFile:
    rc = converter.convert(str(input_path), str(out), workers=workers, verbose=False)
    assert rc == 0
    return pq.ParquetFile(out)


def _rows(converter, input_path: Path, out: Path, workers: int = 1) -> list[dict]:
    return _convert(converter, input_path, out, workers).read().to_pylist()


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
        pf = _convert(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        meta = parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
        assert meta.converter_name == "haproxy2vestigo"
        assert meta.converter_version == converter.CONVERTER_VERSION

    def test_rejects_non_parquet_output_extension(self, converter, tmp_path):
        with pytest.raises(SystemExit, match=r"\.parquet"):
            converter.convert(str(DATA / "haproxy_docker.log"), str(tmp_path / "out.csv"), 1, False)

    def test_rejects_a_file_that_is_not_haproxy(self, converter, tmp_path):
        alien = tmp_path / "nginx.log"
        alien.write_text(
            '192.168.1.10 - alice [25/Jun/2026:09:46:41 +0200] "GET /index.html HTTP/1.1" '
            '200 512 "-" "Mozilla/5.0"\n',
            encoding="utf-8",
        )
        with pytest.raises(SystemExit, match="HAProxy"):
            converter.convert(str(alien), str(tmp_path / "out.parquet"), 1, False)


class TestDockerEnvelope:
    def test_http_line(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        # 8 well-formed envelopes; the truncated JSON line is skipped.
        assert len(rows) == 8

        first = rows[0]
        # message is the inner haproxy line, not the JSON envelope.
        assert first["message"].startswith("192.0.2.10:51514 [01/Jun/2026:10:15:30.100]")
        assert first["artifact"] == "haproxy:http"
        assert first["artifact_long"] == "web:access:request"
        # Timestamp comes from the UTC-anchored docker clock, and says so.
        assert first["timestamp_desc"] == "Log Write Time"
        assert first["timestamp"].isoformat() == "2026-06-01T10:15:30.102000+00:00"

        attrs = dict(first["attributes"])
        assert attrs["envelope"] == "docker"
        assert attrs["docker_stream"] == "stdout"
        assert attrs["haproxy_accept_date"] == "01/Jun/2026:10:15:30.100"
        assert attrs["src_ip"] == "192.0.2.10"
        assert attrs["src_port"] == "51514"
        assert attrs["frontend"] == "fe_web"
        assert attrs["frontend_ssl"] == "true"
        assert attrs["backend"] == "be_app"
        assert attrs["backend_server"] == "app01"
        assert attrs["time_request_ms"] == "0"
        assert attrs["time_connect_ms"] == "1"
        assert attrs["time_response_ms"] == "24"
        assert attrs["time_total_ms"] == "25"
        assert attrs["status_code"] == "200"
        assert attrs["bytes_read"] == "4711"
        assert attrs["termination_state"] == "----"
        assert attrs["conn_active"] == "12"
        assert attrs["conn_frontend"] == "12"
        assert attrs["conn_backend"] == "3"
        assert attrs["conn_server"] == "1"
        assert attrs["retries"] == "0"
        assert attrs["queue_server"] == "0"
        assert attrs["queue_backend"] == "0"
        assert attrs["captured_request_headers"] == "203.0.113.5"
        assert attrs["client_real_ip"] == "203.0.113.5"
        assert attrs["http_method"] == "GET"
        assert attrs["http_uri"] == "https://shop.example.com/cart"
        assert attrs["http_host"] == "shop.example.com"
        assert attrs["http_path"] == "/cart"
        assert attrs["http_protocol"] == "HTTP/2.0"
        # Empty attributes are dropped rather than written.
        assert "request_cookie" not in attrs

    def test_termination_state_is_decoded(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        denied = dict(rows[1]["attributes"])
        assert denied["status_code"] == "403"
        assert denied["termination_state"] == "PT--"
        assert denied["term_event"] == "P"
        assert denied["term_event_desc"] == "proxy intercepted the session"
        assert denied["term_session_state"] == "T"
        assert denied["term_session_state_desc"] == "request was tarpitted"
        assert denied["backend_server"] == "<NOSRV>"
        # -1 timers mean "not measured" and are kept verbatim, not dropped.
        assert denied["time_connect_ms"] == "-1"

    def test_truncated_counter_prefixes_and_two_capture_groups(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        attrs = dict(rows[2]["attributes"])
        assert attrs["bytes_read"] == "52000"
        assert attrs["bytes_read_truncated"] == "true"
        assert attrs["time_total_ms"] == "37"
        assert attrs["retries"] == "1"
        assert attrs["captured_request_headers"] == "198.51.100.9|Mozilla/5.0"
        assert attrs["captured_response_headers"] == "text/html"
        # A capture group that is not a bare IP does not become client_real_ip.
        assert "client_real_ip" not in attrs
        assert attrs["http_method"] == "POST"
        assert attrs["http_path"] == "/api/v1/order"

    def test_tcp_line(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        tcp = rows[3]
        assert tcp["artifact"] == "haproxy:tcp"
        assert tcp["artifact_long"] == "network:proxy:session"
        attrs = dict(tcp["attributes"])
        assert attrs["frontend"] == "fe_smtp"
        assert "frontend_ssl" not in attrs
        assert attrs["backend_server"] == "mx01"
        assert attrs["time_queue_ms"] == "1"
        assert attrs["time_connect_ms"] == "0"
        assert attrs["time_total_ms"] == "1050"
        assert attrs["bytes_read"] == "342"
        assert attrs["termination_state"] == "--"
        assert "status_code" not in attrs

    def test_connection_error_line(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        err = rows[4]
        assert err["artifact"] == "haproxy:error"
        assert err["artifact_long"] == "web:error:log"
        attrs = dict(err["attributes"])
        assert attrs["src_ip"] == "192.0.2.14"
        assert attrs["frontend"] == "fe_web"
        assert attrs["bind_id"] == "2"
        assert attrs["error_message"] == "SSL handshake failure"

    def test_admin_lines(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        notice = rows[5]
        assert notice["artifact"] == "haproxy:admin"
        attrs = dict(notice["attributes"])
        assert attrs["docker_stream"] == "stderr"
        assert attrs["log_level"] == "NOTICE"
        assert attrs["worker_pid"] == "1"
        assert attrs["admin_message"] == "New worker (8) forked"

        warning = dict(rows[6]["attributes"])
        assert warning["log_level"] == "WARNING"
        assert warning["worker_pid"] == "8"

    def test_unmodelled_line_is_kept_not_dropped(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        bare = rows[7]
        # A shape we do not model still becomes an event — inside a file that
        # sniffed as HAProxy, dropping the line loses evidence.
        assert bare["artifact"] == "haproxy:message"
        assert bare["message"] == "Proxy fe_monit stopped (cumulated conns: FE: 0, BE: 0)."
        assert bare["timestamp"] is not None

    def test_provenance_anchors_to_the_envelope_line(self, converter, tmp_path):
        import hashlib

        raw = (DATA / "haproxy_docker.log").read_text(encoding="utf-8").splitlines()
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        # content_hash covers the original file line (the whole JSON record),
        # not the unwrapped payload.
        assert rows[0]["content_hash"] == hashlib.sha256(raw[0].encode("utf-8")).hexdigest()
        assert rows[0]["byte_offset"] == 0
        assert rows[1]["byte_offset"] == len(raw[0].encode("utf-8")) + 1
        assert rows[0]["source_file"] == "haproxy_docker.log"


class TestOtherEnvelopes:
    def test_syslog_envelope(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_syslog.log", tmp_path / "out.parquet")
        assert len(rows) == 4
        attrs = dict(rows[0]["attributes"])
        assert attrs["envelope"] == "syslog"
        assert attrs["syslog_host"] == "lb01"
        assert attrs["syslog_program"] == "haproxy"
        assert attrs["syslog_pid"] == "1234"
        assert attrs["frontend"] == "fe_web"
        # No UTC-anchored clock in the envelope, so accept_date carries the
        # timestamp and the row says which clock it used.
        assert rows[0]["timestamp_desc"] == "HTTP Request Time"
        assert rows[0]["timestamp"].isoformat() == "2026-06-01T10:15:30.100000+00:00"

    def test_bare_envelope(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_plain.log", tmp_path / "out.parquet")
        assert len(rows) == 6
        assert dict(rows[0]["attributes"])["envelope"] == "raw"
        assert rows[0]["timestamp"].isoformat() == "2026-06-01T10:15:30.100000+00:00"
        # An admin line in a bare file has no clock at all.
        assert rows[4]["artifact"] == "haproxy:admin"
        assert rows[4]["timestamp"] is None


class TestTimezoneEvidence:
    def test_measured_skew_is_recorded(self, converter, tmp_path):
        pf = _convert(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        kv = {
            k.decode(): v.decode() for k, v in pf.metadata.metadata.items() if k != b"ARROW:schema"
        }
        decisions = json.loads(kv[converter.META_PARSE_DECISIONS])
        # The fixture's container clock is UTC: docker write trails accept by
        # a couple of ms, not by a whole-hour offset.
        assert decisions["accept_date_skew_ms_p05"] == 1
        assert decisions["accept_date_skew_ms_min"] == 1
        assert decisions["accept_date_skew_ms_median"] == 2
        assert decisions["accept_date_skew_samples"] == 5
        assert decisions["timestamp_source"] == "docker envelope where present, else accept_date"
        assert "UTC" in kv[converter.META_TIMEZONE_ASSUMPTION]

    def test_skew_is_absent_without_a_reference_clock(self, converter, tmp_path):
        pf = _convert(converter, DATA / "haproxy_plain.log", tmp_path / "out.parquet")
        kv = {
            k.decode(): v.decode() for k, v in pf.metadata.metadata.items() if k != b"ARROW:schema"
        }
        decisions = json.loads(kv[converter.META_PARSE_DECISIONS])
        assert decisions["accept_date_skew_samples"] == 0
        assert decisions["accept_date_skew_ms_p05"] is None
        assert decisions["accept_date_skew_ms_median"] is None

    def test_a_long_session_does_not_read_as_a_clock_offset(self, converter, tmp_path):
        # HAProxy stamps accept_date when a session starts and logs it when it
        # ends, so a tarpit or a slow backend inflates the skew without the
        # clock having moved. The estimator must not mistake that for an offset.
        tarpitted = tmp_path / "tarpit.log"
        rows = []
        for i in range(20):
            accept = f"01/Jun/2026:10:{i:02d}:00.000"
            # 19 of 20 sessions sit in a 10 s tarpit; one completes immediately —
            # too few fast sessions for a 5th-percentile estimator to find.
            delay = 0.002 if i == 0 else 10.001
            secs = int(delay)
            written = f"2026-06-01T10:{i:02d}:{secs:02d}.{int((delay % 1) * 1e9):09d}Z"
            payload = (
                f"192.0.2.{i}:5000{i} [{accept}] fe_web~ fe_web/<NOSRV> "
                f"-1/-1/-1/-1/0 403 0 - - PT-- 1/1/0/0/0 0/0 {{203.0.113.1}} "
                f'"GET https://shop.example.com/ HTTP/1.1"'
            )
            rows.append(json.dumps({"log": payload + "\n", "stream": "stdout", "time": written}))
        tarpitted.write_text("\n".join(rows) + "\n", encoding="utf-8")

        pf = _convert(converter, tarpitted, tmp_path / "out.parquet")
        kv = {
            k.decode(): v.decode() for k, v in pf.metadata.metadata.items() if k != b"ARROW:schema"
        }
        decisions = json.loads(kv[converter.META_PARSE_DECISIONS])
        assert decisions["accept_date_skew_ms_median"] == 10001
        assert decisions["accept_date_skew_ms_min"] == 2
        note = kv[converter.META_TIMEZONE_ASSUMPTION]
        assert "same UTC clock" in note
        assert "inflated by session duration" in note

    def test_row_counts(self, converter, tmp_path):
        pf = _convert(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        kv = {
            k.decode(): v.decode() for k, v in pf.metadata.metadata.items() if k != b"ARROW:schema"
        }
        counts = json.loads(kv[converter.META_ROW_COUNTS])
        assert counts == {"parsed": 8, "skipped_malformed": 1, "skipped_by_time": 0}


class TestGzip:
    def test_gzipped_input(self, converter, tmp_path):
        src = DATA / "haproxy_docker.log"
        gz = tmp_path / "haproxy_docker.log.gz"
        gz.write_bytes(gzip.compress(src.read_bytes()))
        rows = _rows(converter, gz, tmp_path / "out.parquet")
        assert len(rows) == 8
        assert rows[0]["source_file"] == "haproxy_docker.log.gz"


class TestDirectoryInput:
    def test_directory_of_logs(self, converter, tmp_path):
        d = tmp_path / "logs"
        d.mkdir()
        (d / "haproxy.log").write_bytes((DATA / "haproxy_plain.log").read_bytes())
        (d / "haproxy.log.1").write_bytes((DATA / "haproxy_syslog.log").read_bytes())
        rows = _rows(converter, d, tmp_path / "out.parquet")
        assert len(rows) == 10
        assert {r["source_file"] for r in rows} == {"haproxy.log", "haproxy.log.1"}


class TestParallel:
    def test_chunk_boundaries_cover_file_without_overlap(self, converter, tmp_path):
        big = tmp_path / "haproxy.log"
        big.write_text((DATA / "haproxy_plain.log").read_text(encoding="utf-8") * 100)
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

        big = tmp_path / "haproxy.log"
        big.write_text((DATA / "haproxy_docker.log").read_text(encoding="utf-8") * 300)

        seq = _rows(converter, big, tmp_path / "seq.parquet", workers=1)
        env = dict(os.environ, HAPROXY2VESTIGO_PARALLEL_MIN_BYTES="0")
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
        par = pq.ParquetFile(tmp_path / "par.parquet").read().to_pylist()
        assert len(seq) == 8 * 300
        # Rows keep original file order in both modes, byte offsets included.
        assert [(r["byte_offset"], r["content_hash"]) for r in par] == [
            (r["byte_offset"], r["content_hash"]) for r in seq
        ]


class TestTimeWindow:
    def test_since_until(self, converter, tmp_path):
        rows = _rows(converter, DATA / "haproxy_docker.log", tmp_path / "out.parquet")
        assert len(rows) == 8
        rc = converter.convert(
            str(DATA / "haproxy_docker.log"),
            str(tmp_path / "win.parquet"),
            1,
            False,
            since="2026-06-01T10:15:31Z",
            until="2026-06-01T10:15:33.500Z",
        )
        assert rc == 0
        windowed = pq.ParquetFile(tmp_path / "win.parquet").read().to_pylist()
        assert [dict(r["attributes"]).get("src_port") for r in windowed] == [
            "33221",
            "40001",
            "44444",
        ]


class TestSplit:
    def test_split_into_parts(self, converter, tmp_path):
        out = tmp_path / "out.parquet"
        rc = converter.convert(str(DATA / "haproxy_docker.log"), str(out), 1, False, split="2")
        assert rc == 0
        parts = sorted(tmp_path.glob("out.part*.parquet"))
        assert len(parts) == 2
        total = []
        for part in parts:
            pf = pq.ParquetFile(part)
            # Every part is independently ingestible.
            parquet_format.validate_parquet_source(pf.schema_arrow, pf.schema_arrow.metadata)
            total.extend(pf.read().to_pylist())
        assert len(total) == 8
        assert not out.exists()
