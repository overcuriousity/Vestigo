#!/usr/bin/env python3
"""Convert a generic Timesketch-compatible CSV or JSONL file to a TraceSignal Parquet file.

Parses an arbitrary Timesketch timeline (any column set — no per-source-type parsing
logic, fields are taken over as present) locally and writes one ``.parquet`` file in
the TraceSignal interchange format (version 1). Upload the result to the TraceSignal
web interface or ingest it with ``tsig ingest`` — no server re-parse.

Column requirements follow upstream google/timesketch's own CSV/JSONL import spec
(https://github.com/google/timesketch/blob/master/docs/guides/user/import-from-json-csv.md,
``timesketch/lib/utils.py::read_and_validate_csv``/``read_and_validate_jsonl``), not
TraceSignal's own server-side generic-CSV parser conventions:

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
import concurrent.futures
import csv
import datetime
import gzip
import hashlib
import io
import json
import multiprocessing
import os
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
        "or run this script via:  uv run --with pyarrow timesketch2parquet.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "timesketch2parquet"
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

# Every row gets the same fixed artifact — upstream Timesketch has no equivalent of
# TraceSignal's Artifact-stamped-event model, so there's nothing per-row to derive it from.
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
    TraceSignal's server-side generic-CSV parser, upstream does not also
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
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    dt = dt.astimezone(datetime.timezone.utc)
    if dt.year < 1700 or dt.year > 9999:
        return None
    return dt


def _parse_timestamp_numeric(value: Any) -> datetime.datetime | None:
    """Parse a numeric epoch ``timestamp`` value to a UTC datetime.

    Applies upstream's exact magnitude heuristic to detect the unit
    (seconds/milliseconds/microseconds/nanoseconds), rather than TraceSignal's
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
        dt = datetime.datetime.fromtimestamp(seconds, tz=datetime.timezone.utc)
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


def _convert_jsonl_stream(
    fh: BinaryIO,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    start_offset: int = 0,
    validate: bool = False,
) -> tuple[int, int]:
    """Parse a binary JSONL line stream into the buffer. Returns ``(parsed, skipped)``."""
    parsed = 0
    skipped = 0
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
        buffer.append(source_file, file_hash, start_offset + offset, line.encode("utf-8"), row)
        parsed += 1
    return parsed, skipped


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


def _parse_jsonl_chunk(
    path_str: str, start: int, end: int, source_file: str, file_hash: str
) -> tuple[bytes, int, int]:
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
    parsed, skipped = _convert_jsonl_stream(
        io.BytesIO(window), source_file, file_hash, buffer, start_offset=start
    )
    buffer.flush()
    writer_ipc.close()
    return sink.getvalue(), parsed, skipped


def _convert_jsonl_file_parallel(
    path: Path, file_hash: str, buffer: _BatchBuffer, workers: int, verbose: bool
) -> tuple[int, int]:
    """Parse a large plain JSONL file across worker processes."""
    chunks = find_chunk_boundaries(path, target_chunks=workers * 4)
    if verbose:
        sys.stderr.write(f"  parallel: {len(chunks)} chunks, {workers} workers\n")
    parsed_total = 0
    skipped_total = 0
    ctx = multiprocessing.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=workers, mp_context=ctx) as pool:
        futures = [
            pool.submit(_parse_jsonl_chunk, str(path), start, end, path.name, file_hash)
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
    ``tracesignal.*``.
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
    path: Path, source_file: str, file_hash: str, buffer: _BatchBuffer
) -> tuple[int, int]:
    """Parse one CSV/TSV file (plain or ``.gz``) into the buffer."""
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    parsed = 0
    skipped = 0
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
            return 0, 0
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
            buffer.append(source_file, file_hash, byte_offset, raw_bytes, result)
            parsed += 1
    return parsed, skipped


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def convert(input_path: str, output: str, workers: int, verbose: bool) -> int:
    """Convert a generic Timesketch CSV/JSONL file/directory into ``output`` (.parquet)."""
    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "TraceSignal server detects the ingest parser strictly by file extension."
        )

    files = find_input_files(input_path)

    if verbose:
        sys.stderr.write(f"hashing {len(files)} input file(s)...\n")
    provenance = []
    hashes: dict[Path, str] = {}
    for path, _fmt in files:
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
        for path, fmt in files:
            if verbose:
                sys.stderr.write(f"parsing {path} as {fmt}...\n")
            if fmt == "csv":
                parsed, skipped = _convert_csv_file(path, path.name, hashes[path], buffer)
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
                        for offset, line in _iter_lines_with_offsets(fh):
                            if line.strip():
                                try:
                                    first = json.loads(line)
                                except json.JSONDecodeError:
                                    first = None
                                if isinstance(first, dict):
                                    validate_columns(
                                        set(first.keys()), f"{path} (first record)"
                                    )
                                break
                    parsed, skipped = _convert_jsonl_file_parallel(
                        path, hashes[path], buffer, workers, verbose
                    )
                else:
                    opener = gzip.open if path.name.lower().endswith(".gz") else open
                    with opener(path, "rb") as fh:
                        parsed, skipped = _convert_jsonl_stream(
                            fh, path.name, hashes[path], buffer, validate=True
                        )
            parsed_total += parsed
            skipped_total += skipped
        buffer.flush()

    sys.stderr.write(
        f"{CONVERTER_NAME}: wrote {parsed_total} events to {output} "
        f"({skipped_total} unparseable records skipped)\n"
    )
    return 0 if parsed_total > 0 else 1


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(
        description=(
            "Convert a generic Timesketch-compatible CSV or JSONL file (plain or "
            ".gz, file or directory) to a TraceSignal Parquet file for direct upload."
        )
    )
    parser.add_argument("-i", "--input", required=True, help="CSV/JSONL file or directory")
    parser.add_argument("-o", "--output", required=True, help="output .parquet path")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=getattr(os, "process_cpu_count", os.cpu_count)() or 4,
        help="parallel parser processes for large plain JSONL files (default: CPU count); "
        "CSV is always parsed single-process (see module docstring)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="progress on stderr")
    args = parser.parse_args()
    return convert(args.input, args.output, max(1, args.workers), args.verbose)


if __name__ == "__main__":
    sys.exit(main())
