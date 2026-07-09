#!/usr/bin/env python3
"""Convert Suricata IDS/IPS logs to a TraceSignal Parquet file.

Parses raw Suricata logs (plain or ``.gz``, single file or a directory) locally
and writes one ``.parquet`` file in the TraceSignal interchange format
(version 1). Upload the result to the TraceSignal web interface or ingest it
with ``tsig ingest`` — no CSV/JSONL intermediate, no server re-parse.

Supports three input formats, auto-detected per line so a single input can mix
formats if necessary:

- **EVE JSON** (``eve.json``): one JSON object per line, Suricata's native
  structured output. Any ``event_type`` is accepted; alerts are summarised by
  their signature, all other events by event type and key fields.
- **fast.log**: Suricata's classic single-line alert format.
- **OPNsense syslog export**: tab-separated syslog lines where the message
  payload is a fast.log-style alert, optionally prefixed with markers such as
  ``[wDrop]``.

Forensic provenance embedded in the output:
  * per input file: sha256 + size in the Parquet footer metadata,
  * per event row: the sha256 of its original file (``file_hash``), the byte
    offset of the line within that file (``byte_offset``; offsets into the
    *decompressed* stream for ``.gz`` inputs), and the sha256 of the line
    itself (``content_hash``),
  * the converter name and version, which become the server-side parser
    identity.

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow suricata2tracesignal.py ...

Usage:

    python suricata2tracesignal.py -i eve.json -o suricata.parquet
    python suricata2tracesignal.py -i /var/log/suricata/ -o suricata.parquet -w 8
"""

from __future__ import annotations

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
import sys
from pathlib import Path
from typing import Any, BinaryIO

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "error: pyarrow is required to write TraceSignal Parquet files.\n"
        "Install it with:  pip install pyarrow\n"
        "or run this script via:  uv run --with pyarrow suricata2tracesignal.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "suricata2tracesignal"
CONVERTER_VERSION = "1.0.0"

# ---------------------------------------------------------------------------
# TraceSignal Parquet interchange format v1 — embedded copy of the spec in
# src/tracesignal/ingestion/parquet_format.py (this script is a standalone
# download and cannot import it; the repo test suite asserts both stay equal).
# ---------------------------------------------------------------------------

FORMAT_VERSION = "1"
META_FORMAT_VERSION = "tracesignal.format_version"
META_CONVERTER_NAME = "tracesignal.converter_name"
META_CONVERTER_VERSION = "tracesignal.converter_version"
META_ORIGINAL_FILES = "tracesignal.original_files"

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
# Suricata line parsing (ported from suricata2timesketch.py, converter parity)
# ---------------------------------------------------------------------------

_FASTLOG_RE = re.compile(
    r"^(?P<ts>\d{2}/\d{2}/\d{4}-\d{2}:\d{2}:\d{2}\.\d+)\s+"
    r"\[\*\*\]\s+"
    r"\[(?P<gid>\d+):(?P<sid>\d+):(?P<rev>\d+)\]\s+"
    r"(?P<msg>.+?)\s+\[\*\*\]\s+"
    r"(?:\[Classification:\s*(?P<class>[^\]]+)\]\s+)?"
    r"(?:\[Priority:\s*(?P<priority>\d+)\]\s+)?"
    r"\{(?P<proto>[^}]+)\}\s+"
    r"(?P<src_ip>\S+):(?P<src_port>\d+)\s+->\s+"
    r"(?P<dst_ip>\S+):(?P<dst_port>\d+)\s*$"
)

_OPNSENSE_SYSLOG_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:[+-]\d{2}:?\d{2})?)\t"
    r"(?P<level>[^\t]+)\t"
    r"(?P<program>[^\t]+)\t"
    r"(?P<msg>.*)$"
)

_ALERT_PAYLOAD_RE = re.compile(
    r"^\s*(?:\[(?P<marker>[^\]]+)\]\s+)?"
    r"\[(?P<gid>\d+):(?P<sid>\d+):(?P<rev>\d+)\]\s+"
    r"(?P<msg>.+?)\s+"
    r"(?:\[Classification:\s*(?P<class>[^\]]+)\]\s+)?"
    r"(?:\[Priority:\s*(?P<priority>\d+)\]\s+)?"
    r"\{(?P<proto>[^}]+)\}\s+"
    r"(?P<src_ip>\S+):(?P<src_port>\d+)\s+->\s+"
    r"(?P<dst_ip>\S+):(?P<dst_port>\d+)\s*$"
)

_GENERIC_NOTICE_RE = re.compile(r"^\[(?P<pid>\d+)\]\s+<(?P<level>[^>]+)>\s+--\s+(?P<text>.*)$")

# EVE JSON raw field names that duplicate a suite-wide canonical column
# already promoted onto the row (protocol, dst_ip, dst_port); skipped during
# flattening to avoid emitting the same value under two attribute names.
_EVE_RAW_DUPLICATE_KEYS = {"proto", "dest_ip", "dest_port", "src_ip", "timestamp", "event_type"}


class SuricataParseError(Exception):
    """Raised when a Suricata line cannot be parsed."""


def normalize_ip(value: str | None) -> str:
    """Validate and canonicalize a single IPv4/IPv6 address string."""
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return ""


def _safe_int(value: Any) -> int | None:
    """Return ``value`` as int, or None if it is empty/non-numeric."""
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _parse_timestamp(value: str) -> datetime.datetime:
    """Parse a Suricata timestamp string into a UTC-aware datetime.

    Accepts EVE JSON ISO 8601, fast.log (``MM/DD/YYYY-HH:MM:SS.ffffff``), and
    OPNsense syslog ISO variants. Timestamps without timezone info are UTC.
    """
    value = value.strip()
    iso = value.replace("Z", "+00:00")
    dt = None
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        pass
    if dt is None:
        try:
            dt = datetime.datetime.strptime(value, "%m/%d/%Y-%H:%M:%S.%f")
        except ValueError:
            raise SuricataParseError(f"Unrecognised timestamp format: {value}") from None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.astimezone(datetime.timezone.utc)


def _flatten(obj: Any, prefix: str = "") -> dict[str, Any]:
    """Recursively flatten a nested dict into dot-notation keys."""
    result: dict[str, Any] = {}
    if isinstance(obj, dict):
        for key, value in obj.items():
            new_key = f"{prefix}.{key}" if prefix else str(key)
            if isinstance(value, dict):
                result.update(_flatten(value, new_key))
            else:
                result[new_key] = value
    else:
        result[prefix] = obj
    return result


def _detect_line_format(line: str) -> str:
    """Detect whether a line is EVE JSON, OPNsense syslog, or fast.log."""
    stripped = line.strip()
    if not stripped:
        return "empty"
    if stripped.startswith("{"):
        return "eve"
    if "\t" in line:
        parts = line.split("\t", 3)
        if len(parts) >= 3 and parts[2].lower() == "suricata":
            return "opnsense"
    return "fast"


def _build_alert_message(
    signature: str,
    category: str | None,
    proto: str,
    src: str,
    dst: str,
    marker: str | None = None,
) -> str:
    parts: list[str] = []
    if marker:
        parts.append(f"[{marker}]")
    parts.append(f"Suricata {proto} alert:")
    parts.append(signature)
    if category:
        parts.append(f"[{category}]")
    parts.append(f"{src} -> {dst}")
    return " ".join(parts)


def _build_eve_message(record: dict[str, Any]) -> str:
    event_type = record.get("event_type", "unknown")
    proto = record.get("proto", "")
    src_ip = record.get("src_ip", "")
    src_port = record.get("src_port", "")
    dest_ip = record.get("dest_ip", "")
    dest_port = record.get("dest_port", "")

    src = f"{src_ip}:{src_port}" if src_port not in (None, "") else src_ip
    dst = f"{dest_ip}:{dest_port}" if dest_port not in (None, "") else dest_ip

    alert = record.get("alert") or {}
    if alert:
        signature = alert.get("signature", "Unknown signature")
        category = alert.get("category")
        action = alert.get("action")
        parts = ["Suricata alert:"]
        if action:
            parts.append(f"[{action}]")
        parts.append(signature)
        if category:
            parts.append(f"[{category}]")
        if src or dst:
            parts.append(f"{src} -> {dst}")
        return " ".join(parts)

    if event_type == "http":
        http = record.get("http") or {}
        return (
            f"Suricata HTTP {http.get('http_method', '')} "
            f"{http.get('hostname', '')}{http.get('url', '')} ({src} -> {dst})"
        ).strip()
    if event_type == "dns":
        dns = record.get("dns") or {}
        return f"Suricata DNS query {dns.get('rrname', '')} ({src} -> {dst})".strip()
    if event_type == "tls":
        tls = record.get("tls") or {}
        return f"Suricata TLS {tls.get('sni', '')} ({src} -> {dst})".strip()
    if event_type in ("flow", "netflow"):
        return f"Suricata {event_type} {proto} {src} -> {dst}"
    return f"Suricata {event_type} {proto} {src} -> {dst}".strip()


def _parse_eve_line(line: str) -> dict[str, Any]:
    """Parse a single EVE JSON line into an event row dict."""
    try:
        record = json.loads(line)
    except json.JSONDecodeError as exc:
        raise SuricataParseError(f"Invalid JSON: {exc}") from exc
    if not isinstance(record, dict):
        raise SuricataParseError("JSON line is not an object")

    event_type = record.get("event_type", "unknown")
    ts_value = record.get("timestamp", "")
    if not ts_value:
        raise SuricataParseError("EVE record missing timestamp")
    dt = _parse_timestamp(str(ts_value))

    attrs: dict[str, Any] = {
        "event_type": event_type,
        "protocol": record.get("proto", ""),
        "src_ip": normalize_ip(record.get("src_ip", "")),
        "dst_ip": normalize_ip(record.get("dest_ip", "")),
        "src_port": _safe_int(record.get("src_port")),
        "dst_port": _safe_int(record.get("dest_port")),
    }

    if event_type == "alert":
        alert = record.get("alert") or {}
        attrs["alert_action"] = alert.get("action", "")
        attrs["alert_gid"] = _safe_int(alert.get("gid"))
        attrs["alert_signature_id"] = _safe_int(alert.get("signature_id"))
        attrs["alert_rev"] = _safe_int(alert.get("rev"))
        attrs["alert_signature"] = alert.get("signature", "")
        attrs["alert_category"] = alert.get("category", "")
        severity = _safe_int(alert.get("severity"))
        priority = _safe_int(alert.get("priority"))
        attrs["alert_severity"] = severity if severity is not None else priority
        attrs["alert_priority"] = priority if priority is not None else severity

    for key, value in record.items():
        if key in attrs or value is None or key == "alert" or key in _EVE_RAW_DUPLICATE_KEYS:
            continue
        if isinstance(value, dict):
            attrs.update(_flatten(value, key))
        elif not isinstance(value, (list, dict)):
            attrs[key] = value

    if "http.url" in attrs:
        attrs["url"] = attrs.pop("http.url")
    if "http.http_user_agent" in attrs:
        attrs["user_agent"] = attrs.pop("http.http_user_agent")

    artifact = "ids:alert:suricata" if event_type == "alert" else "ids:event:suricata"
    return {
        "message": _build_eve_message(record),
        "timestamp": dt,
        "timestamp_desc": f"Suricata {event_type} event time",
        "artifact": artifact,
        "artifact_long": "ids:suricata:event",
        "attributes": attrs,
    }


def _parse_fast_alert_match(
    match: re.Match[str], timestamp_str: str, marker: str | None = None
) -> dict[str, Any]:
    """Turn a fast.log / OPNsense alert regex match into an event row dict."""
    dt = _parse_timestamp(timestamp_str)

    src_ip = normalize_ip(match.group("src_ip"))
    dst_ip = normalize_ip(match.group("dst_ip"))
    src_port = _safe_int(match.group("src_port"))
    dst_port = _safe_int(match.group("dst_port"))
    src = f"{src_ip}:{src_port}" if src_port is not None else src_ip
    dst = f"{dst_ip}:{dst_port}" if dst_port is not None else dst_ip

    signature = match.group("msg").strip()
    category = (match.group("class") or "").strip()
    priority = _safe_int(match.group("priority"))
    proto = (match.group("proto") or "").strip().upper()
    gid = _safe_int(match.group("gid"))
    sid = _safe_int(match.group("sid"))
    rev = _safe_int(match.group("rev"))

    attrs: dict[str, Any] = {
        "src_ip": src_ip,
        "dst_ip": dst_ip,
        "src_port": src_port,
        "dst_port": dst_port,
        "protocol": proto,
        "event_type": "alert",
        "alert_action": "drop" if marker and marker.lower() == "wdrop" else "alert",
        "alert_gid": gid,
        "alert_signature_id": sid,
        "alert_rev": rev,
        "alert_signature": signature,
        "alert_category": category,
        "alert_priority": priority,
        "alert_severity": priority,
    }
    if marker:
        attrs["drop_marker"] = marker

    return {
        "message": _build_alert_message(signature, category or None, proto, src, dst, marker),
        "timestamp": dt,
        "timestamp_desc": "Suricata alert time",
        "artifact": "ids:alert:suricata",
        "artifact_long": "ids:suricata:event",
        "attributes": attrs,
    }


def _parse_fast_line(line: str) -> dict[str, Any] | None:
    """Parse a classic Suricata fast.log alert line."""
    match = _FASTLOG_RE.match(line)
    if match:
        return _parse_fast_alert_match(match, match.group("ts"))

    generic = _GENERIC_NOTICE_RE.match(line.strip())
    if generic:
        ts_str = line.strip().split(" ", 1)[0]
        try:
            dt = _parse_timestamp(ts_str)
        except SuricataParseError:
            return None
        return {
            "message": generic.group("text").strip(),
            "timestamp": dt,
            "timestamp_desc": "Suricata notice",
            "artifact": "ids:notice:suricata",
            "artifact_long": "ids:suricata:event",
            "attributes": {"event_type": "notice", "suricata_pid": _safe_int(generic.group("pid"))},
        }
    return None


def _parse_opnsense_line(line: str) -> dict[str, Any] | None:
    """Parse an OPNsense syslog export line containing a Suricata alert."""
    syslog = _OPNSENSE_SYSLOG_RE.match(line)
    if not syslog:
        return None

    timestamp_str = syslog.group("ts")
    message = syslog.group("msg")

    alert = _ALERT_PAYLOAD_RE.match(message)
    if alert:
        marker = alert.group("marker")
        return _parse_fast_alert_match(alert, timestamp_str, marker=marker)

    generic = _GENERIC_NOTICE_RE.match(message.strip())
    if generic:
        dt = _parse_timestamp(timestamp_str)
        return {
            "message": generic.group("text").strip(),
            "timestamp": dt,
            "timestamp_desc": "Suricata notice",
            "artifact": "ids:notice:suricata",
            "artifact_long": "ids:suricata:event",
            "attributes": {"event_type": "notice", "suricata_pid": _safe_int(generic.group("pid"))},
        }
    return None


def parse_line(line: str) -> dict[str, Any] | None:
    """Parse a single Suricata line in any supported format."""
    fmt = _detect_line_format(line)
    if fmt == "empty":
        return None
    if fmt == "eve":
        return _parse_eve_line(line)
    if fmt == "opnsense":
        return _parse_opnsense_line(line)
    return _parse_fast_line(line)


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------


def find_log_files(input_path: str) -> list[Path]:
    """Resolve the input into a sorted list of Suricata log files."""
    path = Path(input_path)
    if path.is_file():
        return [path]
    if path.is_dir():
        files: set[Path] = set()
        for ext in ("*.log", "*.log.gz", "*.json", "*.json.gz"):
            files.update(path.rglob(ext))
        for candidate in path.rglob("*"):
            if candidate.is_file():
                name = candidate.name.lower()
                if "suricata" in name or "eve" in name:
                    files.add(candidate)
        if not files:
            raise SystemExit(f"error: no Suricata log files found in {input_path}")
        return sorted(files)
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
PARALLEL_MIN_BYTES = int(os.environ.get("SURICATA2TS_PARALLEL_MIN_BYTES", 256 * 1024 * 1024))


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
    """Yield ``(byte_offset, decoded_line)`` from a binary stream."""
    offset = 0
    for raw in fh:
        line = raw.rstrip(b"\r\n\x00").decode("utf-8", errors="replace")
        yield offset, line
        offset += len(raw)


def _convert_stream(
    fh: BinaryIO,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    start_offset: int = 0,
) -> tuple[int, int]:
    """Parse a binary line stream into the buffer. Returns ``(parsed, skipped)``."""
    parsed = 0
    skipped = 0
    for offset, line in _iter_lines_with_offsets(fh):
        if not line.strip():
            continue
        try:
            row = parse_line(line)
        except SuricataParseError:
            skipped += 1
            continue
        if row is None:
            skipped += 1
            continue
        buffer.append(source_file, file_hash, start_offset + offset, line, row)
        parsed += 1
    return parsed, skipped


# ---------------------------------------------------------------------------
# Parallel chunked parsing (plain files only)
# ---------------------------------------------------------------------------


def find_chunk_boundaries(path: Path, target_chunks: int) -> list[tuple[int, int]]:
    """Split a plain file into newline-aligned ``(start, end)`` byte ranges."""
    size = path.stat().st_size
    if size == 0 or target_chunks <= 1:
        return [(0, size)]
    approx = size // target_chunks
    boundaries = [0]
    with open(path, "rb") as fh:
        for i in range(1, target_chunks):
            candidate = i * approx
            if candidate <= boundaries[-1]:
                continue
            fh.seek(candidate)
            window = 4096
            found = None
            while found is None:
                chunk = fh.read(window)
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
    boundaries.append(size)
    return list(zip(boundaries, boundaries[1:]))


def _parse_chunk(
    path_str: str, start: int, end: int, source_file: str, file_hash: str
) -> tuple[bytes, int, int]:
    """Worker: parse ``[start, end)`` of a plain file, return Arrow IPC bytes."""
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
    parsed, skipped = _convert_stream(
        io.BytesIO(window), source_file, file_hash, buffer, start_offset=start
    )
    buffer.flush()
    writer_ipc.close()
    return sink.getvalue(), parsed, skipped


def _convert_file_parallel(
    path: Path, file_hash: str, buffer: _BatchBuffer, workers: int, verbose: bool
) -> tuple[int, int]:
    """Parse a large plain file across worker processes."""
    chunks = find_chunk_boundaries(path, target_chunks=workers * 4)
    if verbose:
        sys.stderr.write(f"  parallel: {len(chunks)} chunks, {workers} workers\n")
    parsed_total = 0
    skipped_total = 0
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        futures = [
            pool.submit(_parse_chunk, str(path), start, end, path.name, file_hash)
            for start, end in chunks
        ]
        for future in concurrent.futures.as_completed(futures):
            ipc_bytes, parsed, skipped = future.result()
            parsed_total += parsed
            skipped_total += skipped
            reader = pa.ipc.open_stream(ipc_bytes)
            for batch in reader:
                if batch.num_rows:
                    buffer.write_batch(batch)
    return parsed_total, skipped_total


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def convert(input_path: str, output: str, workers: int, verbose: bool) -> int:
    """Convert Suricata logs at ``input_path`` into ``output`` (.parquet)."""
    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "TraceSignal server detects the ingest parser strictly by file extension."
        )

    files = find_log_files(input_path)

    if verbose:
        sys.stderr.write(f"hashing {len(files)} input file(s)...\n")
    provenance = []
    hashes: dict[Path, str] = {}
    for path in files:
        digest, size = hash_file(path)
        hashes[path] = digest
        provenance.append({"name": path.name, "sha256": digest, "size_bytes": size})

    metadata = {
        META_FORMAT_VERSION: FORMAT_VERSION,
        META_CONVERTER_NAME: CONVERTER_NAME,
        META_CONVERTER_VERSION: CONVERTER_VERSION,
        META_ORIGINAL_FILES: json.dumps(provenance, sort_keys=True),
    }

    parsed_total = 0
    skipped_total = 0
    schema = PARQUET_EVENT_SCHEMA.with_metadata(metadata)
    with pq.ParquetWriter(output, schema, compression="zstd") as writer:
        buffer = _BatchBuffer(writer)
        for path in files:
            if verbose:
                sys.stderr.write(f"parsing {path}...\n")
            parallel = (
                path.suffix != ".gz" and workers > 1 and path.stat().st_size >= PARALLEL_MIN_BYTES
            )
            if parallel:
                parsed, skipped = _convert_file_parallel(
                    path, hashes[path], buffer, workers, verbose
                )
            else:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rb") as fh:
                    parsed, skipped = _convert_stream(fh, path.name, hashes[path], buffer)
            parsed_total += parsed
            skipped_total += skipped
        buffer.flush()

    sys.stderr.write(
        f"{CONVERTER_NAME}: wrote {parsed_total} events to {output} "
        f"({skipped_total} unparseable lines skipped)\n"
    )
    return 0 if parsed_total > 0 else 1


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Convert Suricata IDS/IPS logs (EVE JSON, fast.log, OPNsense syslog "
            "export; plain or .gz, file or directory) to a TraceSignal Parquet "
            "file for direct upload."
        )
    )
    parser.add_argument("-i", "--input", required=True, help="Suricata log file or directory")
    parser.add_argument("-o", "--output", required=True, help="output .parquet path")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=getattr(os, "process_cpu_count", os.cpu_count)() or 4,
        help="parallel parser processes for large plain files (default: CPU count)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")
    args = parser.parse_args()
    return convert(args.input, args.output, max(1, args.workers), args.verbose)


if __name__ == "__main__":
    sys.exit(main())
