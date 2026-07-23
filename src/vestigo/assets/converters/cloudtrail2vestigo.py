#!/usr/bin/env python3
"""Convert AWS CloudTrail logs to a Vestigo Parquet file.

Parses raw CloudTrail JSON/JSON.gz exports (standard S3 delivery layout: a
top-level ``Records`` array, or a bare JSON list) locally and writes one
``.parquet`` file in the Vestigo interchange format (version 1). Upload
the result to the Vestigo web interface or ingest it with
``vestigo ingest`` — no CSV/JSONL intermediate, no server re-parse.

Nested objects (``userIdentity``, ``requestParameters``, ``responseElements``,
etc.) are flattened into dot-notation attribute keys.

Unlike the line-oriented converters (nginx, filterlog, suricata), a CloudTrail
file holds one JSON array rather than one record per line, so byte offsets
are computed by re-scanning the ``Records`` array with
``json.JSONDecoder.raw_decode`` one object at a time (see
``_iter_json_records_with_offsets``) instead of splitting on newlines. This
gives each row an exact byte span within the original file, so
``content_hash`` is the sha256 of the record's *original* bytes rather than a
re-serialization.

Forensic provenance embedded in the output:
  * per input file: sha256 + size in the Parquet footer metadata,
  * per event row: the sha256 of its original file (``file_hash``), the byte
    offset of the record within it (``byte_offset``; offsets into the
    *decompressed* stream for ``.gz`` inputs), and the sha256 of the record's
    raw bytes (``content_hash``),
  * the converter name and version, which become the server-side parser
    identity.

Requires ``pyarrow`` (the only non-stdlib dependency):

    pip install pyarrow        # or: uv run --with pyarrow cloudtrail2vestigo.py ...

Usage:

    python cloudtrail2vestigo.py -i cloudtrail.json.gz -o cloudtrail.parquet
    python cloudtrail2vestigo.py -i /var/log/cloudtrail/ -o cloudtrail.parquet -w 8
"""

from __future__ import annotations

import collections
import concurrent.futures
import datetime
import gzip
import hashlib
import io
import multiprocessing
import os
import re
import sys
from pathlib import Path
from typing import Any

try:
    import pyarrow as pa
    import pyarrow.parquet as pq
except ImportError:  # pragma: no cover - environment guard
    sys.stderr.write(
        "error: pyarrow is required to write Vestigo Parquet files.\n"
        "Install it with:  pip install pyarrow\n"
        "or run this script via:  uv run --with pyarrow cloudtrail2vestigo.py ...\n"
    )
    sys.exit(2)

CONVERTER_NAME = "cloudtrail2vestigo"
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

# ---------------------------------------------------------------------------
# CloudTrail record parsing (ported from cloudtrail2timesketch.py, converter parity)
# ---------------------------------------------------------------------------

# Top-level scalar fields to promote directly into the row.
_TOP_LEVEL_FIELDS = (
    "eventVersion",
    "eventTime",
    "eventSource",
    "eventName",
    "awsRegion",
    "eventType",
    "readOnly",
    "managementEvent",
    "recipientAccountId",
    "requestID",
    "eventID",
    "errorCode",
    "errorMessage",
    "sharedEventID",
    "vpcEndpointId",
    "vpcEndpointAccountId",
)

# Nested objects that should be flattened with dot notation.
_FLATTEN_FIELDS = (
    "userIdentity",
    "requestParameters",
    "responseElements",
    "additionalEventData",
    "serviceEventDetails",
    "tlsDetails",
)


class CloudTrailParseError(Exception):
    """Raised when a CloudTrail file cannot be parsed."""


def normalize_ip(value: str | None) -> str:
    """Validate and canonicalize a single IPv4/IPv6 address string."""
    import ipaddress

    if not value:
        return ""
    try:
        return str(ipaddress.ip_address(value.strip().strip("[]")))
    except ValueError:
        return ""


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


def _serialize(value: Any) -> str:
    """Serialize a non-scalar value to a compact JSON string."""
    import json

    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _parse_event_time(value: str | None) -> datetime.datetime | None:
    """Parse a CloudTrail ISO 8601 timestamp to a UTC datetime, or None."""
    if not value:
        return None
    ts = str(value).replace("Z", "+00:00")
    try:
        dt = datetime.datetime.fromisoformat(ts)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.UTC)
        return dt.astimezone(datetime.UTC)
    except ValueError:
        return None


def _format_user(record: dict[str, Any]) -> str:
    """Return a concise user identity string for the message column."""
    user = record.get("userIdentity") or {}
    user_type = user.get("type", "Unknown")

    if user_type == "IAMUser":
        return user.get("userName") or user.get("arn") or user_type
    if user_type == "AssumedRole":
        session = user.get("sessionContext", {}).get("sessionIssuer", {})
        role = session.get("userName") or session.get("arn") or ""
        principal = user.get("principalId", "")
        if role:
            return f"{role} ({principal})" if principal else role
        return user.get("arn") or user_type
    if user_type == "AWSService":
        invoked = user.get("invokedBy") or ""
        return f"{user_type} ({invoked})" if invoked else user_type
    if user_type == "Root":
        return user.get("arn") or "Root"
    if user_type == "Federated":
        return user.get("principalId") or user_type
    return user.get("arn") or user.get("principalId") or user_type


def _build_message(record: dict[str, Any]) -> str:
    """Build a human-readable summary of a CloudTrail record."""
    event_type = record.get("eventType", "AwsApiCall")
    event_source = record.get("eventSource", "")
    event_name = record.get("eventName", "Unknown")
    user_str = _format_user(record)
    region = record.get("awsRegion", "")

    action = f"{event_source}:{event_name}" if event_source else event_name
    parts = [f"{event_type}: {action} by {user_str}"]
    if region:
        parts.append(f"in {region}")
    error_code = record.get("errorCode")
    if error_code:
        parts.append(f"[{error_code}]")
    return " ".join(parts)


def build_row(record: dict[str, Any]) -> dict[str, Any]:
    """Map a single CloudTrail record to an event row dict."""
    event_time = record.get("eventTime")
    timestamp = _parse_event_time(event_time)

    event_category = record.get("eventCategory", "unknown")
    artifact = f"cloudtrail:{str(event_category).lower()}:event"

    attrs: dict[str, Any] = {}
    for field in _TOP_LEVEL_FIELDS:
        if field in record:
            attrs[field] = record[field]

    for field in _FLATTEN_FIELDS:
        value = record.get(field)
        if isinstance(value, dict):
            attrs.update(_flatten(value, field))

    resources = record.get("resources")
    if resources is not None:
        attrs["resources"] = _serialize(resources)

    # sourceIPAddress is not always a literal IP - AWS service principals
    # (e.g. "config.amazonaws.com") populate it with a DNS name instead, so
    # src_ip is only set when it validates as a real address.
    source_ip = record.get("sourceIPAddress")
    if source_ip is not None:
        attrs["sourceIPAddress"] = source_ip
        attrs["src_ip"] = normalize_ip(source_ip)
    user_agent = record.get("userAgent")
    if user_agent is not None:
        attrs["userAgent"] = user_agent
        attrs["user_agent"] = user_agent

    return {
        "message": _build_message(record),
        "timestamp": timestamp,
        "timestamp_desc": "CloudTrail Event Time",
        "artifact": artifact,
        "artifact_long": "aws:cloudtrail:event",
        "attributes": attrs,
    }


# ---------------------------------------------------------------------------
# JSON record scanning with byte offsets
# ---------------------------------------------------------------------------

_RECORDS_KEY_RE = re.compile(r'"Records"\s*:\s*\[')


def _find_array_start(text: str) -> int:
    """Return the character index of the ``Records`` array's opening ``[``.

    Falls back to a bare top-level JSON array (no ``Records`` wrapper).
    """
    match = _RECORDS_KEY_RE.search(text)
    if match:
        return match.end() - 1
    stripped = text.lstrip()
    if stripped.startswith("["):
        return len(text) - len(stripped)
    raise CloudTrailParseError("no 'Records' array found")


def iter_json_records_with_offsets(text: str) -> Any:
    """Yield ``(start, end, record)`` character offsets for each array element.

    Uses ``json.JSONDecoder.raw_decode`` to decode one object at a time so the
    exact character span of each record within the original text is known —
    this is what lets ``byte_offset``/``content_hash`` address the original
    evidence bytes without re-serializing.
    """
    import json

    decoder = json.JSONDecoder()
    pos = _find_array_start(text) + 1
    n = len(text)
    while True:
        while pos < n and text[pos] in " \t\r\n,":
            pos += 1
        if pos >= n:
            raise CloudTrailParseError("truncated Records array (missing closing ])")
        if text[pos] == "]":
            return
        try:
            obj, end = decoder.raw_decode(text, pos)
        except ValueError as exc:
            raise CloudTrailParseError(f"malformed JSON record at offset {pos}: {exc}") from exc
        yield pos, end, obj
        pos = end


# ---------------------------------------------------------------------------
# Input discovery
# ---------------------------------------------------------------------------


def find_cloudtrail_files(input_path: str) -> list[Path]:
    """Resolve the input into a sorted list of CloudTrail JSON files."""
    path = Path(input_path)
    if path.is_file():
        if path.suffix in (".json", ".gz"):
            return [path]
        raise SystemExit(f"error: unsupported file extension: {path}")
    if path.is_dir():
        files: list[Path] = []
        for ext in ("*.json.gz", "*.json"):
            files.extend(path.rglob(ext))
        files = [f for f in files if "CloudTrail-Digest" not in f.name]
        if not files:
            raise SystemExit(f"error: no CloudTrail JSON files found in {input_path}")
        return sorted(set(files))
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


def _read_text(path: Path) -> str:
    """Read a plain or gzipped CloudTrail JSON file as UTF-8 text."""
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as fh:
        return fh.read()


def _iter_records(text: str) -> Any:
    """Yield ``(start, end, record)`` for a file's ``Records`` array, or a bare list."""
    import json

    try:
        yield from iter_json_records_with_offsets(text)
    except CloudTrailParseError:
        # Fall back to whole-document parsing for shapes raw_decode-scanning
        # can't handle (e.g. a bare list with unusual whitespace); offsets
        # degrade to 0 for these records rather than aborting the file.
        data = json.loads(text)
        records = data["Records"] if isinstance(data, dict) and "Records" in data else data
        if not isinstance(records, list):
            raise CloudTrailParseError("no Records array found") from None
        for record in records:
            yield 0, 0, record


# ---------------------------------------------------------------------------
# Row batching / Parquet writing
# ---------------------------------------------------------------------------

BATCH_ROWS = 50_000
# Default cap on parallel workers; each worker holds one whole decoded file
# plus its parsed records in memory, so high core counts multiply peak RAM.
DEFAULT_MAX_WORKERS = int(os.environ.get("CLOUDTRAIL2TS_DEFAULT_WORKERS", 4))


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


def _parse_since_until(value: str | None) -> datetime.datetime | None:
    """Parse an ISO 8601 ``--since``/``--until`` value to a UTC-aware datetime."""
    if not value:
        return None
    dt = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.UTC)
    return dt.astimezone(datetime.UTC)


def _convert_text(
    text: str,
    source_file: str,
    file_hash: str,
    buffer: _BatchBuffer,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[int, int, int]:
    """Parse a CloudTrail JSON document's records into the buffer.

    Returns ``(parsed, skipped, skipped_by_time)`` counts.
    """
    parsed = 0
    skipped = 0
    skipped_by_time = 0
    encoded_prefix_len = 0
    prev_char_end = 0
    for start, end, record in _iter_records(text):
        # Incrementally track the byte length of the text consumed so far so
        # re-encoding the whole prefix on every record isn't O(n^2) on large
        # files with many records.
        encoded_prefix_len += len(text[prev_char_end:start].encode("utf-8"))
        prev_char_end = start
        byte_offset = encoded_prefix_len
        span_bytes = text[start:end].encode("utf-8")
        encoded_prefix_len += len(span_bytes)
        prev_char_end = end

        if not isinstance(record, dict):
            skipped += 1
            continue
        row = build_row(record)
        ts = row["timestamp"]
        if ts is not None:
            if since_dt is not None and ts < since_dt:
                skipped_by_time += 1
                continue
            if until_dt is not None and ts > until_dt:
                skipped_by_time += 1
                continue
        # ts is None (unparseable/missing) → keep, matching upstream behavior.
        buffer.append(source_file, file_hash, byte_offset, span_bytes, row)
        parsed += 1
    return parsed, skipped, skipped_by_time


def _parse_file(
    path_str: str,
    file_hash: str,
    since_dt: datetime.datetime | None = None,
    until_dt: datetime.datetime | None = None,
) -> tuple[bytes, int, int, int]:
    """Worker: parse one CloudTrail file, return Arrow IPC bytes + counts."""
    sink = io.BytesIO()
    writer_ipc = pa.ipc.new_stream(sink, PARQUET_EVENT_SCHEMA)

    class _IpcBuffer(_BatchBuffer):
        def __init__(self) -> None:
            self._columns = {name: [] for name in PARQUET_EVENT_SCHEMA.names}
            self.rows_written = 0

        def write_batch(self, batch: pa.RecordBatch) -> None:
            writer_ipc.write_batch(batch)
            self.rows_written += batch.num_rows

    path = Path(path_str)
    buffer = _IpcBuffer()
    text = _read_text(path)
    parsed, skipped, skipped_by_time = _convert_text(
        text, path.name, file_hash, buffer, since_dt=since_dt, until_dt=until_dt
    )
    buffer.flush()
    writer_ipc.close()
    return sink.getvalue(), parsed, skipped, skipped_by_time


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


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
    """Convert CloudTrail logs at ``input_path`` into ``output`` (.parquet)."""
    import json

    if not output.lower().endswith(".parquet"):
        raise SystemExit(
            f"error: output path must end with .parquet (got: {output}) — the "
            "Vestigo server detects the ingest parser strictly by file extension."
        )

    since_dt = _parse_since_until(since)
    until_dt = _parse_since_until(until)

    split_spec = parse_split_spec(split) if split else None
    write_target = output if split_spec is None else f"{output}.tmp"

    files = find_cloudtrail_files(input_path)

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
        META_TIMEZONE_ASSUMPTION: "CloudTrail eventTime is RFC 3339 UTC",
        META_PARSE_DECISIONS: json.dumps({"since": since, "until": until}, sort_keys=True),
    }

    parsed_total = 0
    skipped_total = 0
    skipped_by_time_total = 0
    schema = PARQUET_EVENT_SCHEMA.with_metadata(metadata)
    with pq.ParquetWriter(write_target, schema, compression="zstd") as writer:
        buffer = _BatchBuffer(writer)

        if workers > 1 and len(files) > 1:
            if verbose:
                sys.stderr.write(f"parsing {len(files)} file(s) across {workers} workers...\n")
            ram = _available_ram_bytes()
            largest = max(path.stat().st_size for path in files)
            # Rough per-worker estimate: decoded text + parsed records + Arrow IPC copy.
            estimated = min(workers, len(files)) * largest * 6
            if ram and estimated > ram * 0.75:
                sys.stderr.write(
                    f"warning: {workers} workers on files up to "
                    f"{largest // (1024 * 1024)} MiB may need "
                    f"~{estimated // (1024 * 1024)} MiB RAM; "
                    f"~{ram // (1024 * 1024)} MiB available. Reduce -w if memory runs out.\n"
                )
            ctx = multiprocessing.get_context("spawn")
            with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers, mp_context=ctx
            ) as pool:
                # Submit a bounded window and consume strictly in submit order:
                # rows land in the output in input-file order (forensic
                # requirement), and at most ~2*workers file results exist in
                # the parent at once, so finished-but-unwritten Arrow IPC
                # results cannot pile up and OOM the parent.
                file_iter = iter(files)
                pending: collections.deque = collections.deque()

                def _submit_next() -> None:
                    for path in file_iter:
                        pending.append(
                            pool.submit(_parse_file, str(path), hashes[path], since_dt, until_dt)
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
        else:
            for path in files:
                if verbose:
                    sys.stderr.write(f"parsing {path}...\n")
                text = _read_text(path)
                parsed, skipped, skipped_by_time = _convert_text(
                    text, path.name, hashes[path], buffer, since_dt=since_dt, until_dt=until_dt
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
            "Convert AWS CloudTrail JSON/JSON.gz exports (file or directory) "
            "to a Vestigo Parquet file for direct upload."
        )
    )
    parser.add_argument(
        "-i", "--input", required=True, help="CloudTrail JSON/JSON.gz file or directory"
    )
    parser.add_argument("-o", "--output", required=True, help="output .parquet path")
    parser.add_argument(
        "-w",
        "--workers",
        type=int,
        default=min(getattr(os, "process_cpu_count", os.cpu_count)() or 4, DEFAULT_MAX_WORKERS),
        help="parallel parser processes across input files (default: min(CPU count, %(default)s))",
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
