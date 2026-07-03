"""Core event and provenance models for TraceSignal.

All identifiers, hashing, and serialization choices are designed for
forensic reproducibility: given the same source file, parser, and
embedding configuration, ingestion should produce the same event and
vector identities.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import warnings
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True, slots=True)
class ParserConfig:
    """Immutable parser configuration used for provenance tracking."""

    name: str
    version: str
    options: dict[str, Any] = field(default_factory=dict)

    def config_hash(self) -> str:
        """Return a SHA-256 hex hash of this parser configuration."""
        canonical = json.dumps(
            {"name": self.name, "version": self.version, "options": self.options},
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class EmbeddingConfig:
    """Immutable embedding configuration used for provenance tracking.

    ``field_config_hash`` captures the per-source field selection chosen by the
    analyst via the embedding wizard.  A different field selection produces a
    different hash and therefore lands in a separate Qdrant collection, keeping
    embeddings from different configurations isolated.
    """

    model_name: str
    device: str = "cpu"
    vector_dimension: int | None = None
    normalize: bool = True
    pooling: str = "mean"
    # SHA-256 of the canonical JSON of the per-source field config chosen by
    # the analyst.  Empty string when no custom config was supplied (legacy
    # all-fields behaviour).
    field_config_hash: str = ""

    def config_hash(self) -> str:
        """Return a SHA-256 hex hash of this embedding configuration.

        Includes ``field_config_hash`` so that different field selections
        always produce different collection names.
        """
        canonical = json.dumps(
            {
                "model_name": self.model_name,
                "device": self.device,
                "vector_dimension": self.vector_dimension,
                "normalize": self.normalize,
                "pooling": self.pooling,
                "field_config_hash": self.field_config_hash,
            },
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def as_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary representation."""
        return {
            "model_name": self.model_name,
            "device": self.device,
            "vector_dimension": self.vector_dimension,
            "normalize": self.normalize,
            "pooling": self.pooling,
            "field_config_hash": self.field_config_hash,
        }


@dataclass(slots=True)
class Event:
    """A single forensic event produced by a parser.

    Attributes:
        event_id: Deterministic UUIDv5 derived from case, source, file hash,
            byte offset, and content hash.
        case_id: Investigation case identifier.
        source_id: Source identifier (one ingested file) within the case.
        source_file: Original source file identifier (filename or path) for
            display and provenance. Not used in event identity calculation.
        byte_offset: Byte offset in the source file where the raw record starts.
        content_hash: SHA-256 hex digest of the canonical raw content.
        file_hash: SHA-256 hex digest of the whole source file.
        parser_name: Name of the parser that produced this event.
        parser_version: Version/hash of the parser configuration.
        ingest_time: UTC timestamp when the event was ingested.
        raw_line: Original, unmodified source line or record bytes-as-text.
        message: Human-readable event message.
        timestamp: Optional event timestamp (ISO 8601 string).
        timestamp_desc: Description of what ``timestamp`` represents.
        artifact: Short artifact name (renamed from ``source``).
        artifact_long: Long artifact name (renamed from ``source_long``).
        display_name: Display name of the source artifact.
        tags: List of tags attached by the parser.
        attributes: Additional format-specific fields.
        embedding_model: Name of the embedding model.
        embedding_config_hash: Hash of the embedding configuration.
        vector_id: Identifier used for the vector record (same as event_id).
    """

    case_id: str
    source_id: str
    source_file: Path
    byte_offset: int
    content_hash: str
    parser_name: str
    parser_version: str
    raw_line: str
    message: str
    file_hash: str = ""
    line_number: int | None = None
    ingest_time: datetime = field(default_factory=lambda: datetime.now(UTC))
    timestamp: str | None = None
    timestamp_desc: str | None = None
    artifact: str | None = None
    artifact_long: str | None = None
    display_name: str | None = None
    tags: list[str] = field(default_factory=list)
    attributes: dict[str, Any] = field(default_factory=dict)
    embedding_model: str | None = None
    embedding_config_hash: str | None = None
    event_id: uuid.UUID | None = field(default=None, init=False)
    vector_id: str | None = field(default=None, init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", self._derive_id())
        object.__setattr__(self, "vector_id", str(self.event_id))

    def _derive_id(self) -> uuid.UUID:
        """Derive a deterministic UUIDv5 for this event.

        Identity is based on the file-level hash when available, not the
        transient path where the file happened to be stored during ingestion.
        This makes re-uploads of the same source file produce identical event
        IDs. A missing file hash is a forensic error; callers should ensure a
        real hash is supplied.
        """
        namespace = uuid.uuid5(uuid.NAMESPACE_URL, f"tracesignal:{self.case_id}")
        source_identity = (
            self.file_hash if self.file_hash else self.source_file.resolve().as_posix()
        )
        digest_input = (
            f"{self.source_id}\n"
            f"{source_identity}\n"
            f"{self.byte_offset}\n"
            f"{self.content_hash}\n"
            f"{self.parser_name}\n"
            f"{self.parser_version}"
        )
        return uuid.uuid5(namespace, digest_input)

    def canonical_content(self) -> str:
        """Return the canonical content used for hashing and embedding."""
        return self.raw_line

    def text_for_embedding(
        self,
        artifact_fields: dict[str, list[str]] | None = None,
    ) -> str:
        """Build a single text representation for embedding.

        Delegates to the canonical implementation in
        ``tracesignal.ingestion.pipeline._text_for_embedding`` using a transient
        dict row derived from this event.  Passing ``artifact_fields`` applies the
        same analyst-defined per-artifact field selection used by the pipeline;
        omitting it falls back to the legacy all-fields behaviour.

        Falls back to ``self.raw_line`` when the result would otherwise be empty.
        """
        from tracesignal.ingestion.pipeline import _text_for_embedding

        row: dict[str, Any] = {
            "message": self.message,
            "timestamp": self.timestamp,
            "timestamp_desc": self.timestamp_desc,
            "artifact": self.artifact,
            "artifact_long": self.artifact_long,
            "display_name": self.display_name,
            "tags": self.tags,
            "attributes": self.attributes,
        }
        result = _text_for_embedding(row, artifact_fields)
        return result if result else self.raw_line

    def to_clickhouse_row(self) -> dict[str, Any]:
        """Serialize to a ClickHouse-ready row dictionary."""
        parsed_ts = _parse_timestamp(self.timestamp)
        return {
            "event_id": str(self.event_id),
            "case_id": self.case_id,
            "source_id": self.source_id,
            "source_file": str(self.source_file),
            "byte_offset": self.byte_offset,
            "line_number": self.line_number if self.line_number is not None else 0,
            "content_hash": self.content_hash,
            "file_hash": self.file_hash,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "ingest_time": self.ingest_time,
            "message": self.message,
            "timestamp": parsed_ts if parsed_ts is not None else None,
            "timestamp_desc": self.timestamp_desc or "",
            "artifact": self.artifact or "",
            "artifact_long": self.artifact_long or "",
            "display_name": self.display_name or "",
            "tags": self.tags,
            "attributes": {str(k): str(v) for k, v in self.attributes.items()},
            "embedding_model": self.embedding_model or "",
            "embedding_config_hash": self.embedding_config_hash or "",
            "vector_id": self.vector_id or "",
        }

    def to_qdrant_payload(self) -> dict[str, Any]:
        """Serialize to a Qdrant payload dictionary."""
        return {
            "event_id": str(self.event_id),
            "case_id": self.case_id,
            "source_id": self.source_id,
            "source_file": str(self.source_file),
            "byte_offset": self.byte_offset,
            "line_number": self.line_number,
            "content_hash": self.content_hash,
            "file_hash": self.file_hash,
            "parser_name": self.parser_name,
            "parser_version": self.parser_version,
            "message": self.message,
            "timestamp": self.timestamp,
            "timestamp_desc": self.timestamp_desc,
            "artifact": self.artifact,
            "artifact_long": self.artifact_long,
            "display_name": self.display_name,
            "tags": self.tags,
            "embedding_model": self.embedding_model,
            "embedding_config_hash": self.embedding_config_hash,
        }

    def as_dict(self) -> dict[str, Any]:
        """Return a full serializable dictionary representation."""
        data = asdict(self)
        data["event_id"] = str(self.event_id)
        data["vector_id"] = self.vector_id
        data["source_file"] = str(self.source_file)
        return data


def _parse_timestamp(value: str | int | float | datetime | None) -> datetime | None:
    """Parse a forensic timestamp into a timezone-aware datetime.

    Accepts ISO-8601 strings, common ``YYYY-MM-DD HH:MM:SS`` forms, and
    Unix epoch integers/strings in seconds, milliseconds, or microseconds.
    Returns ``None`` when the value cannot be parsed.

    Naive datetimes and unqualified string timestamps are assumed to be UTC
    with a warning, per the model refinement requirements.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            warnings.warn(
                f"Naive timestamp {value!r} assumed UTC; "
                "configure per-source timezone when available.",
                stacklevel=2,
            )
            return value.replace(tzinfo=UTC)
        return value
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value, tz=UTC)

    s = str(value).strip()
    if not s:
        return None

    # ISO-8601 (python's fromisoformat does not accept trailing Z before 3.11).
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            warnings.warn(
                f"Naive timestamp {s!r} assumed UTC; configure per-source timezone when available.",
                stacklevel=2,
            )
            return dt.replace(tzinfo=UTC)
        return dt
    except ValueError:
        pass

    # Common absolute datetime formats.
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            warnings.warn(
                f"Unqualified timestamp {s!r} assumed UTC; "
                "configure per-source timezone when available.",
                stacklevel=2,
            )
            return dt.replace(tzinfo=UTC)
        except ValueError:
            pass

    # Epoch seconds / milliseconds / microseconds as a numeric string.
    try:
        n = int(s)
        length = len(s)
        if length == 10:
            return datetime.fromtimestamp(n, tz=UTC)
        if length == 13:
            return datetime.fromtimestamp(n / 1000, tz=UTC)
        if length in (16, 17):
            return datetime.fromtimestamp(n / 1_000_000, tz=UTC)
    except ValueError:
        pass

    return None


def content_hash(content: str | bytes) -> str:
    """Return the SHA-256 hex digest of ``content``."""
    if isinstance(content, str):
        return hashlib.sha256(content.encode("utf-8")).hexdigest()
    return hashlib.sha256(content).hexdigest()
