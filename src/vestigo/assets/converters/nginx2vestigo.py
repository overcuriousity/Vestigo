#!/usr/bin/env python3
"""Convert nginx access/error/redirect logs to a Vestigo Parquet file.

Parses raw nginx logs (plain or ``.gz``, single file or a directory of
rotated logs) locally and writes one ``.parquet`` file in the Vestigo
interchange format (version 1). Upload the result to the Vestigo web
interface or ingest it with ``vestigo ingest`` — no CSV intermediate, no server
re-parse.

Forensic provenance embedded in the output:
  * per input file: sha256 + size in the Parquet footer metadata,
  * per event row: the sha256 of its original file (``file_hash``), the byte
    offset of the line within that file (``byte_offset``; offsets into the
    *decompressed* stream for ``.gz`` inputs), and the sha256 of the line
    itself (``content_hash``),
  * the converter name and version, which become the server-side parser
    identity.

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow nginx2vestigo.py ...

Usage:

    python nginx2vestigo.py -i access.log -o access.parquet
    python nginx2vestigo.py -i /var/log/nginx/ -o nginx.parquet -w 8
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
        "or run this script via:  uv run --with pyarrow nginx2vestigo.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "nginx2vestigo"
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
# nginx line parsing (ported from nginx2timesketch.py, converter parity)
# ---------------------------------------------------------------------------

# Combined log format used for access and redirect logs.
_ACCESS_LOG_RE = re.compile(
    r'(\S+) (\S*) (\S*) \[([^\]]+)\] "([^"]*)" (\d+) (\S+) "([^"]*)" "([^"]*)"'
    r'(?:\s+"([^"]*)")?'
)

# Error log format: "2026/06/25 09:46:41 [error] 1234#1234: *1 message..."
_ERROR_LOG_RE = re.compile(
    r"^(\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] (\d+)#(\d+): \*(\d+) (.*)$"
)

_CLIENT_IP_RE = re.compile(r"client:\s+(\S+)")

# Order matters for name-based detection: "redirect" must be checked before
# "access", because "access.log" is a substring of "redirect-access.log".
_LOG_TYPES = {
    "redirect": {
        "patterns": ["redirect-access.log*"],
        "timestamp_desc": "Redirect Request Time",
        "artifact": "nginx:redirect",
        "artifact_long": "web:redirect:request",
    },
    "access": {
        "patterns": ["access.log*"],
        "timestamp_desc": "HTTP Request Time",
        "artifact": "nginx:access",
        "artifact_long": "web:access:request",
    },
    "error": {
        "patterns": ["error.log*"],
        "timestamp_desc": "Error Event Time",
        "artifact": "nginx:error",
        "artifact_long": "web:error:log",
    },
}


def normalize_ip(value: str | None) -> str:
    """Validate and canonicalize a single IPv4/IPv6 address string."""
    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return ""


def _parse_access_line(line: str, log_type: str) -> dict[str, Any] | None:
    """Parse an access/redirect log line into an event row dict."""
    match = _ACCESS_LOG_RE.match(line)
    if not match:
        return None

    groups = match.groups()
    ip = groups[0]
    remote_ident = groups[1] if groups[1] != "-" else None
    remote_user = groups[2] if groups[2] != "-" else None
    timestamp_str = groups[3]
    request = groups[4]
    status = groups[5]
    size = groups[6]
    referer = groups[7] if groups[7] != "-" else None
    user_agent = groups[8]
    additional = groups[9] if len(groups) > 9 and groups[9] else None

    request_parts = request.split(" ")
    method = request_parts[0] if len(request_parts) > 0 else None
    uri = request_parts[1] if len(request_parts) > 1 else None
    protocol = request_parts[2] if len(request_parts) > 2 else None

    try:
        dt = datetime.datetime.strptime(timestamp_str, "%d/%b/%Y:%H:%M:%S %z")
    except ValueError:
        return None

    config = _LOG_TYPES[log_type]
    return {
        "message": line.strip(),
        "timestamp": dt.astimezone(datetime.UTC),
        "timestamp_desc": config["timestamp_desc"],
        "artifact": config["artifact"],
        "artifact_long": config["artifact_long"],
        "attributes": {
            "log_type": log_type,
            "src_ip": normalize_ip(ip),
            "remote_ident": remote_ident,
            "remote_user": remote_user,
            "http_method": method,
            "http_uri": uri,
            "http_protocol": protocol,
            "http_request_full": request,
            "status_code": status,
            "response_size": size if size.isdigit() else "0",
            "referer": referer,
            "user_agent": user_agent,
            "additional_field": additional,
        },
    }


def _parse_error_line(line: str) -> dict[str, Any] | None:
    """Parse an nginx error log line into an event row dict."""
    match = _ERROR_LOG_RE.match(line)
    if not match:
        return None

    timestamp_str, level, pid, tid, conn_id, message = match.groups()
    try:
        dt = datetime.datetime.strptime(timestamp_str, "%Y/%m/%d %H:%M:%S")
        # nginx error logs have no timezone; treat as UTC.
        dt = dt.replace(tzinfo=datetime.UTC)
    except ValueError:
        return None

    src_ip = ""
    client_match = _CLIENT_IP_RE.search(message)
    if client_match:
        src_ip = normalize_ip(client_match.group(1).rstrip(",;."))

    config = _LOG_TYPES["error"]
    return {
        "message": line.strip(),
        "timestamp": dt,
        "timestamp_desc": config["timestamp_desc"],
        "artifact": config["artifact"],
        "artifact_long": config["artifact_long"],
        "attributes": {
            "log_type": "error",
            "src_ip": src_ip,
            "error_level": level,
            "worker_pid": pid,
            "worker_tid": tid,
            "connection_id": conn_id,
        },
    }


def parse_line(line: str, log_type: str) -> dict[str, Any] | None:
    """Parse a single log line according to its detected log type."""
    if log_type in ("access", "redirect"):
        return _parse_access_line(line, log_type)
    if log_type == "error":
        return _parse_error_line(line)
    return None


# ---------------------------------------------------------------------------
# Input discovery and log-type detection
# ---------------------------------------------------------------------------


def _open_log(path: Path) -> Any:
    """Open a plain or gzipped log file for reading text lines."""
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return open(path, encoding="utf-8", errors="replace")


def detect_log_type(filename: str) -> str | None:
    """Determine log type from a filename."""
    name_lower = Path(filename).name.lower()
    for log_type, config in _LOG_TYPES.items():
        for pattern in config["patterns"]:
            if pattern.rstrip("*") in name_lower:
                return log_type
    return None


def sniff_log_type(path: Path) -> str | None:
    """Detect the log type by sampling the first lines of the file.

    A redirect log is indistinguishable from an access log by content, so it
    is classified as "access" unless the filename says otherwise.
    """
    access_hits = 0
    error_hits = 0
    sampled = 0
    try:
        with _open_log(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                if _ACCESS_LOG_RE.match(line):
                    access_hits += 1
                elif _ERROR_LOG_RE.match(line):
                    error_hits += 1
                sampled += 1
                if sampled >= 50:
                    break
    except OSError:
        return None
    if access_hits == 0 and error_hits == 0:
        return None
    return "access" if access_hits >= error_hits else "error"


def find_log_files(input_path: str) -> list[tuple[Path, str]]:
    """Resolve the input into ``(file, log_type)`` pairs."""
    path = Path(input_path)
    if path.is_file():
        log_type = detect_log_type(path.name) or sniff_log_type(path)
        if log_type is None:
            raise SystemExit(
                f"error: could not determine log type for {input_path} "
                "(filename not recognized and content matches neither the "
                "combined access log format nor the error log format)"
            )
        return [(path, log_type)]
    if path.is_dir():
        found: list[tuple[Path, str]] = []
        seen: set[Path] = set()
        for log_type, config in _LOG_TYPES.items():
            for pattern in config["patterns"]:
                for match in sorted(path.glob(pattern)):
                    if match.is_file() and match not in seen:
                        seen.add(match)
                        found.append((match, log_type))
        if not found:
            raise SystemExit(f"error: no supported nginx log files found in {input_path}")
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
PARALLEL_MIN_BYTES = int(os.environ.get("NGINX2TS_PARALLEL_MIN_BYTES", 256 * 1024 * 1024))
# No single parallel chunk may exceed this many bytes, so per-worker memory
# stays bounded on huge files.
MAX_CHUNK_BYTES = int(os.environ.get("NGINX2TS_MAX_CHUNK_BYTES", 128 * 1024 * 1024))
# Default cap on parallel workers; high core counts otherwise multiply peak RAM.
DEFAULT_MAX_WORKERS = int(os.environ.get("NGINX2TS_DEFAULT_WORKERS", 4))


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
    log_type: str,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    start_offset: int = 0,
) -> tuple[int, int]:
    """Parse a binary line stream into the buffer.

    Returns ``(parsed, skipped)`` line counts.
    """
    parsed = 0
    skipped = 0
    for offset, line in _iter_lines_with_offsets(fh):
        if not line.strip():
            continue
        row = parse_line(line, log_type)
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
    path_str: str, start: int, end: int, log_type: str, source_file: str, file_hash: str
) -> tuple[bytes, int, int]:
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
    parsed, skipped = _convert_stream(
        io.BytesIO(window), log_type, source_file, file_hash, buffer, start_offset=start
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
    path: Path,
    log_type: str,
    file_hash: str,
    buffer: _BatchBuffer,
    workers: int,
    verbose: bool,
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
                    pool.submit(_parse_chunk, str(path), start, end, log_type, path.name, file_hash)
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
    input_path: str, output: str, workers: int, verbose: bool, split: str | None = None
) -> int:
    """Convert nginx logs at ``input_path`` into ``output`` (.parquet)."""
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
    for path, _log_type in files:
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
        for path, log_type in files:
            if verbose:
                sys.stderr.write(f"parsing {path} as {log_type}...\n")
            parallel = (
                path.suffix != ".gz" and workers > 1 and path.stat().st_size >= PARALLEL_MIN_BYTES
            )
            if parallel:
                parsed, skipped = _convert_file_parallel(
                    path, log_type, hashes[path], buffer, workers, verbose
                )
            else:
                opener = gzip.open if path.suffix == ".gz" else open
                with opener(path, "rb") as fh:
                    parsed, skipped = _convert_stream(fh, log_type, path.name, hashes[path], buffer)
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
    parser = argparse.ArgumentParser(
        description=(
            "Convert nginx access/error/redirect logs (plain or .gz, file or "
            "directory) to a Vestigo Parquet file for direct upload."
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
    parser.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")
    args = parser.parse_args()
    return convert(args.input, args.output, max(1, args.workers), args.verbose, split=args.split)


if __name__ == "__main__":
    sys.exit(main())
