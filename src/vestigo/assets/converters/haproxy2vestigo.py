#!/usr/bin/env python3
"""Convert HAProxy logs to a Vestigo Parquet file.

Parses raw HAProxy logs (plain or ``.gz``, single file or a directory of
rotated logs) locally and writes one ``.parquet`` file in the Vestigo
interchange format (version 1). Upload the result to the Vestigo web
interface or ingest it with ``vestigo ingest`` — no CSV intermediate, no server
re-parse.

Two layers are detected per line, independently and without a flag:

  * the **envelope** the line arrived in — a Docker ``json-file`` record
    (``{"log": ..., "stream": ..., "time": ...}``), a BSD syslog prefix
    (``May 15 08:02:22 lb01 haproxy[1234]: ...``), or none at all;
  * the **HAProxy payload** shape — HTTP log format, TCP log format, a
    connection/SSL error line, or a startup/reload admin line.

Timestamps: HAProxy's own ``accept_date`` carries no UTC offset, so it never
sets the event timestamp when a UTC-anchored clock is available. A Docker
envelope's ``time`` field is RFC 3339 with an explicit zone and wins; each row
records which clock it used in ``timestamp_desc`` ("Log Write Time" vs "HTTP
Request Time"), and ``accept_date`` survives verbatim as an attribute. Rather
than assert that the container ran UTC, the converter *measures* it: the
smallest observed ``docker_time - accept_date`` skew is written to the
``vestigo.parse_decisions`` footer, where a whole-hour value is the tell that
it did not. The smallest is used rather than the median because the difference
is session duration plus write latency — a tarpit or a slow backend inflates
every other statistic while the clocks stay aligned.

Forensic provenance embedded in the output:
  * per input file: sha256 + size in the Parquet footer metadata,
  * per event row: the sha256 of its original file (``file_hash``), the byte
    offset of the line within that file (``byte_offset``; offsets into the
    *decompressed* stream for ``.gz`` inputs), and the sha256 of the original
    line — the whole envelope record, not the unwrapped payload
    (``content_hash``),
  * the converter name and version, which become the server-side parser
    identity.

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow haproxy2vestigo.py ...

Usage:

    python haproxy2vestigo.py -i haproxy.log -o haproxy.parquet
    python haproxy2vestigo.py -i container-json.log -o haproxy.parquet -w 8
    python haproxy2vestigo.py -i /var/log/haproxy/ -o haproxy.parquet
"""

from __future__ import annotations

import argparse
import collections
import concurrent.futures
import datetime
import gzip
import hashlib
import io
import ipaddress
import json
import multiprocessing
import os
import re
import statistics
import sys
import urllib.parse
from pathlib import Path
from typing import Any, BinaryIO

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "error: pyarrow is required to write Vestigo Parquet files.\n"
        "Install it with:  pip install pyarrow\n"
        "or run this script via:  uv run --with pyarrow haproxy2vestigo.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "haproxy2vestigo"
CONVERTER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# Vestigo Parquet interchange format v1 — embedded copy of the spec in
# src/vestigo/ingestion/parquet_format.py (this script is a standalone
# download and cannot import it; the repo test suite asserts both stay equal).
# ---------------------------------------------------------------------------

FORMAT_VERSION = "1"
META_FORMAT_VERSION = "vestigo.format_version"
META_CONVERTER_NAME = "vestigo.converter_name"
META_CONVERTER_VERSION = "vestigo.converter_version"
META_ORIGINAL_FILES = "vestigo.original_files"
# Additive forensic footer metadata (Tier 1). Ignored by older readers.
META_CONVERTED_AT = "vestigo.converted_at"
META_ROW_COUNTS = "vestigo.row_counts"
META_TIMEZONE_ASSUMPTION = "vestigo.timezone_assumption"
META_PARSE_DECISIONS = "vestigo.parse_decisions"

PARQUET_EVENT_SCHEMA = pa.schema(
    [
        pa.field("source_file", pa.string()),
        pa.field("file_hash", pa.string()),
        pa.field("byte_offset", pa.uint64()),
        pa.field("content_hash", pa.string()),
        pa.field("message", pa.string()),
        pa.field("timestamp", pa.timestamp("ms", tz="UTC")),
        pa.field("timestamp_desc", pa.string()),
        pa.field("artifact", pa.string()),
        pa.field("artifact_long", pa.string()),
        pa.field("display_name", pa.string()),
        pa.field("tags", pa.list_(pa.string())),
        pa.field("attributes", pa.map_(pa.string(), pa.string())),
    ]
)

# ---------------------------------------------------------------------------
# Timestamps
# ---------------------------------------------------------------------------

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}  # fmt: skip

# HAProxy's accept_date: "15/May/2026:08:02:22.053". No timezone, ever.
_RE_ACCEPT_DATE = re.compile(r"^(\d{1,2})/(\w{3})/(\d{4}):(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?$")


def parse_accept_date(value: str) -> datetime.datetime | None:
    """Parse HAProxy's ``accept_date``, treating it as UTC.

    The format carries no offset. Callers that have a UTC-anchored clock use
    this only to measure the skew against it; callers that do not use it as
    the timestamp and say so in the footer metadata.
    """
    m = _RE_ACCEPT_DATE.match(value.strip())
    if not m:
        return None
    month = _MONTHS.get(m.group(2).lower())
    if month is None:
        return None
    frac = (m.group(7) or "").ljust(6, "0")
    try:
        return datetime.datetime(
            int(m.group(3)),
            month,
            int(m.group(1)),
            int(m.group(4)),
            int(m.group(5)),
            int(m.group(6)),
            int(frac),
            tzinfo=datetime.UTC,
        )
    except ValueError:
        return None


def parse_rfc3339(value: str) -> datetime.datetime | None:
    """Parse a Docker ``time`` field (RFC 3339, up to nanosecond precision).

    ``fromisoformat`` rejects more than six fractional digits, which is
    exactly what the Docker json-file driver writes, so the fraction is
    truncated to microseconds first.
    """
    text = value.strip()
    if not text:
        return None
    text = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), text, count=1)
    try:
        dt = datetime.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _parse_since_until(value: str | None) -> datetime.datetime | None:
    """Parse an ISO 8601 ``--since``/``--until`` value to a UTC-aware datetime."""
    if not value:
        return None
    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


# ---------------------------------------------------------------------------
# Envelope layer
# ---------------------------------------------------------------------------

# BSD syslog: "May 15 08:02:22 lb01 haproxy[1234]: <payload>", optionally
# preceded by an RFC 3164 priority such as "<134>".
_RE_SYSLOG = re.compile(
    r"^(?:<\d{1,3}>)?"
    r"(?P<mon>[A-Z][a-z]{2})\s+(?P<day>\d{1,2}) (?P<time>\d{2}:\d{2}:\d{2}) "
    r"(?P<host>\S+) (?P<prog>[^\s:\[]+)(?:\[(?P<pid>\d+)\])?: (?P<payload>.*)$"
)


def unwrap(line: str) -> tuple[str, dict[str, str], datetime.datetime | None] | None:
    """Strip a transport envelope off one raw file line.

    Returns ``(payload, envelope_attributes, utc_timestamp_or_None)``, or
    ``None`` when the line claims to be an envelope but is malformed (a
    truncated Docker JSON record) and must be counted as skipped.
    """
    stripped = line.strip()
    if stripped.startswith("{"):
        try:
            obj = json.loads(stripped)
        except ValueError:
            return None
        if isinstance(obj, dict) and "log" in obj:
            attrs = {"envelope": "docker"}
            if obj.get("stream"):
                attrs["docker_stream"] = str(obj["stream"])
            ts = None
            if obj.get("time"):
                attrs["docker_time"] = str(obj["time"])
                ts = parse_rfc3339(str(obj["time"]))
            return str(obj.get("log") or "").rstrip("\r\n"), attrs, ts
        return None

    m = _RE_SYSLOG.match(line)
    if m:
        attrs = {
            "envelope": "syslog",
            "syslog_host": m.group("host"),
            "syslog_program": m.group("prog"),
        }
        if m.group("pid"):
            attrs["syslog_pid"] = m.group("pid")
        # The syslog prefix has no year and no offset, so it is recorded but
        # never used as the clock — accept_date is strictly better.
        attrs["syslog_time"] = f"{m.group('mon')} {m.group('day')} {m.group('time')}"
        return m.group("payload"), attrs, None

    return line, {"envelope": "raw"}, None


# ---------------------------------------------------------------------------
# HAProxy payload layer
# ---------------------------------------------------------------------------

# HTTP log format:
#   client_ip:client_port [accept_date] frontend_name backend_name/server_name
#   TR/Tw/Tc/Tr/Ta status_code bytes_read captured_request_cookie
#   captured_response_cookie termination_state
#   actconn/feconn/beconn/srv_conn/retries srv_queue/backend_queue
#   {captured_request_headers} {captured_response_headers} "http_request"
_RE_HTTP = re.compile(
    r"^(?P<client_ip>\S+):(?P<client_port>\d+) \[(?P<accept>[^\]]+)\] "
    r"(?P<frontend>\S+) (?P<backend>[^\s/]+)/(?P<server>\S+) "
    r"(?P<t1>[+-]?\d+)/(?P<t2>[+-]?\d+)/(?P<t3>[+-]?\d+)/(?P<t4>[+-]?\d+)/(?P<t5>[+-]?\d+) "
    r"(?P<status>-?\d+) (?P<bytes>\+?\d+) "
    r"(?P<req_cookie>\S+) (?P<resp_cookie>\S+) (?P<term>[A-Za-z-]{4}) "
    r"(?P<actconn>\d+)/(?P<feconn>\d+)/(?P<beconn>\d+)/(?P<srvconn>\d+)/(?P<retries>\+?\d+) "
    r"(?P<srvq>\d+)/(?P<beq>\d+)"
    r"(?P<captures>(?: \{[^}]*\}){0,2}) "
    r'"(?P<request>.*)"$'
)

# TCP log format: three timers, no status code, no cookies, no request.
_RE_TCP = re.compile(
    r"^(?P<client_ip>\S+):(?P<client_port>\d+) \[(?P<accept>[^\]]+)\] "
    r"(?P<frontend>\S+) (?P<backend>[^\s/]+)/(?P<server>\S+) "
    r"(?P<t1>[+-]?\d+)/(?P<t2>[+-]?\d+)/(?P<t3>[+-]?\d+) "
    r"(?P<bytes>\+?\d+) (?P<term>[A-Za-z-]{2,4}) "
    r"(?P<actconn>\d+)/(?P<feconn>\d+)/(?P<beconn>\d+)/(?P<srvconn>\d+)/(?P<retries>\+?\d+) "
    r"(?P<srvq>\d+)/(?P<beq>\d+)$"
)

# Connection/SSL error line:
#   client_ip:client_port [accept_date] frontend/bind_id: message
_RE_CONN_ERROR = re.compile(
    r"^(?P<client_ip>\S+):(?P<client_port>\d+) \[(?P<accept>[^\]]+)\] "
    r"(?P<frontend>[^\s/]+)/(?P<bind>[^\s:]+): (?P<message>.+)$"
)

# Startup/reload line: "[NOTICE]   (1) : New worker (8) forked"
_RE_ADMIN = re.compile(r"^\[(?P<level>[A-Z]+)\]\s*\((?P<pid>\d+)\)\s*:\s*(?P<message>.*)$")

_RE_CAPTURE_GROUP = re.compile(r"\{([^}]*)\}")

# Character 1 of the termination state: what ended the session.
_TERM_EVENT = {
    "C": "client aborted the connection",
    "S": "server aborted the connection or returned an error",
    "P": "proxy intercepted the session",
    "L": "proxy locally processed and closed the session",
    "R": "a resource was exhausted on the proxy",
    "I": "internal error on the proxy",
    "D": "session killed because the server was down",
    "U": "session killed in the queue because the backend went down",
    "K": "session actively killed by an administrator",
    "c": "client-side timeout expired",
    "s": "server-side timeout expired",
    "-": "normal session completion",
}

# Character 2 of the termination state: the phase the session was in.
_TERM_SESSION_STATE = {
    "R": "waiting for a complete valid request",
    "Q": "waiting in the queue for a server slot",
    "C": "waiting for the connection to the server to establish",
    "H": "waiting for or processing the server response headers",
    "D": "data phase transfer",
    "L": "pushing the last data to the client",
    "T": "request was tarpitted",
    "t": "request was tarpitted and a response was sent",
    "-": "normal session completion",
}

_ARTIFACTS = {
    "http": ("haproxy:http", "web:access:request", "HTTP Request Time"),
    "tcp": ("haproxy:tcp", "network:proxy:session", "Session Accept Time"),
    "error": ("haproxy:error", "web:error:log", "Connection Error Time"),
    "admin": ("haproxy:admin", "service:daemon:log", "Log Write Time"),
    "message": ("haproxy:message", "service:daemon:log", "Log Write Time"),
}

# Shapes that prove the file really is HAProxy (the catch-all does not).
_STRUCTURED_KINDS = frozenset({"http", "tcp", "error", "admin"})


def normalize_ip(value: str | None) -> str:
    """Validate and canonicalize a single IPv4/IPv6 address string."""
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return ""


def _counter(value: str, attrs: dict[str, Any], flag: str) -> str:
    """Strip HAProxy's ``+`` truncation marker, recording that it was there."""
    if value.startswith("+"):
        attrs[flag] = "true"
        return value[1:]
    return value


def _split_captures(text: str, attrs: dict[str, Any]) -> None:
    """Split the ``{...}`` capture groups off an HTTP log line.

    HAProxy emits the request capture first and the response capture second;
    with only a response capture configured it still emits an empty request
    group, so a single group is always the request one.
    """
    groups = _RE_CAPTURE_GROUP.findall(text)
    if not groups:
        return
    attrs["captured_request_headers"] = groups[0]
    if len(groups) > 1:
        attrs["captured_response_headers"] = groups[1]
    # A capture that is a bare IP is the real client behind this proxy — the
    # overwhelmingly common capture (an X-Forwarded-For header).
    real_ip = normalize_ip(groups[0])
    if real_ip:
        attrs["client_real_ip"] = real_ip


def _decode_termination(state: str, attrs: dict[str, Any]) -> None:
    """Expand the termination state's characters into readable attributes."""
    attrs["termination_state"] = state
    if len(state) >= 1:
        attrs["term_event"] = state[0]
        attrs["term_event_desc"] = _TERM_EVENT.get(state[0], "")
    if len(state) >= 2:
        attrs["term_session_state"] = state[1]
        attrs["term_session_state_desc"] = _TERM_SESSION_STATE.get(state[1], "")
    # The persistence-cookie characters are kept raw: their meaning depends on
    # the backend's cookie configuration, so a fixed gloss would be a guess.
    if len(state) >= 3:
        attrs["term_cookie_client"] = state[2]
    if len(state) >= 4:
        attrs["term_cookie_server"] = state[3]


def _split_request(request: str, attrs: dict[str, Any]) -> None:
    """Split the quoted HTTP request line into method / URI / protocol."""
    attrs["http_request"] = request
    parts = request.split(" ")
    if parts and parts[0]:
        attrs["http_method"] = parts[0]
    if len(parts) < 2:
        return
    uri = parts[1]
    attrs["http_uri"] = uri
    if len(parts) > 2:
        attrs["http_protocol"] = parts[2]
    try:
        split = urllib.parse.urlsplit(uri)
    except ValueError:
        return
    if split.netloc:
        attrs["http_host"] = split.netloc
    if split.path:
        attrs["http_path"] = split.path
    if split.query:
        attrs["http_query"] = split.query


def _frontend(name: str, attrs: dict[str, Any]) -> None:
    """Record the frontend, splitting off HAProxy's ``~`` SSL marker."""
    if name.endswith("~"):
        attrs["frontend"] = name[:-1]
        attrs["frontend_ssl"] = "true"
    else:
        attrs["frontend"] = name


def parse_payload(payload: str) -> tuple[str, dict[str, Any], str] | None:
    """Parse one unwrapped HAProxy line.

    Returns ``(kind, attributes, accept_date)`` — ``accept_date`` is the raw
    string, empty when the shape has none. Returns ``None`` for an empty line.
    """
    text = payload.strip()
    if not text:
        return None

    m = _RE_HTTP.match(text)
    if m:
        attrs: dict[str, Any] = {
            "src_ip": normalize_ip(m.group("client_ip")) or m.group("client_ip"),
            "src_port": m.group("client_port"),
            "backend": m.group("backend"),
            "backend_server": m.group("server"),
            "time_request_ms": m.group("t1"),
            "time_queue_ms": m.group("t2"),
            "time_connect_ms": m.group("t3"),
            "time_response_ms": m.group("t4"),
            "status_code": m.group("status"),
            "conn_active": m.group("actconn"),
            "conn_frontend": m.group("feconn"),
            "conn_backend": m.group("beconn"),
            "conn_server": m.group("srvconn"),
            "queue_server": m.group("srvq"),
            "queue_backend": m.group("beq"),
        }
        _frontend(m.group("frontend"), attrs)
        attrs["time_total_ms"] = _counter(m.group("t5"), attrs, "time_total_truncated")
        attrs["bytes_read"] = _counter(m.group("bytes"), attrs, "bytes_read_truncated")
        attrs["retries"] = _counter(m.group("retries"), attrs, "retries_truncated")
        if m.group("req_cookie") != "-":
            attrs["request_cookie"] = m.group("req_cookie")
        if m.group("resp_cookie") != "-":
            attrs["response_cookie"] = m.group("resp_cookie")
        _decode_termination(m.group("term"), attrs)
        _split_captures(m.group("captures"), attrs)
        _split_request(m.group("request"), attrs)
        return "http", attrs, m.group("accept")

    m = _RE_TCP.match(text)
    if m:
        attrs = {
            "src_ip": normalize_ip(m.group("client_ip")) or m.group("client_ip"),
            "src_port": m.group("client_port"),
            "backend": m.group("backend"),
            "backend_server": m.group("server"),
            "time_queue_ms": m.group("t1"),
            "time_connect_ms": m.group("t2"),
            "conn_active": m.group("actconn"),
            "conn_frontend": m.group("feconn"),
            "conn_backend": m.group("beconn"),
            "conn_server": m.group("srvconn"),
            "queue_server": m.group("srvq"),
            "queue_backend": m.group("beq"),
        }
        _frontend(m.group("frontend"), attrs)
        attrs["time_total_ms"] = _counter(m.group("t3"), attrs, "time_total_truncated")
        attrs["bytes_read"] = _counter(m.group("bytes"), attrs, "bytes_read_truncated")
        attrs["retries"] = _counter(m.group("retries"), attrs, "retries_truncated")
        _decode_termination(m.group("term"), attrs)
        return "tcp", attrs, m.group("accept")

    m = _RE_CONN_ERROR.match(text)
    if m:
        attrs = {
            "src_ip": normalize_ip(m.group("client_ip")) or m.group("client_ip"),
            "src_port": m.group("client_port"),
            "frontend": m.group("frontend"),
            "bind_id": m.group("bind"),
            "error_message": m.group("message"),
        }
        return "error", attrs, m.group("accept")

    m = _RE_ADMIN.match(text)
    if m:
        return (
            "admin",
            {
                "log_level": m.group("level"),
                "worker_pid": m.group("pid"),
                "admin_message": m.group("message"),
            },
            "",
        )

    # Inside a file that sniffed as HAProxy, a shape we do not model is still
    # evidence. Keep it whole rather than dropping it.
    return "message", {}, ""


def parse_line(line: str) -> tuple[dict[str, Any], int | None] | None:
    """Parse one raw file line into an event row.

    Returns ``(row, skew_ms)`` where ``skew_ms`` is the measured
    ``envelope_clock - accept_date`` difference for this line, or ``None``
    when the line has no such pair. Returns ``None`` when the line is a
    malformed envelope and should count as skipped.
    """
    unwrapped = unwrap(line)
    if unwrapped is None:
        return None
    payload, envelope_attrs, envelope_ts = unwrapped

    parsed = parse_payload(payload)
    if parsed is None:
        return None
    kind, attrs, accept_raw = parsed

    accept_dt = parse_accept_date(accept_raw) if accept_raw else None
    skew_ms: int | None = None
    if accept_raw:
        attrs["haproxy_accept_date"] = accept_raw
    if envelope_ts is not None and accept_dt is not None:
        skew_ms = round((envelope_ts - accept_dt).total_seconds() * 1000)

    artifact, artifact_long, accept_desc = _ARTIFACTS[kind]
    if envelope_ts is not None:
        timestamp = envelope_ts
        timestamp_desc = "Log Write Time"
    else:
        timestamp = accept_dt
        timestamp_desc = accept_desc if accept_dt is not None else ""

    row = {
        "message": payload.strip(),
        "timestamp": timestamp,
        "timestamp_desc": timestamp_desc,
        "artifact": artifact,
        "artifact_long": artifact_long,
        "attributes": {**envelope_attrs, **attrs},
    }
    return row, skew_ms


def line_kind(line: str) -> str | None:
    """Classify a raw line for sniffing; ``None`` for a malformed envelope."""
    unwrapped = unwrap(line)
    if unwrapped is None:
        return None
    parsed = parse_payload(unwrapped[0])
    return parsed[0] if parsed else None


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------

SNIFF_LINES = 200


def _open_log(path: Path) -> Any:
    """Open a plain or gzipped log file for reading text lines."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def sniff_haproxy(path: Path) -> bool:
    """Return True when the file's first lines contain a real HAProxy shape.

    The payload parser has a catch-all, so without this gate any text file at
    all would convert "successfully" into rows of unstructured messages.
    """
    sampled = 0
    try:
        with _open_log(path) as fh:
            for line in fh:
                if not line.strip():
                    continue
                if line_kind(line) in _STRUCTURED_KINDS:
                    return True
                sampled += 1
                if sampled >= SNIFF_LINES:
                    break
    except OSError:
        return False
    return False


def find_log_files(input_path: str) -> list[Path]:
    """Resolve the input into a list of HAProxy log files."""
    path = Path(input_path)
    if path.is_file():
        if not sniff_haproxy(path):
            raise SystemExit(
                f"error: {input_path} does not look like a HAProxy log — none of its "
                f"first {SNIFF_LINES} lines matched the HTTP log, TCP log, connection "
                "error, or startup/reload format (in a Docker json-file, syslog, or "
                "bare envelope)."
            )
        return [path]
    if path.is_dir():
        found = [p for p in sorted(path.iterdir()) if p.is_file() and sniff_haproxy(p)]
        if not found:
            raise SystemExit(f"error: no HAProxy log files found in {input_path}")
        return found
    raise SystemExit(f"error: input path not found: {input_path}")


def hash_file(path: Path) -> tuple[str, int]:
    """Return the streaming sha256 hex digest and size of ``path``."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as fh:
        while chunk := fh.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


# ---------------------------------------------------------------------------
# Row batching / Parquet writing
# ---------------------------------------------------------------------------

BATCH_ROWS = 50_000
# Plain files above this size are parsed in parallel chunks; .gz never is.
# Env-overridable for benchmarking/tests.
PARALLEL_MIN_BYTES = int(os.environ.get("HAPROXY2VESTIGO_PARALLEL_MIN_BYTES", 256 * 1024 * 1024))
# No single parallel chunk may exceed this many bytes, so per-worker memory
# stays bounded on huge files.
MAX_CHUNK_BYTES = int(os.environ.get("HAPROXY2VESTIGO_MAX_CHUNK_BYTES", 128 * 1024 * 1024))
# Default cap on parallel workers; high core counts otherwise multiply peak RAM.
DEFAULT_MAX_WORKERS = int(os.environ.get("HAPROXY2VESTIGO_DEFAULT_WORKERS", 4))
# Skew samples kept per stream/chunk. Chunks are spread across the file, so a
# small per-chunk cap still samples the whole timeline — and keeps the value
# cheap to ship back from a worker process.
SKEW_SAMPLES_PER_CHUNK = 512


class _BatchBuffer:
    """Columnar row buffer flushed to a ParquetWriter as record batches."""

    def __init__(self, writer: pq.ParquetWriter) -> None:
        self._writer = writer
        self._columns: dict[str, list[Any]] = {name: [] for name in PARQUET_EVENT_SCHEMA.names}
        self.rows_written = 0

    def append(
        self, source_file: str, file_hash: str, byte_offset: int, line: str, row: dict[str, Any]
    ) -> None:
        cols = self._columns
        cols["source_file"].append(source_file)
        cols["file_hash"].append(file_hash)
        cols["byte_offset"].append(byte_offset)
        cols["content_hash"].append(hashlib.sha256(line.encode("utf-8")).hexdigest())
        cols["message"].append(row["message"])
        cols["timestamp"].append(row["timestamp"])
        cols["timestamp_desc"].append(row["timestamp_desc"])
        cols["artifact"].append(row["artifact"])
        cols["artifact_long"].append(row["artifact_long"])
        cols["display_name"].append("")
        cols["tags"].append([])
        # Drop empty values — the server strips them anyway; smaller file.
        cols["attributes"].append(
            {k: str(v) for k, v in row["attributes"].items() if v is not None and str(v) != ""}
        )
        if len(cols["source_file"]) >= BATCH_ROWS:
            self.flush()

    def write_batch(self, batch: pa.RecordBatch) -> None:
        self._writer.write_batch(batch)
        self.rows_written += batch.num_rows

    def flush(self) -> None:
        if not self._columns["source_file"]:
            return
        batch = pa.RecordBatch.from_pydict(self._columns, schema=PARQUET_EVENT_SCHEMA)
        self.write_batch(batch)
        self._columns = {name: [] for name in PARQUET_EVENT_SCHEMA.names}


def _iter_lines_with_offsets(fh: BinaryIO) -> Any:
    """Yield ``(byte_offset, decoded_line)`` from a binary stream.

    Offsets count raw stream bytes (decompressed content for ``.gz``), lines
    are decoded utf-8 with replacement so undecodable bytes cannot shift the
    offsets of later lines. Trailing newlines are stripped from the yielded
    line; offsets always advance by the full raw line length.
    """
    offset = 0
    for raw in fh:
        line = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
        yield offset, line
        offset += len(raw)


def _convert_stream(
    fh: BinaryIO,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    start_offset: int = 0,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[int, int, int, list[int]]:
    """Parse a binary line stream into the buffer.

    Returns ``(parsed, skipped, skipped_by_time, skew_samples)``.
    """
    parsed = 0
    skipped = 0
    skipped_by_time = 0
    skews: list[int] = []
    for offset, line in _iter_lines_with_offsets(fh):
        if not line.strip():
            continue
        result = parse_line(line)
        if result is None:
            skipped += 1
            continue
        row, skew_ms = result
        if skew_ms is not None and len(skews) < SKEW_SAMPLES_PER_CHUNK:
            skews.append(skew_ms)
        ts = row["timestamp"]
        if ts is not None:
            if since_dt is not None and ts < since_dt:
                skipped_by_time += 1
                continue
            if until_dt is not None and ts > until_dt:
                skipped_by_time += 1
                continue
        # ts is None (no clock anywhere on the line) → keep; the event is real,
        # it is just unanchored in time.
        buffer.append(source_file, file_hash, start_offset + offset, line, row)
        parsed += 1
    return parsed, skipped, skipped_by_time, skews


# ---------------------------------------------------------------------------
# Parallel chunked parsing (plain files only)
# ---------------------------------------------------------------------------


def find_chunk_boundaries(
    path: Path, target_chunks: int, max_chunk_bytes: int = MAX_CHUNK_BYTES
) -> list[tuple[int, int]]:
    """Split a plain file into newline-aligned ``(start, end)`` byte ranges.

    Seeks near each candidate boundary and scans forward to the next newline —
    no full-file scan. Returns at least one chunk covering the whole file.
    Chunks never exceed ``max_chunk_bytes`` so per-worker memory stays bounded.
    """
    size = path.stat().st_size
    if size == 0 or target_chunks <= 1:
        return [(0, size)]
    approx = min(size // target_chunks, max_chunk_bytes)
    if approx <= 0:
        approx = max_chunk_bytes
    boundaries = [0]
    with open(path, "rb") as fh:
        candidate = approx
        while candidate < size:
            if candidate <= boundaries[-1]:
                candidate += approx
                continue
            fh.seek(candidate)
            found = None
            while found is None:
                chunk = fh.read(4096)
                if not chunk:
                    found = size
                    break
                idx = chunk.find(b"\n")
                if idx >= 0:
                    found = candidate + idx + 1
                else:
                    candidate += len(chunk)
            if boundaries[-1] < found < size:
                boundaries.append(found)
            candidate = found + approx
    boundaries.append(size)
    return list(zip(boundaries, boundaries[1:], strict=False))


def _parse_chunk(
    path_str: str,
    start: int,
    end: int,
    source_file: str,
    file_hash: str,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[bytes, int, int, int, list[int]]:
    """Worker: parse ``[start, end)`` of a plain file, return Arrow IPC bytes.

    Top-level so it pickles under the spawn start method.
    """
    sink = io.BytesIO()
    writer_ipc = pa.ipc.new_stream(sink, PARQUET_EVENT_SCHEMA)

    class _IpcBuffer(_BatchBuffer):
        def __init__(self) -> None:
            self._columns = {name: [] for name in PARQUET_EVENT_SCHEMA.names}
            self.rows_written = 0

        def write_batch(self, batch: pa.RecordBatch) -> None:
            writer_ipc.write_batch(batch)
            self.rows_written += batch.num_rows

    buffer = _IpcBuffer()
    with open(path_str, "rb") as fh:
        fh.seek(start)
        window = fh.read(end - start)
    parsed, skipped, skipped_by_time, skews = _convert_stream(
        io.BytesIO(window),
        source_file,
        file_hash,
        buffer,
        start_offset=start,
        since_dt=since_dt,
        until_dt=until_dt,
    )
    buffer.flush()
    writer_ipc.close()
    return sink.getvalue(), parsed, skipped, skipped_by_time, skews


def _available_ram_bytes() -> int | None:
    """Best-effort available RAM in bytes (Linux MemAvailable, else total)."""
    try:
        with open("/proc/meminfo", "rb") as fh:
            for raw in fh:
                line = raw.decode("ascii", errors="replace")
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, AttributeError, OSError):
        return None


def _warn_if_ram_tight(workers: int) -> None:
    ram = _available_ram_bytes()
    # Rough per-worker estimate: raw chunk + parsed columns + Arrow IPC copy.
    estimated = workers * MAX_CHUNK_BYTES * 6
    if ram and estimated > ram * 0.75:
        sys.stderr.write(
            f"warning: {workers} workers x {MAX_CHUNK_BYTES // (1024 * 1024)} MiB chunks may "
            f"need ~{estimated // (1024 * 1024)} MiB RAM; ~{ram // (1024 * 1024)} MiB available. "
            "Reduce -w if memory runs out.\n"
        )


def _convert_file_parallel(
    path: Path,
    file_hash: str,
    buffer: _BatchBuffer,
    workers: int,
    verbose: bool,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[int, int, int, list[int]]:
    """Parse a large plain file across worker processes."""
    chunks = find_chunk_boundaries(path, target_chunks=workers * 4)
    if verbose:
        sys.stderr.write(f"  parallel: {len(chunks)} chunks, {workers} workers\n")
    _warn_if_ram_tight(workers)
    parsed_total = 0
    skipped_total = 0
    skipped_by_time_total = 0
    skews: list[int] = []
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        # Submit a bounded window and consume strictly in submit order: rows
        # land in the output in original file order (forensic requirement),
        # and at most ~2*workers chunk results exist in the parent at once,
        # so finished-but-unwritten Arrow IPC results cannot pile up and OOM
        # the parent when the Parquet writer is the bottleneck.
        chunk_iter = iter(chunks)
        pending: collections.deque = collections.deque()

        def _submit_next() -> None:
            for start, end in chunk_iter:
                pending.append(
                    pool.submit(
                        _parse_chunk,
                        str(path),
                        start,
                        end,
                        path.name,
                        file_hash,
                        since_dt,
                        until_dt,
                    )
                )
                return

        for _ in range(workers * 2):
            _submit_next()
        while pending:
            ipc_bytes, parsed, skipped, skipped_by_time, chunk_skews = pending.popleft().result()
            _submit_next()
            parsed_total += parsed
            skipped_total += skipped
            skipped_by_time_total += skipped_by_time
            skews.extend(chunk_skews)
            reader = pa.ipc.open_stream(ipc_bytes)
            for batch in reader:
                if batch.num_rows:
                    buffer.write_batch(batch)
    return parsed_total, skipped_total, skipped_by_time_total, skews


# ---------------------------------------------------------------------------
# Output splitting (ported from the 2vestigo converter suite's --split)
# ---------------------------------------------------------------------------

_RE_SPLIT_SIZE = re.compile(r"^(\d+)\s*([KMG])(?:I?B)?$", re.IGNORECASE)

# Upper bound on the row-batch granularity when rotating parts by size; the
# actual batch is scaled down for small size limits (see split_parquet).
_SPLIT_SIZE_BATCH_ROWS = 8192


def parse_split_spec(value: str) -> tuple[str, int]:
    """Parse a ``--split`` specification.

    Returns ``("parts", n)`` for a bare integer (split into ``n`` parts with
    an equal number of rows) or ``("size", nbytes)`` for a size specification
    such as ``"512K"``, ``"4M"``, or ``"1GiB"`` (suffixes are KiB/MiB/GiB,
    i.e. 1024-based).
    """
    text = value.strip()
    if text.isdigit():
        n = int(text)
        if n < 1:
            raise SystemExit(
                f"error: invalid --split value {value!r}: number of parts must be at least 1"
            )
        return ("parts", n)
    m = _RE_SPLIT_SIZE.match(text)
    if m:
        amount = int(m.group(1))
        if amount < 1:
            raise SystemExit(f"error: invalid --split value {value!r}: size must be at least 1")
        factor = {"K": 1024, "M": 1024**2, "G": 1024**3}[m.group(2).upper()]
        return ("size", amount * factor)
    raise SystemExit(
        f"error: invalid --split value {value!r}: use N (number of parts) or "
        "NK/NM/NG (part size in KiB/MiB/GiB, e.g. 4M)"
    )


def _part_path(output: str, index: int) -> Path:
    """Return the output path for part ``index`` (1-based)."""
    out = Path(output)
    return out.with_name(f"{out.stem}.part{index:03d}{out.suffix}")


def split_parquet(src: Path, output: str, spec: tuple[str, int], verbose: bool) -> list[Path]:
    """Repartition the single-file conversion at ``src`` into part files.

    ``("parts", n)`` distributes the rows into at most ``n`` parts of
    ``ceil(total / n)`` rows each; ``("size", nbytes)`` rotates to a new part
    once the current one reaches ``nbytes``, checked at row-batch granularity
    so a part may overshoot by up to one batch. Rows keep their original
    order, are never duplicated across parts, and every part carries the full
    interchange schema and provenance metadata, so each part is independently
    ingestible.
    """
    mode, amount = spec
    pf = pq.ParquetFile(src)
    schema = pf.schema_arrow
    # Carry footer key-value metadata added after the write loop (e.g.
    # vestigo.row_counts via add_key_value_metadata) into every part, so each
    # part keeps the full provenance. schema_arrow.metadata only holds keys set
    # at writer-open time; the rest live in the file's FileMetaData KV.
    extra = {
        k: v
        for k, v in (pf.metadata.metadata or {}).items()
        if k != b"ARROW:schema" and k not in (schema.metadata or {})
    }
    if extra:
        merged = dict(schema.metadata or {})
        merged.update(extra)
        schema = schema.with_metadata(merged)
    total = pf.metadata.num_rows
    if mode == "parts":
        rows_per_part = -(-total // amount) if total else 0
        batch_rows = max(1, min(BATCH_ROWS, rows_per_part or 1))
    else:
        rows_per_part = 0
        # The batch is the rotation granularity (a part may overshoot the
        # limit by up to one batch), so scale it to the limit; the 128 B/row
        # divisor keeps the overshoot small even for well-compressing rows.
        batch_rows = max(64, min(_SPLIT_SIZE_BATCH_ROWS, amount // 128))

    parts: list[Path] = []
    writer: pq.ParquetWriter | None = None
    part_rows = 0

    def open_next() -> pq.ParquetWriter:
        nonlocal writer, part_rows
        if writer is not None:
            writer.close()
        path = _part_path(output, len(parts) + 1)
        parts.append(path)
        part_rows = 0
        writer = pq.ParquetWriter(str(path), schema, compression="zstd")
        return writer

    try:
        for batch in pf.iter_batches(batch_size=batch_rows):
            while batch.num_rows:
                if (
                    writer is None
                    or (mode == "parts" and part_rows >= rows_per_part)
                    or (mode == "size" and part_rows > 0 and parts[-1].stat().st_size >= amount)
                ):
                    open_next()
                take = batch.num_rows
                if mode == "parts":
                    take = min(take, rows_per_part - part_rows)
                writer.write_batch(batch.slice(0, take))
                part_rows += take
                batch = batch.slice(take)
        if writer is None:
            # Zero rows: still produce a first (empty, schema-only) part.
            open_next()
    finally:
        if writer is not None:
            writer.close()
    if verbose:
        for path in parts:
            sys.stderr.write(f"  wrote {path}\n")
    return parts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile of an already-collected sample."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(len(ordered) * fraction))]


def _skew_stats(skews: list[int]) -> dict[str, int | None]:
    """Summarize the measured ``envelope_clock - accept_date`` differences.

    The difference is *session duration plus write latency*: HAProxy logs an
    HTTP session when it ends, but stamps ``accept_date`` when it began. It is
    therefore bounded below by the clock offset and unbounded above — a proxy
    that tarpits requests for ten seconds pushes the median ten seconds out
    while its clock is perfectly aligned.

    Only the minimum is bounded by the offset, so that is what the timezone
    conclusion rests on; ``p05`` and ``median`` are reported beside it so a
    reader can see whether that minimum is an isolated outlier. A low
    percentile is not enough on its own — a log where every session is
    tarpitted has no fast session for it to land on.
    """
    if not skews:
        return {
            "p05": None,
            "median": None,
            "min": None,
            "samples": 0,
        }
    return {
        "p05": _percentile(skews, 0.05),
        "median": round(statistics.median(skews)),
        "min": min(skews),
        "samples": len(skews),
    }


def _timezone_note(stats: dict[str, int | None]) -> str:
    """Describe what the measured skew says about the source's clock."""
    floor = stats["min"]
    if floor is None:
        return (
            "no UTC-anchored clock in this input: HAProxy's accept_date carries no "
            "offset and is assumed UTC. If the host logged local time, every "
            "timestamp is off by that offset."
        )
    median = stats["median"]
    tail = (
        f" (the median is {median} ms, inflated by session duration — HAProxy "
        "stamps accept_date when a session begins and logs it when it ends)"
        if median is not None and abs(median - floor) >= 1000
        else ""
    )
    if abs(floor) < 60_000:
        return (
            f"timestamps come from the envelope's UTC clock. The smallest observed "
            f"envelope-minus-accept_date skew is {floor} ms, i.e. sub-minute, so "
            f"HAProxy's accept_date was being written on the same UTC clock{tail}."
        )
    return (
        f"timestamps come from the envelope's UTC clock. The smallest observed "
        f"envelope-minus-accept_date skew is {floor} ms ({floor / 3_600_000:.2f} h), "
        f"so HAProxy's accept_date was NOT written in UTC — read the "
        f"haproxy_accept_date attribute with that offset in mind{tail}."
    )


def convert(
    input_path: str,
    output: str,
    workers: int,
    verbose: bool,
    split: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Convert HAProxy logs at ``input_path`` into ``output`` (.parquet)."""
    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "Vestigo server detects the ingest parser strictly by file extension."
        )

    since_dt = _parse_since_until(since)
    until_dt = _parse_since_until(until)

    split_spec = parse_split_spec(split) if split else None
    write_target = output if split_spec is None else f"{output}.tmp"

    files = find_log_files(input_path)

    if verbose:
        sys.stderr.write(f"hashing {len(files)} input file(s)...\n")
    provenance = []
    hashes: dict[Path, str] = {}
    for path in files:
        digest, size = hash_file(path)
        hashes[path] = digest
        stat = path.stat()
        provenance.append(
            {
                "name": path.name,
                "sha256": digest,
                "size_bytes": size,
                "path": str(path.resolve()),
                "mtime": datetime.datetime.fromtimestamp(stat.st_mtime, datetime.UTC).isoformat(),
            }
        )

    metadata = {
        META_FORMAT_VERSION: FORMAT_VERSION,
        META_CONVERTER_NAME: CONVERTER_NAME,
        META_CONVERTER_VERSION: CONVERTER_VERSION,
        META_ORIGINAL_FILES: json.dumps(provenance, sort_keys=True),
        META_CONVERTED_AT: datetime.datetime.now(datetime.UTC).isoformat(),
    }

    parsed_total = 0
    skipped_total = 0
    skipped_by_time_total = 0
    skews: list[int] = []
    schema = PARQUET_EVENT_SCHEMA.with_metadata(metadata)
    with pq.ParquetWriter(write_target, schema, compression="zstd") as writer:
        buffer = _BatchBuffer(writer)
        for path in files:
            if verbose:
                sys.stderr.write(f"parsing {path}...\n")
            parallel = (
                path.suffix != ".gz" and workers > 1 and path.stat().st_size >= PARALLEL_MIN_BYTES
            )
            if parallel:
                parsed, skipped, skipped_by_time, file_skews = _convert_file_parallel(
                    path, hashes[path], buffer, workers, verbose, since_dt, until_dt
                )
            else:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rb") as fh:
                    parsed, skipped, skipped_by_time, file_skews = _convert_stream(
                        fh,
                        path.name,
                        hashes[path],
                        buffer,
                        since_dt=since_dt,
                        until_dt=until_dt,
                    )
            parsed_total += parsed
            skipped_total += skipped
            skipped_by_time_total += skipped_by_time
            skews.extend(file_skews)
        buffer.flush()
        skew_stats = _skew_stats(skews)
        writer.add_key_value_metadata(
            {
                META_ROW_COUNTS: json.dumps(
                    {
                        "parsed": parsed_total,
                        "skipped_malformed": skipped_total,
                        "skipped_by_time": skipped_by_time_total,
                    }
                ),
                META_TIMEZONE_ASSUMPTION: _timezone_note(skew_stats),
                META_PARSE_DECISIONS: json.dumps(
                    {
                        "timestamp_source": ("docker envelope where present, else accept_date"),
                        "accept_date_skew_ms_p05": skew_stats["p05"],
                        "accept_date_skew_ms_median": skew_stats["median"],
                        "accept_date_skew_ms_min": skew_stats["min"],
                        "accept_date_skew_samples": skew_stats["samples"],
                        "single_capture_group_read_as": "request",
                        "since": since,
                        "until": until,
                    },
                    sort_keys=True,
                ),
            }
        )

    time_note = f", {skipped_by_time_total} outside --since/--until" if (since or until) else ""
    if split_spec is not None:
        try:
            parts = split_parquet(Path(write_target), output, split_spec, verbose)
        finally:
            Path(write_target).unlink(missing_ok=True)
        sys.stderr.write(
            f"{CONVERTER_NAME}: wrote {parsed_total} events to {len(parts)} part "
            f"file(s) [{parts[0].name} .. {parts[-1].name}] "
            f"({skipped_total} unparseable lines skipped{time_note})\n"
        )
    else:
        sys.stderr.write(
            f"{CONVERTER_NAME}: wrote {parsed_total} events to {output} "
            f"({skipped_total} unparseable lines skipped{time_note})\n"
        )
    if verbose:
        sys.stderr.write(f"  {_timezone_note(skew_stats)}\n")
    return 0 if parsed_total > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert HAProxy logs (plain or .gz, file or directory; Docker "
            "json-file, syslog, or bare envelope) to a Vestigo Parquet file "
            "for direct upload."
        )
    )
    parser.add_argument("-i", "--input", required=True, help="log file or directory")
    parser.add_argument("-o", "--output", required=True, help="output .parquet path")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=min(getattr(os, "process_cpu_count", os.cpu_count)() or 4, DEFAULT_MAX_WORKERS),
        help="parallel parser processes for large plain files (default: min(CPU count, %(default)s))",
    )
    parser.add_argument(
        "--split",
        metavar="N|SIZE",
        help="split the output into multiple .parquet files: N = N parts with "
        "an equal number of rows (e.g. 4); SIZE = rotate to a new part once "
        "it reaches SIZE, with a K/M/G suffix meaning KiB/MiB/GiB (e.g. "
        "512M). Parts are named <name>.partNNN.parquet.",
    )
    parser.add_argument(
        "--since",
        help="Only entries at or after this ISO 8601 timestamp "
        "(e.g. 2026-07-01T00:00:00Z). Rows with no parseable timestamp are kept.",
    )
    parser.add_argument(
        "--until",
        help="Only entries at or before this ISO 8601 timestamp "
        "(e.g. 2026-07-01T23:59:59Z). Rows with no parseable timestamp are kept.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")
    args = parser.parse_args()
    return convert(
        args.input,
        args.output,
        max(1, args.workers),
        args.verbose,
        split=args.split,
        since=args.since,
        until=args.until,
    )


if __name__ == "__main__":
    sys.exit(main())
