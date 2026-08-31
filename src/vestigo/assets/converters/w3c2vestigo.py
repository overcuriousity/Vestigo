#!/usr/bin/env python3
"""Convert W3C Extended Log File Format logs to a Vestigo Parquet file.

Parses IIS site logs (``u_ex*.log``), http.sys ``HTTPERR`` logs, and any
other ``#Fields:``-described W3C Extended log (plain or ``.gz``, single file
or a log root searched recursively) locally, and writes one ``.parquet`` file
in the Vestigo interchange format (version 1). Upload the result to the
Vestigo web interface or ingest it with ``vestigo ingest`` — no CSV
intermediate, no server re-parse.

The ``#Fields:`` directive is authoritative and honoured **per occurrence**.
IIS rewrites it whenever the site's logging configuration changes or the
service restarts, so a single file legitimately changes its column set
mid-stream; a converter that latches the first directive shifts every later
column without saying so. Parallel chunk workers therefore start from the
directive state that was in force at their chunk's byte offset, recovered by
a cheap pre-scan, rather than from an empty one.

Per the W3C spec — and by IIS/http.sys default — the ``date`` and ``time``
fields are UTC, and are taken as such. A site configured to log local time is
off by its offset; ``--assume-tz`` overrides the assumption for the whole run
and either way the choice is written into the Parquet footer, so the shift is
a visible decision rather than an invisible one.

Forensic provenance embedded in the output:
  * per input file: sha256 + size in the Parquet footer metadata,
  * per event row: the sha256 of its original file (``file_hash``), the byte
    offset of the line within that file (``byte_offset``; offsets into the
    *decompressed* stream for ``.gz`` inputs), and the sha256 of the line
    itself (``content_hash``),
  * the converter name and version, which become the server-side parser
    identity.

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow w3c2vestigo.py ...

Usage:

    python w3c2vestigo.py -i u_ex260101.log -o iis.parquet
    python w3c2vestigo.py -i C:/inetpub/logs/LogFiles -o iis.parquet -w 8
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
        "or run this script via:  uv run --with pyarrow w3c2vestigo.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "w3c2vestigo"
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
# W3C parsing (ported from w3c2timesketch.py, converter parity)
# ---------------------------------------------------------------------------

# "#Fields: date time c-ip ..." / "#Software: ..." / "#Date: 2025-04-08 11:11:32"
_DIRECTIVE_RE = re.compile(r"^#\s*([A-Za-z][A-Za-z-]*)\s*:\s*(.*)$")

# An IPv6 literal logged by Windows may carry a scope/zone index ("%3"), which
# ipaddress rejects. The zone is kept as its own attribute rather than dropped:
# it names the interface the address was seen on.
_ZONE_RE = re.compile(r"%[0-9A-Za-z]+$")

_SOFTWARE_FLAVORS = [
    ("microsoft internet information services", "iis"),
    ("microsoft-iis", "iis"),
    ("microsoft http api", "httperr"),
    ("microsoft httpapi", "httperr"),
]

_FLAVORS = {
    "iis": {
        "timestamp_desc": "HTTP Request Time",
        "artifact": "iis:access",
        "artifact_long": "iis:access:entry",
    },
    "httperr": {
        "timestamp_desc": "HTTP Error Time",
        "artifact": "httperr:error",
        "artifact_long": "httperr:error:entry",
    },
    "w3c": {
        "timestamp_desc": "W3C Log Entry Time",
        "artifact": "w3c:extended",
        "artifact_long": "w3c:extended:entry",
    },
}

# IIS names its logs by the fields it records (u_ex = extended/UTC, u_in =
# IIS format, u_nc = NCSA, ex/in/nc = local time).
_FILENAME_FLAVORS = [
    (re.compile(r"^httperr", re.IGNORECASE), "httperr"),
    (re.compile(r"^u_(ex|in|nc|ec)\d", re.IGNORECASE), "iis"),
    (re.compile(r"^(ex|in|nc|ec)\d{6}", re.IGNORECASE), "iis"),
]

# W3C field name (lowercased) -> canonical attribute name. Names not listed
# here are kept under a sanitized form of their own W3C name, so an
# unmodelled dialect loses nothing.
_FIELD_MAP = {
    "c-ip": "src_ip",
    "s-ip": "dst_ip",
    "c-port": "src_port",
    "s-port": "dst_port",
    "cs-method": "http_method",
    "cs-uri-stem": "http_uri",
    "cs-uri-query": "http_query",
    "cs-uri": "http_uri",
    "cs-version": "http_protocol",
    "cs-username": "username",
    "cs-host": "http_host",
    "cs(host)": "http_host",
    "cs(user-agent)": "user_agent",
    "cs(referer)": "referer",
    "cs(referrer)": "referer",
    "cs(cookie)": "cookie",
    "cs(x-forwarded-for)": "x_forwarded_for",
    "x-forwarded-for": "x_forwarded_for",
    "sc-status": "status_code",
    "sc-substatus": "sub_status",
    "sc-win32-status": "win32_status",
    "sc-bytes": "response_size",
    "cs-bytes": "request_size",
    "sc(x-powered-by)": "x_powered_by",
    "time-taken": "time_taken_ms",
    "s-sitename": "site_name",
    "s-computername": "server_name",
    "s-siteid": "site_id",
    "s-reason": "reason",
    "s-queuename": "queue_name",
    "streamid": "stream_id",
    "s-event": "event",
    "s-process-type": "process_type",
    "s-user-time": "user_time",
    "s-kernel-time": "kernel_time",
}

_SANITIZE_RE = re.compile(r"[^a-z0-9]+")

# Columns carrying the row's clock, excluded from the attribute map because
# the timestamp column already holds them.
_TIME_COLUMNS = {"date", "time", "date_time", "datetime"}


def _sanitize_field(name: str) -> str:
    """Return a W3C field name as a lowercase snake_case attribute name."""
    return _SANITIZE_RE.sub("_", name.strip().lower()).strip("_") or "field"


def canonical_field(name: str) -> str:
    """Map a W3C field name to its canonical attribute name."""
    return _FIELD_MAP.get(name.strip().lower()) or _sanitize_field(name)


def normalize_ip(value: str | None) -> str:
    """Validate and canonicalize a single IPv4/IPv6 address string."""
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return ""


def _split_ip_zone(value: str) -> tuple[str, str]:
    """Split ``fe80::1%3`` into its address and scope-zone parts."""
    match = _ZONE_RE.search(value)
    if not match:
        return value, ""
    return value[: match.start()], match.group(0)[1:]


def _decode_value(column: str, value: str) -> str | None:
    """Decode one W3C field value into its attribute form.

    ``-`` is the format's "not recorded" placeholder and becomes an omitted
    attribute rather than a literal dash. IIS writes a ``+`` where the
    User-Agent contained a space; that substitution is reversed for the
    User-Agent alone, because it is the only field where a literal ``+`` is
    not also plausible payload (a URI, query, or Referer may legitimately
    contain one, and "fixing" those would corrupt the evidence).
    """
    if value in ("-", ""):
        return None
    if column == "user_agent":
        return value.replace("+", " ")
    return value


def parse_tz_offset(value: str | None) -> datetime.timezone:
    """Parse an ``--assume-tz`` value into a fixed-offset timezone.

    Accepts ``UTC``/``Z``, ``local`` (the converting machine's current
    offset), or an explicit ``+HH:MM`` / ``-HHMM`` / ``+02`` offset.
    """
    if not value or value.upper() in {"UTC", "Z"}:
        return datetime.UTC
    if value.lower() == "local":
        offset = datetime.datetime.now().astimezone().utcoffset()
        return datetime.timezone(offset or datetime.timedelta(0))
    match = re.fullmatch(r"([+-])(\d{2}):?(\d{2})?", value.strip())
    if not match:
        raise SystemExit(
            f"error: invalid --assume-tz value {value!r}: use UTC, local, or an "
            "offset such as +02:00"
        )
    sign = 1 if match.group(1) == "+" else -1
    hours = int(match.group(2))
    minutes = int(match.group(3) or 0)
    return datetime.timezone(sign * datetime.timedelta(hours=hours, minutes=minutes))


def _parse_datetime(
    values: dict[str, str], header_date: str | None, tz: datetime.timezone
) -> datetime.datetime | None:
    """Build a UTC datetime from the row's date/time fields.

    Handles the three shapes seen in the wild: separate ``date`` + ``time``
    columns (IIS, HTTPERR), a ``time`` column alone (the day then comes from
    the preceding ``#Date:`` directive), and a single combined ``date-time``
    ISO column.
    """
    combined = values.get("date-time") or values.get("datetime")
    if combined:
        try:
            dt = datetime.datetime.fromisoformat(
                re.sub(r"(\.\d{6})\d+", r"\1", combined.replace("Z", "+00:00"))
            )
        except ValueError:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=tz)
        return dt.astimezone(datetime.UTC)

    time_str = values.get("time")
    if not time_str:
        return None
    date_str = values.get("date") or header_date
    if not date_str:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(f"{date_str} {time_str}", fmt)
        except ValueError:
            continue
        return dt.replace(tzinfo=tz).astimezone(datetime.UTC)
    return None


class FileState:
    """Directive state carried across the lines of one file (or chunk).

    W3C directives are positional: a ``#Fields:`` line describes the rows
    that follow it, until the next one. A parallel chunk worker starting at
    an arbitrary byte offset must therefore be handed the state that was in
    force there; see :func:`scan_directives`.
    """

    __slots__ = (
        "fields",
        "software",
        "header_date",
        "flavor",
        "flavor_from_filename",
        "field_sets",
    )

    def __init__(self, flavor: str | None = None) -> None:
        self.fields: list[str] = []
        self.software: str = ""
        self.header_date: str | None = None
        self.flavor: str | None = flavor
        self.flavor_from_filename = flavor is not None
        self.field_sets = 0

    def apply_directive(self, name: str, value: str) -> None:
        key = name.lower()
        if key == "fields":
            self.fields = value.split()
            self.field_sets += 1
        elif key == "software":
            self.software = value.strip()
            if not self.flavor_from_filename:
                lowered = self.software.lower()
                for needle, flavor in _SOFTWARE_FLAVORS:
                    if needle in lowered:
                        self.flavor = flavor
                        break
        elif key == "date":
            parts = value.split()
            self.header_date = parts[0] if parts else None

    def apply_line(self, line: str) -> None:
        """Apply a ``#``-prefixed directive line, ignoring anything else."""
        match = _DIRECTIVE_RE.match(line.strip())
        if match:
            self.apply_directive(match.group(1), match.group(2))

    def snapshot(self) -> dict[str, Any]:
        """Return a picklable copy for handing to a chunk worker."""
        return {
            "fields": list(self.fields),
            "software": self.software,
            "header_date": self.header_date,
            "flavor": self.flavor,
            "flavor_from_filename": self.flavor_from_filename,
        }

    @classmethod
    def restore(cls, snapshot: dict[str, Any]) -> FileState:
        state = cls()
        state.fields = list(snapshot["fields"])
        state.software = snapshot["software"]
        state.header_date = snapshot["header_date"]
        state.flavor = snapshot["flavor"]
        state.flavor_from_filename = snapshot["flavor_from_filename"]
        return state


def parse_line(line: str, state: FileState, tz: datetime.timezone) -> dict[str, Any] | None:
    """Parse one line against the file's current ``#Fields:`` set.

    Directive lines mutate ``state`` and return None; so do blank lines, rows
    seen before any ``#Fields:`` directive, and rows with no usable timestamp.
    """
    stripped = line.strip()
    if not stripped:
        return None
    if stripped.startswith("#"):
        state.apply_line(stripped)
        return None
    if not state.fields:
        return None

    parts = stripped.split(" ")
    # A trailing field may legitimately contain spaces in some dialects; fold
    # the surplus back into the last column rather than dropping it.
    if len(parts) > len(state.fields):
        parts = parts[: len(state.fields) - 1] + [" ".join(parts[len(state.fields) - 1 :])]

    raw = {name.lower(): value for name, value in zip(state.fields, parts, strict=False)}
    dt = _parse_datetime(raw, state.header_date, tz)
    if dt is None:
        return None

    flavor = state.flavor or "w3c"
    config = _FLAVORS.get(flavor, _FLAVORS["w3c"])

    attributes: dict[str, str] = {"log_type": flavor}
    if state.software:
        attributes["software"] = state.software

    for field, value in zip(state.fields, parts, strict=False):
        column = canonical_field(field)
        if column in _TIME_COLUMNS:
            continue
        if column in ("src_ip", "dst_ip"):
            if value == "-":
                continue
            address, zone = _split_ip_zone(value)
            attributes[column] = normalize_ip(address) or address
            if zone:
                attributes[f"{column}_zone"] = zone
            continue
        decoded = _decode_value(column, value)
        if decoded is not None:
            attributes[column] = decoded

    return {
        "message": stripped,
        "timestamp": dt,
        "timestamp_desc": config["timestamp_desc"],
        "artifact": config["artifact"],
        "artifact_long": config["artifact_long"],
        "attributes": attributes,
    }


def _parse_since_until(value: str | None) -> datetime.datetime | None:
    """Parse an ISO 8601 ``--since``/``--until`` value to a UTC-aware datetime."""
    if not value:
        return None
    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


# ---------------------------------------------------------------------------
# Input discovery and directive pre-scan
# ---------------------------------------------------------------------------

_LOG_GLOBS = ["*.log", "*.log.gz", "*.LOG", "*.txt", "u_*", "httperr*"]


def _open_log(path: Path) -> Any:
    """Open a plain or gzipped log file for reading text lines."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8-sig", errors="replace")
    return open(path, encoding="utf-8-sig", errors="replace")


def detect_flavor_from_name(filename: str) -> str | None:
    """Return the W3C flavor implied by a filename, if any."""
    name = Path(filename).name
    for pattern, flavor in _FILENAME_FLAVORS:
        if pattern.search(name):
            return flavor
    return None


def is_w3c_log(path: Path) -> bool:
    """Return True when the file's opening lines declare a ``#Fields:`` set."""
    try:
        with _open_log(path) as fh:
            for index, line in enumerate(fh):
                if line.startswith("#"):
                    match = _DIRECTIVE_RE.match(line.strip())
                    if match and match.group(1).lower() == "fields":
                        return True
                if index >= 32:
                    break
    except OSError:
        return False
    return False


def find_log_files(input_path: str) -> list[Path]:
    """Resolve the input into W3C log files.

    A directory is searched recursively: IIS keeps one directory per site
    (``W3SVC1/``, ``W3SVC2/``) under a single log root, and HTTPERR sits
    beside them, so the useful thing to point this at is the root.
    """
    path = Path(input_path)
    if path.is_file():
        return [path]
    if path.is_dir():
        seen: set[Path] = set()
        for pattern in _LOG_GLOBS:
            for match in path.rglob(pattern):
                if match.is_file():
                    seen.add(match)
        found = sorted(p for p in seen if is_w3c_log(p))
        if not found:
            raise SystemExit(
                f"error: no W3C Extended log files found in {input_path} "
                "(no file carried a '#Fields:' directive in its first lines)"
            )
        return found
    raise SystemExit(f"error: input path not found: {input_path}")


def scan_directives(path: Path) -> list[tuple[int, str]]:
    """Return every ``#`` directive line in a plain file, with its byte offset.

    Scanned with :meth:`bytes.find` over 1 MiB blocks rather than by decoding
    lines, so the cost is a sequential read and no per-line work. The result
    lets a chunk worker be handed the ``#Fields:`` set that was in force at
    its start offset — without it, every row after the first directive change
    in a parallel run would be parsed against the wrong columns.
    """
    directives: list[tuple[int, str]] = []
    with open(path, "rb") as fh:
        block_start = 0
        carry = b""
        while True:
            block = fh.read(1024 * 1024)
            if not block:
                break
            data = carry + block
            data_start = block_start - len(carry)
            search_from = 0
            while True:
                # A directive is a '#' at the start of a line: offset 0, or
                # immediately after a newline.
                index = data.find(b"#", search_from)
                if index < 0:
                    break
                if index == 0 and data_start == 0 or (index > 0 and data[index - 1] == 0x0A):
                    end = data.find(b"\n", index)
                    if end < 0:
                        break  # incomplete; picked up with the next block
                    directives.append(
                        (
                            data_start + index,
                            data[index:end].decode("utf-8", errors="replace").rstrip("\r"),
                        )
                    )
                    search_from = end + 1
                    continue
                search_from = index + 1
            # Keep the tail from the last newline so a directive split across
            # the block boundary is still seen whole.
            last_nl = data.rfind(b"\n")
            carry = data[last_nl + 1 :] if last_nl >= 0 else data
            block_start += len(block)
    return directives


def state_at_offset(
    directives: list[tuple[int, str]], offset: int, flavor: str | None
) -> FileState:
    """Build the directive state in force at ``offset``."""
    state = FileState(flavor=flavor)
    for position, line in directives:
        if position >= offset:
            break
        state.apply_line(line)
    return state


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
PARALLEL_MIN_BYTES = int(os.environ.get("W3C2VESTIGO_PARALLEL_MIN_BYTES", 256 * 1024 * 1024))
# No single parallel chunk may exceed this many bytes, so per-worker memory
# stays bounded on huge files.
MAX_CHUNK_BYTES = int(os.environ.get("W3C2VESTIGO_MAX_CHUNK_BYTES", 128 * 1024 * 1024))
# Default cap on parallel workers; high core counts otherwise multiply peak RAM.
DEFAULT_MAX_WORKERS = int(os.environ.get("W3C2VESTIGO_DEFAULT_WORKERS", 4))


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
    state: FileState,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    tz: datetime.timezone,
    start_offset: int = 0,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[int, int, int]:
    """Parse a binary line stream into the buffer.

    ``state`` is mutated as directives are encountered, so it also reports the
    field-set count back to the caller.

    Returns ``(parsed, skipped, skipped_by_time)`` line counts.
    """
    parsed = 0
    skipped = 0
    skipped_by_time = 0
    for offset, line in _iter_lines_with_offsets(fh):
        stripped = line.strip()
        if not stripped:
            continue
        row = parse_line(line, state, tz)
        if row is None:
            # Directive lines are structure, not unparseable data.
            if not stripped.startswith("#"):
                skipped += 1
            continue
        ts = row["timestamp"]
        if since_dt is not None and ts < since_dt:
            skipped_by_time += 1
            continue
        if until_dt is not None and ts > until_dt:
            skipped_by_time += 1
            continue
        buffer.append(source_file, file_hash, start_offset + offset, line, row)
        parsed += 1
    return parsed, skipped, skipped_by_time


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
    state_snapshot: dict[str, Any],
    source_file: str,
    file_hash: str,
    tz: datetime.timezone,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[bytes, int, int, int]:
    """Worker: parse ``[start, end)`` of a plain file, return Arrow IPC bytes.

    ``state_snapshot`` carries the ``#Fields:``/``#Software:``/``#Date:``
    state that was in force at ``start``; a worker that began with an empty
    one would drop every row up to the next directive and misattribute the
    rest. Top-level so it pickles under the spawn start method.
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
    parsed, skipped, skipped_by_time = _convert_stream(
        io.BytesIO(window),
        FileState.restore(state_snapshot),
        source_file,
        file_hash,
        buffer,
        tz,
        start_offset=start,
        since_dt=since_dt,
        until_dt=until_dt,
    )
    buffer.flush()
    writer_ipc.close()
    return sink.getvalue(), parsed, skipped, skipped_by_time


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
    flavor: str | None,
    file_hash: str,
    buffer: _BatchBuffer,
    workers: int,
    verbose: bool,
    tz: datetime.timezone,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[int, int, int]:
    """Parse a large plain file across worker processes."""
    chunks = find_chunk_boundaries(path, target_chunks=workers * 4)
    directives = scan_directives(path)
    if verbose:
        sys.stderr.write(
            f"  parallel: {len(chunks)} chunks, {workers} workers, "
            f"{len(directives)} directive line(s)\n"
        )
    _warn_if_ram_tight(workers)
    parsed_total = 0
    skipped_total = 0
    skipped_by_time_total = 0
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
                        state_at_offset(directives, start, flavor).snapshot(),
                        path.name,
                        file_hash,
                        tz,
                        since_dt,
                        until_dt,
                    )
                )
                return

        for _ in range(workers * 2):
            _submit_next()
        while pending:
            ipc_bytes, parsed, skipped, skipped_by_time = pending.popleft().result()
            _submit_next()
            parsed_total += parsed
            skipped_total += skipped
            skipped_by_time_total += skipped_by_time
            reader = pa.ipc.open_stream(ipc_bytes)
            for batch in reader:
                if batch.num_rows:
                    buffer.write_batch(batch)
    return parsed_total, skipped_total, skipped_by_time_total


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


def convert(
    input_path: str,
    output: str,
    workers: int,
    verbose: bool,
    split: str | None = None,
    since: str | None = None,
    until: str | None = None,
    assume_tz: str | None = None,
) -> int:
    """Convert W3C Extended logs at ``input_path`` into ``output`` (.parquet)."""
    import json

    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "Vestigo server detects the ingest parser strictly by file extension."
        )

    since_dt = _parse_since_until(since)
    until_dt = _parse_since_until(until)
    tz = parse_tz_offset(assume_tz)

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

    parsed_total = 0
    skipped_total = 0
    skipped_by_time_total = 0
    field_sets_total = 0
    flavors: dict[str, int] = {}

    metadata = {
        META_FORMAT_VERSION: FORMAT_VERSION,
        META_CONVERTER_NAME: CONVERTER_NAME,
        META_CONVERTER_VERSION: CONVERTER_VERSION,
        META_ORIGINAL_FILES: json.dumps(provenance, sort_keys=True),
        META_CONVERTED_AT: datetime.datetime.now(datetime.UTC).isoformat(),
        META_TIMEZONE_ASSUMPTION: (
            "W3C date/time fields carry no offset and are taken as "
            f"{assume_tz or 'UTC'} (the format's and IIS's own default is UTC)"
        ),
    }

    schema = PARQUET_EVENT_SCHEMA.with_metadata(metadata)
    with pq.ParquetWriter(write_target, schema, compression="zstd") as writer:
        buffer = _BatchBuffer(writer)
        for path in files:
            flavor = detect_flavor_from_name(path.name)
            if verbose:
                sys.stderr.write(f"parsing {path} as {flavor or 'w3c (sniffed)'}...\n")
            parallel = (
                path.suffix != ".gz" and workers > 1 and path.stat().st_size >= PARALLEL_MIN_BYTES
            )
            if parallel:
                parsed, skipped, skipped_by_time = _convert_file_parallel(
                    path, flavor, hashes[path], buffer, workers, verbose, tz, since_dt, until_dt
                )
                # The pre-scan already enumerated the file's directives.
                state = state_at_offset(scan_directives(path), path.stat().st_size + 1, flavor)
                field_sets_total += state.field_sets
                flavors[state.flavor or "w3c"] = flavors.get(state.flavor or "w3c", 0) + 1
            else:
                state = FileState(flavor=flavor)
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rb") as fh:
                    parsed, skipped, skipped_by_time = _convert_stream(
                        fh,
                        state,
                        path.name,
                        hashes[path],
                        buffer,
                        tz,
                        since_dt=since_dt,
                        until_dt=until_dt,
                    )
                field_sets_total += state.field_sets
                flavors[state.flavor or "w3c"] = flavors.get(state.flavor or "w3c", 0) + 1
            if verbose and state.field_sets > 1:
                sys.stderr.write(
                    f"  {path.name}: {state.field_sets} '#Fields:' directives "
                    "(column set changed mid-file; each block parsed against its own)\n"
                )
            parsed_total += parsed
            skipped_total += skipped
            skipped_by_time_total += skipped_by_time
        buffer.flush()
        writer.add_key_value_metadata(
            {
                META_PARSE_DECISIONS: json.dumps(
                    {
                        "flavors": flavors,
                        "field_directives": field_sets_total,
                        "since": since,
                        "until": until,
                    },
                    sort_keys=True,
                ),
                META_ROW_COUNTS: json.dumps(
                    {
                        "parsed": parsed_total,
                        "skipped_malformed": skipped_total,
                        "skipped_by_time": skipped_by_time_total,
                    }
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
    return 0 if parsed_total > 0 else 1


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Convert W3C Extended Log File Format logs — IIS site logs, http.sys "
            "HTTPERR logs, and any other '#Fields:'-described W3C log (plain or "
            ".gz, file or directory) — to a Vestigo Parquet file for direct upload."
        )
    )
    parser.add_argument(
        "-i", "--input", required=True, help="log file or directory (searched recursively)"
    )
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
        "--assume-tz",
        dest="assume_tz",
        metavar="TZ",
        help="Timezone the date/time fields were written in: UTC (default, and "
        "the W3C/IIS default), local, or an explicit offset such as +02:00. "
        "The choice is recorded in the Parquet footer.",
    )
    parser.add_argument(
        "--since",
        help="Only entries at or after this ISO 8601 timestamp (e.g. 2026-01-01T00:00:00Z).",
    )
    parser.add_argument(
        "--until",
        help="Only entries at or before this ISO 8601 timestamp (e.g. 2026-01-01T23:59:59Z).",
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
        assume_tz=args.assume_tz,
    )


if __name__ == "__main__":
    sys.exit(main())
