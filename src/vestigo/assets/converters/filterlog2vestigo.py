#!/usr/bin/env python3
"""Convert pfSense/OPNsense filterlog firewall logs to a Vestigo Parquet file.

Parses raw ``filterlog`` firewall log entries (plain or ``.gz``, single file or
a directory of rotated logs) locally and writes one ``.parquet`` file in the
Vestigo interchange format (version 1). Upload the result to the
Vestigo web interface or ingest it with ``vestigo ingest`` — no CSV
intermediate, no server re-parse.

Supports the de-facto standard FreeBSD ``pf`` filterlog format for IPv4/IPv6,
TCP/UDP/ICMP, and the common syslog variants produced by pfSense and OPNsense.

Forensic provenance embedded in the output:
  * per input file: sha256 + size in the Parquet footer metadata,
  * per event row: the sha256 of its original file (``file_hash``), the byte
    offset of the line within that file (``byte_offset``; offsets into the
    *decompressed* stream for ``.gz`` inputs), and the sha256 of the line
    itself (``content_hash``),
  * the converter name and version, which become the server-side parser
    identity.

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow filterlog2vestigo.py ...

Usage:

    python filterlog2vestigo.py -i filter.log -o filter.parquet
    python filterlog2vestigo.py -i /var/log/filter/ -o filterlog.parquet -w 8
"""

from __future__ import annotations

import collections
import concurrent.futures
import datetime
import gzip
import hashlib
import io
import ipaddress
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
        "error: pyarrow is required to write Vestigo Parquet files.\n"
        "Install it with:  pip install pyarrow\n"
        "or run this script via:  uv run --with pyarrow filterlog2vestigo.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "filterlog2vestigo"
CONVERTER_VERSION = "1.2.0"

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
# filterlog line parsing (ported from filterlog2timesketch.py, converter parity)
# ---------------------------------------------------------------------------

# Regexes for the leading syslog/export timestamp.
_ISO_TIMESTAMP_RE = re.compile(
    r"^(?:<\d+>\s*)?(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?"
    r"(?:[+-]\d{2}:?\d{2})?)"
)
_BSD_TIMESTAMP_RE = re.compile(r"^(?:<\d+>\s*)?([A-Z][a-z]{2}\s+\d{1,2}\s+\d{2}:\d{2}:\d{2})")

# ICMP type -> ordered list of field names that follow the ICMP type field.
_ICMP_FIELD_MAP: dict[str, list[str]] = {
    "request": ["icmp_id", "icmp_sequence"],
    "reply": ["icmp_id", "icmp_sequence"],
    "unreachproto": ["icmp_destination_ip", "icmp_protocol_id"],
    "unreachport": ["icmp_destination_ip", "icmp_protocol_id", "icmp_port"],
    "needfrag": ["icmp_destination_ip", "icmp_mtu"],
    "tstamp": ["icmp_id", "icmp_sequence"],
    "tstampreply": ["icmp_id", "icmp_sequence", "icmp_otime", "icmp_rtime", "icmp_ttime"],
}

# ICMP types that only carry a free-form description after the type field.
_ICMP_DESCRIPTION_TYPES = {"unreach", "timexceed", "paramprob", "redirect", "maskreply"}


def normalize_ip(value: str | None) -> str:
    """Validate and canonicalize a single IPv4/IPv6 address string."""
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return ""


def _safe_int(value: str) -> int | None:
    """Return ``value`` as int, or None if it is empty/non-numeric."""
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _add_field(attrs: dict[str, Any], key: str, value: Any) -> None:
    """Add an attribute if it has a non-empty value."""
    if value is not None and value != "":
        attrs[key] = value


def _extract_payload(line: str) -> str:
    """Return the CSV payload of a filterlog line.

    Handles:
    - OPNsense export: ``<ts>\t<level>\tfilterlog\t <csv>``
    - BSD syslog:    ``<ts> <host> filterlog: <csv>``
    - Bare CSV:      ``<csv>``
    """
    marker = "filterlog"
    idx = line.find(marker)
    if idx == -1:
        return line.strip()
    payload = line[idx + len(marker) :]
    return payload.lstrip(" \t:").rstrip("\n\r")


def _parse_syslog_timestamp(prefix: str, year: int | None) -> datetime.datetime | None:
    """Parse a syslog/export timestamp prefix into a UTC-aware datetime."""
    prefix = prefix.strip()

    iso_match = _ISO_TIMESTAMP_RE.match(prefix)
    if iso_match:
        ts = iso_match.group(1)
        if " " in ts:
            ts = ts.replace(" ", "T")
        ts = ts.replace("Z", "+00:00")
        try:
            dt = datetime.datetime.fromisoformat(ts)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.UTC)
            return dt.astimezone(datetime.UTC)
        except ValueError:
            return None

    bsd_match = _BSD_TIMESTAMP_RE.match(prefix)
    if bsd_match:
        ts = bsd_match.group(1)
        try:
            dt = datetime.datetime.strptime(ts, "%b %d %H:%M:%S")
        except ValueError:
            return None
        if year is None:
            year = datetime.datetime.now(datetime.UTC).year
        try:
            dt = dt.replace(year=year, tzinfo=datetime.UTC)
        except ValueError:
            return None
        return dt

    return None


def _timestamp_from_line(line: str, year: int | None) -> datetime.datetime | None:
    """Extract the event timestamp from the syslog/export prefix, if present."""
    marker = "filterlog"
    idx = line.find(marker)
    if idx == -1:
        return None
    return _parse_syslog_timestamp(line[:idx], year)


def _map_icmp_fields(fields: list[str], start: int) -> dict[str, Any]:
    """Map ICMP-specific fields starting at ``start`` (the icmp_type position)."""
    result: dict[str, Any] = {}
    if start >= len(fields):
        return result

    icmp_type = fields[start]
    _add_field(result, "icmp_type", icmp_type)

    field_names = _ICMP_FIELD_MAP.get(icmp_type)
    if field_names is None and icmp_type in _ICMP_DESCRIPTION_TYPES:
        field_names = ["icmp_description"]

    if field_names is None:
        trailing = fields[start + 1 :]
        if trailing:
            result["icmp_raw_fields"] = ",".join(trailing)
        return result

    for offset, name in enumerate(field_names, start=1):
        idx = start + offset
        if idx < len(fields):
            value = fields[idx]
            if any(s in name for s in ("_id", "_sequence", "_mtu", "_port", "_protocol_id")):
                int_value = _safe_int(value)
                _add_field(result, name, int_value if int_value is not None else value)
            else:
                _add_field(result, name, value)

    return result


def _build_message(attrs: dict[str, Any]) -> str:
    """Build a concise human-readable summary of a filterlog row."""
    action = attrs.get("action", "unknown")
    interface = attrs.get("interface", "")
    protocol = attrs.get("protocol", "")
    protocol_id = attrs.get("protocol_id")
    source_ip = attrs.get("src_ip", "")
    destination_ip = attrs.get("dst_ip", "")
    source_port = attrs.get("src_port")
    destination_port = attrs.get("dst_port")
    rule_number = attrs.get("rule_number", "")
    rule_uuid = attrs.get("rule_uuid", "")
    icmp_type = attrs.get("icmp_type", "")

    proto = protocol or (str(protocol_id) if protocol_id is not None else "unknown")

    src = source_ip
    if source_port is not None:
        src = f"{src}:{source_port}"
    dst = destination_ip
    if destination_port is not None:
        dst = f"{dst}:{destination_port}"

    parts = [f"firewall {action} {proto}"]
    if src and dst:
        parts.append(f"{src} -> {dst}")
    elif src or dst:
        parts.append(f"{src}{dst}")
    if interface:
        parts.append(f"on {interface}")
    if icmp_type:
        parts.append(f"icmp_type={icmp_type}")

    rule_parts = []
    if rule_number:
        rule_parts.append(str(rule_number))
    if rule_uuid:
        rule_parts.append(str(rule_uuid))
    if rule_parts:
        parts.append(f"(rule {' / '.join(rule_parts)})")

    return " ".join(parts)


def parse_filterlog_csv(
    csv_payload: str, event_dt: datetime.datetime | None
) -> dict[str, Any] | None:
    """Parse a single filterlog CSV payload into an event row dict."""
    fields = csv_payload.split(",")
    if len(fields) < 9:
        return None

    (
        rule_number,
        sub_rule_number,
        anchor,
        rule_uuid,
        interface,
        reason,
        action,
        direction,
        ip_version,
    ) = fields[:9]

    if not action or not ip_version:
        return None

    attrs: dict[str, Any] = {
        "rule_number": rule_number,
        "sub_rule_number": sub_rule_number,
        "anchor": anchor,
        "rule_uuid": rule_uuid,
        "interface": interface,
        "reason": reason,
        "action": action,
        "direction": direction,
        "ip_version": ip_version,
    }

    source_ip = ""
    destination_ip = ""

    if ip_version == "4":
        ipv4_fields = {
            "tos": 9,
            "ecn": 10,
            "ttl": 11,
            "ip_id": 12,
            "fragment_offset": 13,
            "ip_flags": 14,
            "protocol_id": 15,
            "protocol": 16,
            "packet_length": 17,
            "src_ip": 18,
            "dst_ip": 19,
        }
        for name, idx in ipv4_fields.items():
            if idx < len(fields):
                value = fields[idx]
                if name in {"ttl", "ip_id", "fragment_offset", "protocol_id", "packet_length"}:
                    int_value = _safe_int(value)
                    _add_field(attrs, name, int_value if int_value is not None else value)
                else:
                    _add_field(attrs, name, value)

        protocol = (attrs.get("protocol") or "").lower()
        source_ip = attrs.get("src_ip", "")
        destination_ip = attrs.get("dst_ip", "")

        if protocol in ("tcp", "udp"):
            if len(fields) > 20:
                _add_field(attrs, "src_port", _safe_int(fields[20]))
            if len(fields) > 21:
                _add_field(attrs, "dst_port", _safe_int(fields[21]))
            if len(fields) > 22:
                _add_field(attrs, "data_length", _safe_int(fields[22]))
            if protocol == "tcp" and len(fields) > 23:
                tcp_names = [
                    "tcp_flags",
                    "tcp_sequence",
                    "tcp_ack",
                    "tcp_window",
                    "tcp_urg",
                    "tcp_options",
                ]
                for offset, name in enumerate(tcp_names, start=23):
                    if offset >= len(fields):
                        break
                    value = fields[offset]
                    if name in {"tcp_sequence", "tcp_ack", "tcp_window"}:
                        _add_field(attrs, name, _safe_int(value))
                    else:
                        _add_field(attrs, name, value)
        elif protocol == "icmp":
            attrs.update(_map_icmp_fields(fields, 20))
        else:
            if len(fields) > 20:
                attrs["raw_fields"] = ",".join(fields[20:])

    elif ip_version == "6":
        ipv6_fields = {
            "class": 9,
            "flow_label": 10,
            "hop_limit": 11,
            "protocol": 12,
            "protocol_id": 13,
            "packet_length": 14,
            "src_ip": 15,
            "dst_ip": 16,
        }
        for name, idx in ipv6_fields.items():
            if idx < len(fields):
                value = fields[idx]
                if name in {"flow_label", "hop_limit", "protocol_id", "packet_length"}:
                    int_value = _safe_int(value)
                    _add_field(attrs, name, int_value if int_value is not None else value)
                else:
                    _add_field(attrs, name, value)

        protocol = (attrs.get("protocol") or "").lower()
        source_ip = attrs.get("src_ip", "")
        destination_ip = attrs.get("dst_ip", "")

        if protocol in ("tcp", "udp"):
            if len(fields) > 17:
                _add_field(attrs, "src_port", _safe_int(fields[17]))
            if len(fields) > 18:
                _add_field(attrs, "dst_port", _safe_int(fields[18]))
            if len(fields) > 19:
                _add_field(attrs, "data_length", _safe_int(fields[19]))
            if protocol == "tcp" and len(fields) > 20:
                tcp_names = [
                    "tcp_flags",
                    "tcp_sequence",
                    "tcp_ack",
                    "tcp_window",
                    "tcp_urg",
                    "tcp_options",
                ]
                for offset, name in enumerate(tcp_names, start=20):
                    if offset >= len(fields):
                        break
                    value = fields[offset]
                    if name in {"tcp_sequence", "tcp_ack", "tcp_window"}:
                        _add_field(attrs, name, _safe_int(value))
                    else:
                        _add_field(attrs, name, value)
        elif protocol == "icmp":
            attrs.update(_map_icmp_fields(fields, 17))
        else:
            if len(fields) > 17:
                attrs["raw_fields"] = ",".join(fields[17:])
    else:
        attrs["raw_fields"] = ",".join(fields[9:])

    attrs["src_ip"] = normalize_ip(source_ip)
    attrs["dst_ip"] = normalize_ip(destination_ip)

    message = _build_message(attrs)
    artifact_action = (action or "unknown").lower()

    return {
        "message": message,
        "timestamp": event_dt,
        "timestamp_desc": "Firewall Log Event Time",
        "artifact": f"firewall:filterlog:{artifact_action}",
        "artifact_long": "firewall:pf:filterlog",
        "attributes": attrs,
    }


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------


def find_log_files(input_path: str) -> list[Path]:
    """Resolve the input into a sorted list of filterlog files."""
    path = Path(input_path)
    if path.is_file():
        return [path]
    if path.is_dir():
        files: set[Path] = set()
        for ext in ("*.log", "*.log.gz"):
            files.update(path.rglob(ext))
        for candidate in path.rglob("*"):
            if candidate.is_file() and "filterlog" in candidate.name.lower():
                files.add(candidate)
        if not files:
            raise SystemExit(f"error: no filterlog files found in {input_path}")
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
PARALLEL_MIN_BYTES = int(os.environ.get("FILTERLOG2TS_PARALLEL_MIN_BYTES", 256 * 1024 * 1024))
# No single parallel chunk may exceed this many bytes, so per-worker memory
# stays bounded on huge files.
MAX_CHUNK_BYTES = int(os.environ.get("FILTERLOG2TS_MAX_CHUNK_BYTES", 128 * 1024 * 1024))
# Default cap on parallel workers; high core counts otherwise multiply peak RAM.
DEFAULT_MAX_WORKERS = int(os.environ.get("FILTERLOG2TS_DEFAULT_WORKERS", 4))


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
        line = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
        yield offset, line
        offset += len(raw)


def _convert_stream(
    fh: BinaryIO,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    year: int | None,
    start_offset: int = 0,
) -> tuple[int, int]:
    """Parse a binary line stream into the buffer. Returns ``(parsed, skipped)``."""
    parsed = 0
    skipped = 0
    for offset, line in _iter_lines_with_offsets(fh):
        if not line.strip():
            continue
        payload = _extract_payload(line)
        if not payload:
            skipped += 1
            continue
        event_dt = _timestamp_from_line(line, year)
        row = parse_filterlog_csv(payload, event_dt)
        if row is None:
            skipped += 1
            continue
        buffer.append(source_file, file_hash, start_offset + offset, line, row)
        parsed += 1
    return parsed, skipped


# ---------------------------------------------------------------------------
# Parallel chunked parsing (plain files only)
# ---------------------------------------------------------------------------


def find_chunk_boundaries(
    path: Path, target_chunks: int, max_chunk_bytes: int = MAX_CHUNK_BYTES
) -> list[tuple[int, int]]:
    """Split a plain file into newline-aligned ``(start, end)`` byte ranges.

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
    path_str: str, start: int, end: int, source_file: str, file_hash: str, year: int | None
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
        io.BytesIO(window), source_file, file_hash, buffer, year, start_offset=start
    )
    buffer.flush()
    writer_ipc.close()
    return sink.getvalue(), parsed, skipped


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
    path: Path, file_hash: str, buffer: _BatchBuffer, workers: int, year: int | None, verbose: bool
) -> tuple[int, int]:
    """Parse a large plain file across worker processes."""
    chunks = find_chunk_boundaries(path, target_chunks=workers * 4)
    if verbose:
        sys.stderr.write(f"  parallel: {len(chunks)} chunks, {workers} workers\n")
    _warn_if_ram_tight(workers)
    parsed_total = 0
    skipped_total = 0
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
                    pool.submit(_parse_chunk, str(path), start, end, path.name, file_hash, year)
                )
                return

        for _ in range(workers * 2):
            _submit_next()
        while pending:
            ipc_bytes, parsed, skipped = pending.popleft().result()
            _submit_next()
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


# ---------------------------------------------------------------------------
# Output splitting (ported from the 2timesketch converter suite's --split)
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


def convert(
    input_path: str,
    output: str,
    workers: int,
    verbose: bool,
    year: int | None = None,
    split: str | None = None,
) -> int:
    """Convert filterlog entries at ``input_path`` into ``output`` (.parquet)."""
    import json

    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "Vestigo server detects the ingest parser strictly by file extension."
        )

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
    with pq.ParquetWriter(write_target, schema, compression="zstd") as writer:
        buffer = _BatchBuffer(writer)
        for path in files:
            if verbose:
                sys.stderr.write(f"parsing {path}...\n")
            parallel = (
                path.suffix != ".gz" and workers > 1 and path.stat().st_size >= PARALLEL_MIN_BYTES
            )
            if parallel:
                parsed, skipped = _convert_file_parallel(
                    path, hashes[path], buffer, workers, year, verbose
                )
            else:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rb") as fh:
                    parsed, skipped = _convert_stream(fh, path.name, hashes[path], buffer, year)
            parsed_total += parsed
            skipped_total += skipped
        buffer.flush()

    if split_spec is not None:
        try:
            parts = split_parquet(Path(write_target), output, split_spec, verbose)
        finally:
            Path(write_target).unlink(missing_ok=True)
        sys.stderr.write(
            f"{CONVERTER_NAME}: wrote {parsed_total} events to {len(parts)} part "
            f"file(s) [{parts[0].name} .. {parts[-1].name}] "
            f"({skipped_total} unparseable lines skipped)\n"
        )
    else:
        sys.stderr.write(
            f"{CONVERTER_NAME}: wrote {parsed_total} events to {output} "
            f"({skipped_total} unparseable lines skipped)\n"
        )
    return 0 if parsed_total > 0 else 1


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Convert pfSense/OPNsense filterlog entries (plain or .gz, file or "
            "directory) to a Vestigo Parquet file for direct upload."
        )
    )
    parser.add_argument("-i", "--input", required=True, help="filterlog file or directory")
    parser.add_argument("-o", "--output", required=True, help="output .parquet path")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=min(getattr(os, "process_cpu_count", os.cpu_count)() or 4, DEFAULT_MAX_WORKERS),
        help="parallel parser processes for large plain files (default: min(CPU count, %(default)s))",
    )
    parser.add_argument(
        "--year",
        type=int,
        help="year to assume for BSD-style syslog timestamps that omit the year "
        "(default: current year)",
    )
    parser.add_argument(
        "--split",
        metavar="N|SIZE",
        help="split the output into multiple .parquet files: N = N parts with "
        "an equal number of rows (e.g. 4); SIZE = rotate to a new part once "
        "it reaches SIZE, with a K/M/G suffix meaning KiB/MiB/GiB (e.g. "
        "512M). Parts are named <name>.partNNN.parquet.",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")
    args = parser.parse_args()
    return convert(
        args.input, args.output, max(1, args.workers), args.verbose, args.year, split=args.split
    )


if __name__ == "__main__":
    sys.exit(main())
