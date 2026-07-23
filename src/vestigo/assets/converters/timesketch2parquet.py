#!/usr/bin/env python3
"""Convert a generic Timesketch-compatible CSV or JSONL file to a Vestigo Parquet file.

Parses an arbitrary Timesketch timeline (any column set — no per-source-type parsing
logic, fields are taken over as present) locally and writes one ``.parquet`` file in
the Vestigo interchange format (version 1). Upload the result to the Vestigo
web interface or ingest it with ``vestigo ingest`` — no server re-parse.

Column requirements follow upstream google/timesketch's own CSV/JSONL import spec
(https://github.com/google/timesketch/blob/master/docs/guides/user/import-from-json-csv.md,
``timesketch/lib/utils.py::read_and_validate_csv``/``read_and_validate_jsonl``), not
Vestigo's own server-side generic-CSV parser conventions:

- Mandatory columns: ``message``, ``timestamp_desc``, and ``datetime`` (CSV may substitute
  a numeric ``timestamp`` column for ``datetime``).
- ``tag``/``tags``: parsed into a list (JSON array, comma-separated, or a single bare string).
- Everything else — including any ``source``/``source_long``/``data_type``/``display_name``
  columns a file happens to have — is just an arbitrary extra column and lands in
  ``attributes`` verbatim; there is no special promotion of such columns to `artifact`,
  since upstream Timesketch has no equivalent concept. Every row gets the same fixed
  ``artifact``/``artifact_long`` values.

Forensic provenance embedded in the output:
  * per input file: sha256 + size in the Parquet footer metadata,
  * per event row: the sha256 of its original file (``file_hash``), the byte offset of the
    record within it (``byte_offset``; a CSV logical record can span multiple physical
    lines when a field contains embedded newlines — the byte span covers all of them), and
    the sha256 of the record's raw bytes (``content_hash``),
  * the converter name and version, which become the server-side parser identity.

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow timesketch2parquet.py ...

Usage:

    python timesketch2parquet.py -i timeline.csv -o timeline.parquet
    python timesketch2parquet.py -i timeline.jsonl.gz -o timeline.parquet
    python timesketch2parquet.py -i /var/timelines/ -o timelines.parquet -w 8
"""

from __future__ import annotations

import ast
import collections
import concurrent.futures
import csv
import datetime
import gzip
import hashlib
import io
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
        "error: pyarrow is required to write Vestigo Parquet files.\n"
        "Install it with:  pip install pyarrow\n"
        "or run this script via:  uv run --with pyarrow timesketch2parquet.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "timesketch2parquet"
CONVERTER_VERSION = "1.3.0"

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

# Every row gets the same fixed artifact — upstream Timesketch has no equivalent of
# Vestigo's Artifact-stamped-event model, so there's nothing per-row to derive it from.
_ARTIFACT = "generic:timesketch:event"
_ARTIFACT_LONG = "timesketch:generic:event"

_RECOGNIZED_KEYS = ("message", "timestamp_desc", "datetime", "timestamp", "tag", "tags")


# ---------------------------------------------------------------------------
# Field parsing (ported from upstream google/timesketch's own import rules)
# ---------------------------------------------------------------------------


def _parse_tag_field(value: str) -> list[str]:
    """Split a Timesketch tag field into individual tags.

    Matches upstream's own ``_parse_tag_field``: a JSON array string, a
    comma-separated string, or a single bare string, in that order. Unlike
    Vestigo's server-side generic-CSV parser, upstream does not also
    split on ``|`` — this matches upstream exactly.
    """
    if not value:
        return []
    stripped = value.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        parsed: Any = None
        try:
            parsed = json.loads(stripped)
        except ValueError:
            try:
                parsed = ast.literal_eval(stripped)
            except (ValueError, SyntaxError):
                parsed = None
        if isinstance(parsed, list):
            return [str(t).strip() for t in parsed if str(t).strip()]
    if "," in stripped:
        return [t.strip() for t in stripped.split(",") if t.strip()]
    return [stripped] if stripped else []


def _parse_datetime_iso(value: str) -> datetime.datetime | None:
    """Parse a Timesketch ``datetime`` column value to a UTC datetime.

    Upstream uses ``pandas.to_datetime(..., format="mixed", utc=True)`` (CSV) /
    ``dateutil.parser.parse`` (JSONL) — a much broader fuzzy parser than the
    stdlib affords. This script stays stdlib+pyarrow only, so it covers ISO-8601
    (the documented/expected format) via ``datetime.fromisoformat`` plus a couple
    of common non-ISO fallbacks, deliberately narrower than upstream's fuzzy
    matcher. Naive values are assumed UTC. Returns None if unparseable or if the
    result falls outside upstream's own validity window (year 1700-9999).
    """
    value = value.strip()
    if not value:
        return None
    iso = value.replace("Z", "+00:00")
    dt: datetime.datetime | None = None
    try:
        dt = datetime.datetime.fromisoformat(iso)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                dt = datetime.datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    dt = dt.astimezone(datetime.UTC)
    if dt.year < 1700 or dt.year > 9999:
        return None
    return dt


def _parse_timestamp_numeric(value: Any) -> datetime.datetime | None:
    """Parse a numeric epoch ``timestamp`` value to a UTC datetime.

    Applies upstream's exact magnitude heuristic to detect the unit
    (seconds/milliseconds/microseconds/nanoseconds), rather than Vestigo's
    own digit-count heuristic used elsewhere in this codebase — this converter
    follows upstream Timesketch's own rules verbatim.
    """
    try:
        magnitude = float(value)
    except (TypeError, ValueError):
        return None
    abs_magnitude = abs(magnitude)
    if abs_magnitude > 1e17:
        seconds = magnitude / 1_000_000_000
    elif abs_magnitude > 1e14:
        seconds = magnitude / 1_000_000
    elif abs_magnitude > 1e11:
        seconds = magnitude / 1_000
    else:
        seconds = magnitude
    try:
        dt = datetime.datetime.fromtimestamp(seconds, tz=datetime.UTC)
    except (OverflowError, OSError, ValueError):
        return None
    if dt.year < 1700 or dt.year > 9999:
        return None
    return dt


def build_row(fields: dict[str, Any]) -> dict[str, Any] | None:
    """Map one CSV row / JSONL record's fields to an event row dict.

    ``fields`` keys keep their original casing (used verbatim for any
    non-recognized column folded into ``attributes``); recognized columns are
    matched case-insensitively. Returns None if the record lacks a mandatory
    field or a resolvable timestamp.
    """
    lower_map: dict[str, str] = {}
    for key in fields:
        lk = key.strip().lower()
        if lk not in lower_map:
            lower_map[lk] = key

    def get(name: str) -> Any:
        orig = lower_map.get(name)
        return fields.get(orig) if orig is not None else None

    message = get("message")
    timestamp_desc = get("timestamp_desc")
    if message in (None, "") or timestamp_desc in (None, ""):
        return None

    datetime_value = get("datetime")
    timestamp_value = get("timestamp")

    ts: datetime.datetime | None = None
    if datetime_value not in (None, ""):
        ts = _parse_datetime_iso(str(datetime_value))
    if ts is None and timestamp_value not in (None, ""):
        ts = _parse_timestamp_numeric(timestamp_value)
    if ts is None:
        return None

    tag_value = get("tag")
    if tag_value in (None, ""):
        tag_value = get("tags")
    tags: list[str] = []
    if isinstance(tag_value, list):
        tags = [str(t).strip() for t in tag_value if str(t).strip()]
    elif tag_value not in (None, ""):
        tags = _parse_tag_field(str(tag_value))

    recognized_originals = {lower_map[k] for k in _RECOGNIZED_KEYS if k in lower_map}
    attrs = {
        k: v
        for k, v in fields.items()
        if k not in recognized_originals and v is not None and str(v) != ""
    }

    return {
        "message": str(message),
        "timestamp": ts,
        "timestamp_desc": str(timestamp_desc),
        "artifact": _ARTIFACT,
        "artifact_long": _ARTIFACT_LONG,
        "tags": tags,
        "attributes": attrs,
    }


def validate_columns(keys: set[str], context: str) -> None:
    """Fail fast if the mandatory columns aren't structurally present.

    Mirrors upstream's own fail-fast header check (``TIMESKETCH_FIELDS``)
    rather than silently skipping every row of a malformed file.
    """
    lower_keys = {k.strip().lower() for k in keys if k}
    missing = []
    if "message" not in lower_keys:
        missing.append("message")
    if "timestamp_desc" not in lower_keys:
        missing.append("timestamp_desc")
    if "datetime" not in lower_keys and "timestamp" not in lower_keys:
        missing.append("datetime (or timestamp)")
    if missing:
        raise SystemExit(
            f"error: {context} is missing mandatory column(s): {', '.join(missing)} "
            "(see https://github.com/google/timesketch/blob/master/docs/guides/user/"
            "import-from-json-csv.md)"
        )


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------

_CSV_EXTENSIONS = (".csv", ".tsv")
_JSONL_EXTENSIONS = (".jsonl", ".json", ".ndjson")


def detect_format(path: Path) -> str | None:
    """Return ``"csv"``/``"jsonl"`` for a recognized extension, else None."""
    name = path.name.lower()
    stem = name[:-3] if name.endswith(".gz") else name
    if stem.endswith(_CSV_EXTENSIONS):
        return "csv"
    if stem.endswith(_JSONL_EXTENSIONS):
        return "jsonl"
    return None


def find_input_files(input_path: str) -> list[tuple[Path, str]]:
    """Resolve the input into a sorted list of ``(file, format)`` pairs."""
    path = Path(input_path)
    if path.is_file():
        fmt = detect_format(path)
        if fmt is None:
            raise SystemExit(
                f"error: cannot detect CSV/JSONL format for {input_path} "
                "(expected .csv/.tsv or .jsonl/.json/.ndjson, optionally .gz)"
            )
        return [(path, fmt)]
    if path.is_dir():
        found: list[tuple[Path, str]] = []
        for candidate in sorted(path.rglob("*")):
            if not candidate.is_file():
                continue
            fmt = detect_format(candidate)
            if fmt is not None:
                found.append((candidate, fmt))
        if not found:
            raise SystemExit(f"error: no CSV/JSONL files found in {input_path}")
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
PARALLEL_MIN_BYTES = int(os.environ.get("TIMESKETCH2PARQUET_PARALLEL_MIN_BYTES", 256 * 1024 * 1024))
# No single parallel chunk may exceed this many bytes, so per-worker memory
# stays bounded on huge files.
MAX_CHUNK_BYTES = int(os.environ.get("TIMESKETCH2PARQUET_MAX_CHUNK_BYTES", 128 * 1024 * 1024))
# Default cap on parallel workers; high core counts otherwise multiply peak RAM.
DEFAULT_MAX_WORKERS = int(os.environ.get("TIMESKETCH2PARQUET_DEFAULT_WORKERS", 4))


class _BatchBuffer:
    """Columnar row buffer flushed to a ParquetWriter as record batches."""

    def __init__(self, writer: pq.ParquetWriter) -> None:
        self._writer = writer
        self._columns: dict[str, list[Any]] = {name: [] for name in PARQUET_EVENT_SCHEMA.names}
        self.rows_written = 0

    def append(
        self,
        source_file: str,
        file_hash: str,
        byte_offset: int,
        content_bytes: bytes,
        row: dict[str, Any],
    ) -> None:
        cols = self._columns
        cols["source_file"].append(source_file)
        cols["file_hash"].append(file_hash)
        cols["byte_offset"].append(byte_offset)
        cols["content_hash"].append(hashlib.sha256(content_bytes).hexdigest())
        cols["message"].append(row["message"])
        cols["timestamp"].append(row["timestamp"])
        cols["timestamp_desc"].append(row["timestamp_desc"])
        cols["artifact"].append(row["artifact"])
        cols["artifact_long"].append(row["artifact_long"])
        cols["display_name"].append("")
        cols["tags"].append(row["tags"])
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


# ---------------------------------------------------------------------------
# JSONL parsing (line-oriented — chunkable like the other converters)
# ---------------------------------------------------------------------------


def _iter_lines_with_offsets(fh: BinaryIO) -> Any:
    """Yield ``(byte_offset, decoded_line)`` from a binary stream."""
    offset = 0
    for raw in fh:
        line = raw.rstrip(b"\r\n").decode("utf-8", errors="replace")
        yield offset, line
        offset += len(raw)


def _parse_since_until(value: str | None) -> datetime.datetime | None:
    """Parse an ISO 8601 ``--since``/``--until`` value to a UTC-aware datetime."""
    if not value:
        return None
    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _in_window(
    ts: datetime.datetime | None,
    since_dt: datetime.datetime | None,
    until_dt: datetime.datetime | None,
) -> bool:
    """Return True if ``ts`` passes the window. A ``None`` ts always passes."""
    if ts is None:
        return True
    if since_dt is not None and ts < since_dt:
        return False
    return not (until_dt is not None and ts > until_dt)


def _convert_jsonl_stream(
    fh: BinaryIO,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    start_offset: int = 0,
    validate: bool = False,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[int, int, int]:
    """Parse a binary JSONL line stream into the buffer.

    Returns ``(parsed, skipped, skipped_by_time)``.
    """
    parsed = 0
    skipped = 0
    skipped_by_time = 0
    validated = not validate
    for offset, line in _iter_lines_with_offsets(fh):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            skipped += 1
            continue
        if not isinstance(record, dict):
            skipped += 1
            continue
        if not validated:
            validate_columns(set(record.keys()), f"{source_file} (first record)")
            validated = True
        row = build_row(record)
        if row is None:
            skipped += 1
            continue
        if not _in_window(row["timestamp"], since_dt, until_dt):
            skipped_by_time += 1
            continue
        buffer.append(source_file, file_hash, start_offset + offset, line.encode("utf-8"), row)
        parsed += 1
    return parsed, skipped, skipped_by_time


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


def _parse_jsonl_chunk(
    path_str: str,
    start: int,
    end: int,
    source_file: str,
    file_hash: str,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[bytes, int, int, int]:
    """Worker: parse ``[start, end)`` of a plain JSONL file, return Arrow IPC bytes."""
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
    parsed, skipped, skipped_by_time = _convert_jsonl_stream(
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


def _convert_jsonl_file_parallel(
    path: Path,
    file_hash: str,
    buffer: _BatchBuffer,
    workers: int,
    verbose: bool,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[int, int, int]:
    """Parse a large plain JSONL file across worker processes."""
    chunks = find_chunk_boundaries(path, target_chunks=workers * 4)
    if verbose:
        sys.stderr.write(f"  parallel: {len(chunks)} chunks, {workers} workers\n")
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
                        _parse_jsonl_chunk,
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
# CSV parsing (a logical record can span multiple physical lines — not
# safely newline-chunkable, so this path is always single-process)
# ---------------------------------------------------------------------------


class _ByteTrackingCsvSource:
    """Feed decoded lines to ``csv.reader``/``csv.DictReader`` while tracking the
    exact raw bytes of each logical record (which may span several physical
    lines when a quoted field embeds a newline).

    ``csv.reader`` pulls exactly the physical lines needed to complete one
    logical record before yielding it (no read-ahead), so at
    ``finish_record()`` time the buffer holds precisely that record's raw
    bytes — the same technique ``ingestion/parser.py::_RecordTrackingIterator``
    uses server-side, reimplemented here since this script cannot import
    ``vestigo.*``.
    """

    def __init__(self, raw_line_iter: Any, start_offset: int) -> None:
        self._it = raw_line_iter
        self._buffer_raw: list[bytes] = []
        self._next_offset = start_offset
        self._record_offset = start_offset

    def __iter__(self) -> _ByteTrackingCsvSource:
        return self

    def __next__(self) -> str:
        raw = next(self._it)
        self._buffer_raw.append(raw)
        self._next_offset += len(raw)
        return raw.decode("utf-8", errors="replace")

    def finish_record(self) -> tuple[int, bytes]:
        """Return ``(byte_offset, raw_bytes)`` of the record just completed."""
        offset = self._record_offset
        raw_bytes = b"".join(self._buffer_raw)
        self._buffer_raw.clear()
        self._record_offset = self._next_offset
        return offset, raw_bytes


def _convert_csv_file(
    path: Path,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[int, int, int]:
    """Parse one CSV/TSV file (plain or ``.gz``) into the buffer.

    Returns ``(parsed, skipped, skipped_by_time)``.
    """
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    parsed = 0
    skipped = 0
    skipped_by_time = 0
    with opener(path, "rb") as fh:
        sample = fh.read(4096)
        fh.seek(0)
        sample_text = sample.decode("utf-8", errors="replace")
        try:
            dialect = csv.Sniffer().sniff(sample_text, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        dialect.doublequote = True

        header_raw = fh.readline()
        if not header_raw:
            return 0, 0, 0
        header_line = header_raw.decode("utf-8", errors="replace")
        header_reader = csv.reader([header_line], dialect=dialect)
        headers = next(header_reader, None) or []
        headers = [h.strip() if h else h for h in headers]
        validate_columns(set(headers), str(path))

        source = _ByteTrackingCsvSource(fh, start_offset=len(header_raw))
        row_reader = csv.DictReader(source, fieldnames=headers, dialect=dialect)
        for row in row_reader:
            byte_offset, raw_bytes = source.finish_record()
            if not raw_bytes.strip():
                continue
            result = build_row(row)
            if result is None:
                skipped += 1
                continue
            if not _in_window(result["timestamp"], since_dt, until_dt):
                skipped_by_time += 1
                continue
            buffer.append(source_file, file_hash, byte_offset, raw_bytes, result)
            parsed += 1
    return parsed, skipped, skipped_by_time


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


def convert(
    input_path: str,
    output: str,
    workers: int,
    verbose: bool,
    split: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    """Convert a generic Timesketch CSV/JSONL file/directory into ``output`` (.parquet)."""
    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "Vestigo server detects the ingest parser strictly by file extension."
        )

    since_dt = _parse_since_until(since)
    until_dt = _parse_since_until(until)

    split_spec = parse_split_spec(split) if split else None
    write_target = output if split_spec is None else f"{output}.tmp"

    files = find_input_files(input_path)

    if verbose:
        sys.stderr.write(f"hashing {len(files)} input file(s)...\n")
    provenance = []
    hashes: dict[Path, str] = {}
    for path, _fmt in files:
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
        META_TIMEZONE_ASSUMPTION: (
            "timestamps taken from the source Timesketch 'datetime'/'timestamp' "
            "fields (ISO offsets honored; naive assumed UTC)"
        ),
        META_PARSE_DECISIONS: json.dumps({"since": since, "until": until}, sort_keys=True),
    }

    parsed_total = 0
    skipped_total = 0
    skipped_by_time_total = 0
    schema = PARQUET_EVENT_SCHEMA.with_metadata(metadata)
    with pq.ParquetWriter(write_target, schema, compression="zstd") as writer:
        buffer = _BatchBuffer(writer)
        for path, fmt in files:
            if verbose:
                sys.stderr.write(f"parsing {path} as {fmt}...\n")
            if fmt == "csv":
                parsed, skipped, skipped_by_time = _convert_csv_file(
                    path, path.name, hashes[path], buffer, since_dt=since_dt, until_dt=until_dt
                )
            else:
                parallel = (
                    not path.name.lower().endswith(".gz")
                    and workers > 1
                    and path.stat().st_size >= PARALLEL_MIN_BYTES
                )
                if parallel:
                    # Validate the mandatory columns up front in the main
                    # process before fanning out chunked workers.
                    opener_check = gzip.open if path.name.lower().endswith(".gz") else open
                    with opener_check(path, "rb") as fh:
                        for _offset, line in _iter_lines_with_offsets(fh):
                            if line.strip():
                                try:
                                    first = json.loads(line)
                                except json.JSONDecodeError:
                                    first = None
                                if isinstance(first, dict):
                                    validate_columns(set(first.keys()), f"{path} (first record)")
                                break
                    parsed, skipped, skipped_by_time = _convert_jsonl_file_parallel(
                        path, hashes[path], buffer, workers, verbose, since_dt, until_dt
                    )
                else:
                    opener = gzip.open if path.name.lower().endswith(".gz") else open
                    with opener(path, "rb") as fh:
                        parsed, skipped, skipped_by_time = _convert_jsonl_stream(
                            fh,
                            path.name,
                            hashes[path],
                            buffer,
                            validate=True,
                            since_dt=since_dt,
                            until_dt=until_dt,
                        )
            parsed_total += parsed
            skipped_total += skipped
            skipped_by_time_total += skipped_by_time
        buffer.flush()
        writer.add_key_value_metadata(
            {
                META_ROW_COUNTS: json.dumps(
                    {
                        "parsed": parsed_total,
                        "skipped_malformed": skipped_total,
                        "skipped_by_time": skipped_by_time_total,
                    }
                )
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
            f"({skipped_total} unparseable records skipped{time_note})\n"
        )
    else:
        sys.stderr.write(
            f"{CONVERTER_NAME}: wrote {parsed_total} events to {output} "
            f"({skipped_total} unparseable records skipped{time_note})\n"
        )
    return 0 if parsed_total > 0 else 1


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Convert a generic Timesketch-compatible CSV or JSONL file (plain or "
            ".gz, file or directory) to a Vestigo Parquet file for direct upload."
        )
    )
    parser.add_argument("-i", "--input", required=True, help="CSV/JSONL file or directory")
    parser.add_argument("-o", "--output", required=True, help="output .parquet path")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=min(getattr(os, "process_cpu_count", os.cpu_count)() or 4, DEFAULT_MAX_WORKERS),
        help="parallel parser processes for large plain JSONL files "
        "(default: min(CPU count, %(default)s)); "
        "CSV is always parsed single-process (see module docstring)",
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
        help="Only records at or after this ISO 8601 timestamp "
        "(e.g. 2026-07-01T00:00:00Z). Rows with no parseable timestamp are kept.",
    )
    parser.add_argument(
        "--until",
        help="Only records at or before this ISO 8601 timestamp "
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
