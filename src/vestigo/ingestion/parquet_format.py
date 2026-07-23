"""Vestigo Parquet interchange format (version 1).

The contract between client-side converter scripts (``assets/converters/
*2vestigo.py``) and the server-side reader (:mod:`ingestion.parquet_reader`):
converters parse raw evidence logs locally and emit one ``.parquet`` file with
the per-row columns of :data:`PARQUET_EVENT_SCHEMA` plus the footer key-value
metadata below; the server maps those rows onto the ClickHouse ``events``
schema without re-parsing.

Forensic provenance: each row carries the sha256 of the **original raw
evidence file** (``file_hash``), the byte offset of the record within it
(decompressed stream offsets for ``.gz`` inputs), and the sha256 of the raw
line (``content_hash``) — so event identity is anchored to the raw evidence
plus the converter name/version, re-derivable by an examiner from the raw log
alone. The uploaded ``.parquet`` gets its own hash as the Source-level
``file_hash`` (retention/dedup convention unchanged).

Converter scripts are standalone downloads and cannot import this module;
they embed the same constants. ``tests/test_nginx_converter.py`` asserts the
embedded copies stay identical to this spec.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pyarrow as pa

FORMAT_VERSION = "1"

# Footer key-value metadata keys (Parquet stores footer metadata as bytes).
META_FORMAT_VERSION = "vestigo.format_version"
META_CONVERTER_NAME = "vestigo.converter_name"
META_CONVERTER_VERSION = "vestigo.converter_version"
# JSON array of {"name": str, "sha256": str, "size_bytes": int, "path": str,
# "mtime": str} — one entry per original raw input file (directory inputs yield
# several). `path` (absolute source path) and `mtime` (ISO-8601 UTC) are
# additive since converter 1.3.0; `OriginalFile` ignores them.
META_ORIGINAL_FILES = "vestigo.original_files"

# Additive forensic footer metadata written by converters >= 1.3.0. The reader
# does not require these (validate_parquet_source ignores them); they are
# self-documenting chain-of-custody data readable from the Parquet footer.
# `converted_at`: ISO-8601 UTC timestamp of the conversion run.
# `row_counts`: JSON {"parsed", "skipped_malformed", "skipped_by_time"}.
# `timezone_assumption`: free-text note on any tz/year guess the parser made.
# `parse_decisions`: JSON of format-specific parsing choices.
META_CONVERTED_AT = "vestigo.converted_at"
META_ROW_COUNTS = "vestigo.row_counts"
META_TIMEZONE_ASSUMPTION = "vestigo.timezone_assumption"
META_PARSE_DECISIONS = "vestigo.parse_decisions"

# Files written by converters from before the TraceSignal→Vestigo rename use
# this key prefix; the reader accepts them transparently.
_LEGACY_META_PREFIX = "tracesignal."

# Per-row columns a converter must write. `timestamp` is nullable here —
# unparseable timestamps are the *server's* sentinel-encoding concern
# (db/_dt.py), not the converter's.
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


@dataclass(frozen=True, slots=True)
class OriginalFile:
    """Provenance record for one raw evidence input file."""

    name: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True, slots=True)
class ParquetSourceMeta:
    """Validated footer metadata of a Vestigo interchange Parquet file."""

    converter_name: str
    converter_version: str
    original_files: tuple[OriginalFile, ...]


def validate_parquet_source(schema: pa.Schema, metadata: dict[bytes, bytes]) -> ParquetSourceMeta:
    """Validate an uploaded Parquet file's schema and footer metadata.

    Args:
        schema: The file's Arrow schema (``ParquetFile.schema_arrow``).
        metadata: The footer key-value metadata (bytes-keyed, as pyarrow
            exposes it; ``None`` is treated as empty).

    Returns:
        The parsed provenance metadata.

    Raises:
        ValueError: With an actionable message when the file is not a
            Vestigo interchange file, has an unsupported format version,
            or is missing required columns/metadata.
    """
    meta = {
        (k.decode() if isinstance(k, bytes) else k): (v.decode() if isinstance(v, bytes) else v)
        for k, v in (metadata or {}).items()
    }
    for key, value in list(meta.items()):
        if key.startswith(_LEGACY_META_PREFIX):
            meta.setdefault("vestigo." + key.removeprefix(_LEGACY_META_PREFIX), value)
    version = meta.get(META_FORMAT_VERSION)
    if version is None:
        raise ValueError(
            "Not a Vestigo interchange Parquet file: footer metadata key "
            f"{META_FORMAT_VERSION!r} is missing. Re-create the file with a "
            "Vestigo converter script (e.g. nginx2vestigo.py)."
        )
    if version != FORMAT_VERSION:
        raise ValueError(
            f"Unsupported Vestigo Parquet format version {version!r} "
            f"(this server reads version {FORMAT_VERSION!r}). "
            "Update the converter script or the server so the versions match."
        )

    converter_name = meta.get(META_CONVERTER_NAME, "")
    converter_version = meta.get(META_CONVERTER_VERSION, "")
    if not converter_name or not converter_version:
        raise ValueError(
            f"Vestigo Parquet file is missing {META_CONVERTER_NAME!r} or "
            f"{META_CONVERTER_VERSION!r} footer metadata — required for "
            "forensic provenance (parser identity)."
        )

    try:
        raw_files = json.loads(meta.get(META_ORIGINAL_FILES, ""))
        original_files = tuple(
            OriginalFile(
                name=str(entry["name"]),
                sha256=str(entry["sha256"]),
                size_bytes=int(entry["size_bytes"]),
            )
            for entry in raw_files
        )
    except (ValueError, TypeError, KeyError) as exc:
        raise ValueError(
            f"Vestigo Parquet file has missing or malformed "
            f"{META_ORIGINAL_FILES!r} footer metadata — required for forensic "
            "provenance (sha256 of the original evidence files)."
        ) from exc
    if not original_files:
        raise ValueError(
            f"Vestigo Parquet file lists no original evidence files in "
            f"{META_ORIGINAL_FILES!r} footer metadata."
        )

    missing = [name for name in PARQUET_EVENT_SCHEMA.names if schema.get_field_index(name) < 0]
    if missing:
        raise ValueError(f"Vestigo Parquet file is missing required columns: {', '.join(missing)}.")
    for name in PARQUET_EVENT_SCHEMA.names:
        expected = PARQUET_EVENT_SCHEMA.field(name).type
        actual = schema.field(name).type
        if actual != expected:
            raise ValueError(
                f"Vestigo Parquet column {name!r} has type {actual}, expected {expected}."
            )

    return ParquetSourceMeta(
        converter_name=converter_name,
        converter_version=converter_version,
        original_files=original_files,
    )
