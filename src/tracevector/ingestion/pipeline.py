"""Ingestion and embedding pipelines.

Upload ingestion only parses and writes events to ClickHouse so users can
browse data immediately. Vector embedding is a separate, user-triggered
background job.
"""

from __future__ import annotations

import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from qdrant_client.models import PointStruct

from tracevector.core.config import get_settings
from tracevector.db.clickhouse import ClickHouseStore
from tracevector.db.qdrant import QdrantStore
from tracevector.ingestion.files import hash_file
from tracevector.ingestion.parser import Parser, detect_format, get_parser
from tracevector.models.embeddings import EmbeddingModel
from tracevector.models.event import Event


@dataclass
class IngestionResult:
    """Result of an event-ingestion run."""

    case_id: str
    timeline_id: str
    files: list[Path] = field(default_factory=list)
    events_parsed: int = 0
    events_inserted: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Return a human-readable summary."""
        file_list = ", ".join(str(p) for p in self.files) or "none"
        return (
            f"Ingested {self.events_inserted} events "
            f"into case '{self.case_id}' / timeline '{self.timeline_id}' "
            f"from {file_list}"
        )


@dataclass
class EmbeddingResult:
    """Result of an embedding run."""

    case_id: str
    timeline_id: str
    events_processed: int = 0
    vectors_inserted: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Return a human-readable summary."""
        return (
            f"Embedded {self.events_processed} events "
            f"({self.vectors_inserted} vectors) "
            f"into case '{self.case_id}' / timeline '{self.timeline_id}'"
        )


class IngestionPipeline:
    """Event ingestion pipeline: parse and persist events to ClickHouse.

    This pipeline deliberately does *not* compute embeddings, so uploads
    complete quickly and users can start investigating immediately.
    """

    def __init__(
        self,
        case_id: str,
        timeline_id: str,
        clickhouse: ClickHouseStore | None = None,
        batch_size: int | None = None,
        file_hash: str | None = None,
        source_name: str | None = None,
    ) -> None:
        self.case_id = case_id
        self.timeline_id = timeline_id
        self.clickhouse = clickhouse or ClickHouseStore()
        self.batch_size = batch_size or get_settings().embedding_batch_size
        self.file_hash = file_hash
        self.source_name = source_name

    def run(
        self,
        path: Path,
        format_name: str | None = None,
    ) -> IngestionResult:
        """Run ingestion over ``path``.

        ``path`` may be a single file or a directory. Files are matched to the
        requested parser format; when ``format_name`` is ``None`` the format is
        inferred from the file extension.
        """
        path = path.resolve()
        files = self._resolve_files(path)
        result = IngestionResult(
            case_id=self.case_id,
            timeline_id=self.timeline_id,
            files=files,
        )

        self.clickhouse.init_schema()

        first_exception: BaseException | None = None
        single_file = len(files) == 1
        for file_path in files:
            fmt = format_name or detect_format(file_path)
            # Use the caller-supplied file hash for a single-file upload; for
            # directory ingestion compute a per-file hash.
            file_hash = self.file_hash if single_file else hash_file(file_path)
            source_name = self.source_name if single_file else file_path.name
            parser = get_parser(
                fmt,
                self.case_id,
                self.timeline_id,
                file_hash=file_hash,
                source_name=source_name or file_path.name,
            )
            try:
                self._ingest_file(file_path, parser, result)
            except Exception as exc:  # noqa: BLE001
                if first_exception is None:
                    first_exception = exc
                result.errors.append(f"{file_path}: {exc}\n{traceback.format_exc()}")

        if result.errors:
            message = "Ingestion failed:\n" + "\n".join(result.errors)
            raise RuntimeError(message) from first_exception

        return result

    def _resolve_files(self, path: Path) -> list[Path]:
        """Return the list of source files to ingest."""
        if path.is_file():
            return [path]
        if path.is_dir():
            return sorted(p for p in path.rglob("*") if p.is_file())
        raise FileNotFoundError(f"Ingestion path not found: {path}")

    def _ingest_file(
        self,
        file_path: Path,
        parser: Parser,
        result: IngestionResult,
    ) -> None:
        """Stream a single file into ClickHouse in batches."""
        batch: list[Event] = []

        for event in parser.parse(file_path):
            batch.append(event)
            result.events_parsed += 1

            if len(batch) >= self.batch_size:
                inserted = self.clickhouse.insert_events(batch)
                result.events_inserted += inserted
                batch = []

        if batch:
            inserted = self.clickhouse.insert_events(batch)
            result.events_inserted += inserted


class EmbeddingPipeline:
    """Background embedding pipeline: read events from ClickHouse, embed,
    and write vectors to Qdrant.

    ``field_config`` is the analyst-defined per-source field selection
    (shape: ``{"version": 1, "sources": {"<source>": ["message", "attr:k"]}}``)
    produced by the embedding wizard.  When ``None``, the legacy all-fields
    behaviour applies and all sources are embedded.  When supplied, only sources
    listed in ``field_config["sources"]`` are embedded, and only the selected
    fields are used to build the embedding text.
    """

    def __init__(
        self,
        case_id: str,
        timeline_id: str,
        embedding_model: EmbeddingModel | None = None,
        clickhouse: ClickHouseStore | None = None,
        qdrant: QdrantStore | None = None,
        batch_size: int | None = None,
        progress_callback: Any | None = None,
        field_config: dict[str, Any] | None = None,
    ) -> None:
        self.case_id = case_id
        self.timeline_id = timeline_id
        self.embedding_model = embedding_model or EmbeddingModel()
        self.clickhouse = clickhouse or ClickHouseStore()
        self.qdrant = qdrant or QdrantStore()
        self.batch_size = batch_size or get_settings().embedding_batch_size
        self.progress_callback = progress_callback
        self.field_config = field_config  # None → legacy all-fields

    def run(self) -> EmbeddingResult:
        """Generate embeddings for all events of the configured timeline."""
        result = EmbeddingResult(
            case_id=self.case_id,
            timeline_id=self.timeline_id,
        )

        self.clickhouse.init_schema()
        # Build embedding config, incorporating the field-config hash so that
        # different analyst field selections land in distinct Qdrant collections.
        import hashlib as _hashlib  # local to avoid shadowing
        import json as _json  # local to avoid shadowing

        field_config_hash = ""
        if self.field_config is not None:
            canonical = _json.dumps(self.field_config, sort_keys=True, separators=(",", ":"))
            field_config_hash = _hashlib.sha256(canonical.encode()).hexdigest()
        from tracevector.models.event import EmbeddingConfig as _EC

        base_config = self.embedding_model.as_config()
        config = _EC(
            model_name=base_config.model_name,
            device=base_config.device,
            vector_dimension=base_config.vector_dimension,
            normalize=base_config.normalize,
            pooling=base_config.pooling,
            field_config_hash=field_config_hash,
        )
        self.qdrant.init_collection(
            case_id=self.case_id,
            embedding_config_hash=config.config_hash(),
            vector_size=base_config.vector_dimension or self.embedding_model.vector_dimension(),
        )

        total = self.clickhouse.count_events(
            case_id=self.case_id,
            timeline_id=self.timeline_id,
        )

        if total == 0:
            return result

        self._report_progress(total=total, processed=0)

        first_exception: BaseException | None = None
        processed = 0
        offset = 0
        while processed < total:
            try:
                batch = self.clickhouse.list_events(
                    case_id=self.case_id,
                    timeline_id=self.timeline_id,
                    limit=self.batch_size,
                    offset=offset,
                )
            except Exception as exc:  # noqa: BLE001
                error = f"Failed to read events at offset {offset}: {exc}\n{traceback.format_exc()}"
                result.errors.append(error)
                if first_exception is None:
                    first_exception = exc
                break

            if not batch:
                break

            try:
                vectors_inserted = self._embed_batch(batch, config)
                result.events_processed += len(batch)
                result.vectors_inserted += vectors_inserted
            except Exception as exc:  # noqa: BLE001
                error = f"Failed to embed batch at offset {offset}: {exc}\n{traceback.format_exc()}"
                result.errors.append(error)
                if first_exception is None:
                    first_exception = exc
                break

            processed += len(batch)
            offset += len(batch)
            self._report_progress(total=total, processed=processed)

        if result.errors:
            message = "Embedding failed:\n" + "\n".join(result.errors)
            raise RuntimeError(message) from first_exception

        return result

    def _embed_batch(
        self,
        batch: list[dict[str, Any]],
        config: Any,
    ) -> int:
        """Embed one batch and persist vectors to Qdrant.

        When ``self.field_config`` is set, rows whose ``source`` is not listed
        in the config are silently skipped (not embedded).  This allows the
        analyst to exclude noisy sources entirely.
        """
        source_fields: dict[str, list[str]] | None = None
        if self.field_config is not None:
            source_fields = self.field_config.get("sources", {})

        # Filter out rows for unconfigured sources when a field config is set.
        if source_fields is not None:
            batch = [row for row in batch if (row.get("source") or "") in source_fields]

        if not batch:
            return 0

        texts = [_text_for_embedding(row, source_fields) for row in batch]
        vectors = self.embedding_model.encode(texts)

        config_hash = config.config_hash()
        model_name = self.embedding_model.model_name
        points: list[PointStruct] = []
        for row, vector in zip(batch, vectors, strict=False):
            row["embedding_model"] = model_name
            row["embedding_config_hash"] = config_hash
            points.append(
                PointStruct(
                    id=row["event_id"],
                    vector=vector,
                    payload=_qdrant_payload(row),
                )
            )

        self.qdrant.upsert(
            self.qdrant.collection_name(self.case_id, config_hash),
            points,
        )
        return len(points)

    def _report_progress(self, total: int, processed: int) -> None:
        if self.progress_callback is not None:
            self.progress_callback(total=total, processed=processed)


def _text_for_embedding(
    row: dict[str, Any],
    source_fields: dict[str, list[str]] | None = None,
) -> str:
    """Build a single text representation for embedding from a stored event row.

    ``source_fields`` maps each source name to the list of field tokens chosen
    by the analyst in the embedding wizard.  Field tokens are either plain
    top-level column names (``"message"``, ``"display_name"``, …) or
    ``"attr:<key>"`` for entries in the ``attributes`` map.

    When ``source_fields`` is ``None`` (legacy / no config), the original
    all-fields behaviour is preserved: every non-empty field is included.
    """
    parts: list[str] = []
    attributes = row.get("attributes") or {}
    message = row.get("message")

    if source_fields is not None:
        source = row.get("source") or ""
        selected = source_fields.get(source, [])
        for token in selected:
            if token.startswith("attr:"):
                key = token[5:]
                value = attributes.get(key)
                if value is not None and value != "":
                    parts.append(f"{key}={value}")
            else:
                # top-level column
                if token == "message":
                    value = message
                    if value:
                        parts.append(str(value))
                elif token == "tags":
                    tags = row.get("tags") or []
                    if tags:
                        parts.append(f"tags={','.join(sorted(str(t) for t in tags))}")
                else:
                    value = row.get(token)
                    if value:
                        parts.append(f"{token}={value}")
    else:
        # Legacy: include every non-empty field.
        if message:
            parts.append(str(message))
        timestamp = row.get("timestamp")
        if timestamp:
            parts.append(f"time={timestamp}")
        timestamp_desc = row.get("timestamp_desc")
        if timestamp_desc:
            parts.append(f"time_desc={timestamp_desc}")
        source = row.get("source")
        if source:
            parts.append(f"source={source}")
        source_long = row.get("source_long")
        if source_long:
            parts.append(f"source_long={source_long}")
        display_name = row.get("display_name")
        if display_name:
            parts.append(f"display_name={display_name}")
        tags = row.get("tags") or []
        if tags:
            parts.append(f"tags={','.join(sorted(str(t) for t in tags))}")
        for key in sorted(attributes):
            value = attributes[key]
            if value is not None and value != "":
                parts.append(f"{key}={value}")

    if not parts:
        return str(message) if message else ""
    return " | ".join(parts)


def _qdrant_payload(row: dict[str, Any]) -> dict[str, Any]:
    """Serialize a stored event row to a Qdrant payload dictionary."""
    return {
        "event_id": str(row.get("event_id")),
        "case_id": row.get("case_id"),
        "timeline_id": row.get("timeline_id"),
        "source_file": str(row.get("source_file", "")),
        "byte_offset": row.get("byte_offset"),
        "line_number": row.get("line_number"),
        "content_hash": row.get("content_hash"),
        "parser_name": row.get("parser_name"),
        "parser_version": row.get("parser_version"),
        "message": row.get("message"),
        "timestamp": row.get("timestamp"),
        "timestamp_desc": row.get("timestamp_desc"),
        "source": row.get("source"),
        "source_long": row.get("source_long"),
        "display_name": row.get("display_name"),
        "tags": row.get("tags"),
        "embedding_model": row.get("embedding_model"),
        "embedding_config_hash": row.get("embedding_config_hash"),
    }
