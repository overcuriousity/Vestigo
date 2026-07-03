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
    source_id: str
    files: list[Path] = field(default_factory=list)
    events_parsed: int = 0
    events_inserted: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        """Return a human-readable summary."""
        file_list = ", ".join(str(p) for p in self.files) or "none"
        return (
            f"Ingested {self.events_inserted} events "
            f"into case '{self.case_id}' / source '{self.source_id}' "
            f"from {file_list}"
        )


@dataclass
class EmbeddingResult:
    """Result of an embedding run."""

    case_id: str
    source_ids: list[str]
    events_processed: int = 0
    vectors_inserted: int = 0
    errors: list[str] = field(default_factory=list)
    # Full config hash used for the Qdrant collection; set by EmbeddingPipeline.run().
    config_hash: str = ""

    def summary(self) -> str:
        """Return a human-readable summary."""
        return (
            f"Embedded {self.events_processed} events "
            f"({self.vectors_inserted} vectors) "
            f"into case '{self.case_id}' / sources {self.source_ids}"
        )


class IngestionPipeline:
    """Event ingestion pipeline: parse and persist events to ClickHouse.

    This pipeline deliberately does *not* compute embeddings, so uploads
    complete quickly and users can start investigating immediately.
    """

    def __init__(
        self,
        case_id: str,
        source_id: str,
        clickhouse: ClickHouseStore | None = None,
        batch_size: int | None = None,
        file_hash: str | None = None,
        source_name: str | None = None,
        progress_callback: Any | None = None,
    ) -> None:
        self.case_id = case_id
        self.source_id = source_id
        self.clickhouse = clickhouse or ClickHouseStore()
        self.batch_size = batch_size or get_settings().embedding_batch_size
        self.file_hash = file_hash
        self.source_name = source_name
        # Called as progress_callback(total=..., processed=...) with *bytes*
        # (event totals are unknown until parsing finishes) — same keyword
        # shape as EmbeddingPipeline's callback.
        self.progress_callback = progress_callback

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
            source_id=self.source_id,
            files=files,
        )

        self.clickhouse.init_schema()

        total_bytes = sum(f.stat().st_size for f in files)
        bytes_done = 0
        self._report_progress(total=total_bytes, processed=0)

        first_exception: BaseException | None = None
        single_file = len(files) == 1
        for file_path in files:
            fmt = format_name or detect_format(file_path)
            # Use the caller-supplied file hash for a single-file upload; for
            # directory ingestion compute a per-file hash.
            file_hash = self.file_hash if single_file else hash_file(file_path)
            source_name = self.source_name if single_file else file_path.name
            if not file_hash:
                raise ValueError(
                    f"Could not compute file hash for {file_path}; "
                    "ingestion requires a file-level hash for forensic integrity."
                )
            parser = get_parser(
                fmt,
                self.case_id,
                self.source_id,
                file_hash=file_hash,
                source_name=source_name or file_path.name,
            )
            try:
                self._ingest_file(file_path, parser, result, total_bytes, bytes_done)
            except Exception as exc:  # noqa: BLE001
                if first_exception is None:
                    first_exception = exc
                result.errors.append(f"{file_path}: {exc}\n{traceback.format_exc()}")
            bytes_done += file_path.stat().st_size
            self._report_progress(total=total_bytes, processed=bytes_done)

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
        total_bytes: int = 0,
        bytes_before_file: int = 0,
    ) -> None:
        """Stream a single file into ClickHouse in batches.

        Progress is reported per flushed batch as bytes consumed, using the
        last event's ``byte_offset`` within the current file on top of the
        bytes of already-completed files.
        """
        batch: list[Event] = []

        for event in parser.parse(file_path):
            batch.append(event)
            result.events_parsed += 1

            if len(batch) >= self.batch_size:
                inserted = self.clickhouse.insert_events(batch)
                result.events_inserted += inserted
                self._report_progress(
                    total=total_bytes,
                    processed=bytes_before_file + batch[-1].byte_offset,
                )
                batch = []

        if batch:
            inserted = self.clickhouse.insert_events(batch)
            result.events_inserted += inserted
            self._report_progress(
                total=total_bytes,
                processed=bytes_before_file + batch[-1].byte_offset,
            )

    def _report_progress(self, total: int, processed: int) -> None:
        if self.progress_callback is not None:
            self.progress_callback(total=total, processed=processed)


class EmbeddingPipeline:
    """Background embedding pipeline: read events from ClickHouse, embed,
    and write vectors to Qdrant.

    ``field_config`` is the analyst-defined per-artifact field selection
    (shape: ``{"version": 1, "artifacts": {"<artifact>": ["message", "attr:k"]}}``)
    produced by the embedding wizard.  When ``None``, the legacy all-fields
    behaviour applies and all artifacts are embedded.  When supplied, only
    artifacts listed in ``field_config["artifacts"]`` are embedded, and only
    the selected fields are used to build the embedding text.
    """

    def __init__(
        self,
        case_id: str,
        source_ids: list[str],
        embedding_model: EmbeddingModel | None = None,
        clickhouse: ClickHouseStore | None = None,
        qdrant: QdrantStore | None = None,
        batch_size: int | None = None,
        progress_callback: Any | None = None,
        field_config: dict[str, Any] | None = None,
    ) -> None:
        self.case_id = case_id
        self.source_ids = source_ids
        self.embedding_model = embedding_model or EmbeddingModel()
        self.clickhouse = clickhouse or ClickHouseStore()
        self.qdrant = qdrant or QdrantStore()
        self.batch_size = batch_size or get_settings().embedding_batch_size
        self.progress_callback = progress_callback
        self.field_config = field_config  # None → legacy all-fields

    def run(self) -> EmbeddingResult:
        """Generate embeddings for all events of the configured sources."""
        result = EmbeddingResult(
            case_id=self.case_id,
            source_ids=self.source_ids,
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
        config_hash = config.config_hash()
        result.config_hash = config_hash
        self.qdrant.init_collection(
            case_id=self.case_id,
            embedding_config_hash=config_hash,
            vector_size=base_config.vector_dimension or self.embedding_model.vector_dimension(),
        )

        total = sum(
            self.clickhouse.count_events(
                case_id=self.case_id,
                source_id=source_id,
            )
            for source_id in self.source_ids
        )

        if total == 0:
            return result

        self._report_progress(total=total, processed=0)

        first_exception: BaseException | None = None
        processed = 0
        for source_id in self.source_ids:
            offset = 0
            while True:
                try:
                    batch = self.clickhouse.list_events(
                        case_id=self.case_id,
                        source_id=source_id,
                        limit=self.batch_size,
                        offset=offset,
                    )
                except Exception as exc:  # noqa: BLE001
                    error = (
                        f"Failed to read events for source {source_id} "
                        f"at offset {offset}: {exc}\n{traceback.format_exc()}"
                    )
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
                    error = (
                        f"Failed to embed batch for source {source_id} "
                        f"at offset {offset}: {exc}\n{traceback.format_exc()}"
                    )
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

        When ``self.field_config`` is set, rows whose ``artifact`` is not listed
        in the config are silently skipped (not embedded).  This allows the
        analyst to exclude noisy artifacts entirely.
        """
        artifact_fields: dict[str, list[str]] | None = None
        if self.field_config is not None:
            artifact_fields = self.field_config.get("artifacts", {})

        # Filter out rows for unconfigured artifacts when a field config is set.
        if artifact_fields is not None:
            batch = [row for row in batch if (row.get("artifact") or "") in artifact_fields]

        if not batch:
            return 0

        texts = [_text_for_embedding(row, artifact_fields) for row in batch]
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
    artifact_fields: dict[str, list[str]] | None = None,
) -> str:
    """Build a single text representation for embedding from a stored event row.

    ``artifact_fields`` maps each artifact name to the list of field tokens chosen
    by the analyst in the embedding wizard.  Field tokens are either plain
    top-level column names (``"message"``, ``"display_name"``, …) or
    ``"attr:<key>"`` for entries in the ``attributes`` map.

    When ``artifact_fields`` is ``None`` (legacy / no config), the original
    all-fields behaviour is preserved: every non-empty field is included.
    """
    parts: list[str] = []
    attributes = row.get("attributes") or {}
    message = row.get("message")

    if artifact_fields is not None:
        artifact = row.get("artifact") or ""
        selected = artifact_fields.get(artifact, [])
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
        artifact = row.get("artifact")
        if artifact:
            parts.append(f"artifact={artifact}")
        artifact_long = row.get("artifact_long")
        if artifact_long:
            parts.append(f"artifact_long={artifact_long}")
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
        "source_id": row.get("source_id"),
        "source_file": str(row.get("source_file", "")),
        "byte_offset": row.get("byte_offset"),
        "line_number": row.get("line_number"),
        "content_hash": row.get("content_hash"),
        "file_hash": row.get("file_hash"),
        "parser_name": row.get("parser_name"),
        "parser_version": row.get("parser_version"),
        "message": row.get("message"),
        "timestamp": row.get("timestamp"),
        "timestamp_desc": row.get("timestamp_desc"),
        "artifact": row.get("artifact"),
        "artifact_long": row.get("artifact_long"),
        "display_name": row.get("display_name"),
        "tags": row.get("tags"),
        "embedding_model": row.get("embedding_model"),
        "embedding_config_hash": row.get("embedding_config_hash"),
    }
