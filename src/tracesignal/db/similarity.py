"""Vector-backed similarity search.

Finds events semantically similar to a query event using stored Qdrant vectors.

Statistical anomaly detection (value novelty, frequency spikes) lives in
:mod:`tracesignal.db.anomaly_stats` and operates on ClickHouse fields without
requiring embeddings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from tracesignal.db._dt import ensure_utc_iso
from tracesignal.db.clickhouse import ClickHouseStore  # noqa: I001
from tracesignal.db.qdrant import QdrantStore
from tracesignal.models.embeddings import EmbeddingModel

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class SimilarResult:
    """One result returned by :py:meth:`SimilarityService.find_similar`."""

    event_id: str
    score: float
    event: dict[str, Any]


@dataclass
class SimilaritySearchResult:
    """Return value of :py:meth:`SimilarityService.find_similar`."""

    status: str  # "ok" | "not_embedded" | "vector_not_found"
    results: list[SimilarResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cosine_distance(a: np.ndarray, b: np.ndarray) -> float:
    """Return cosine distance (0 = identical, 2 = opposite) between unit vectors.

    Both ``a`` and ``b`` are assumed to be L2-normalised (as stored by the
    embedding pipeline with ``normalize_embeddings=True``).  For unit vectors
    cosine distance simplifies to ``1 - dot(a, b)``, which avoids recomputing
    norms.
    """
    dot = float(np.dot(a, b))
    dot = max(-1.0, min(1.0, dot))
    return 1.0 - dot


def _l2_normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """Return ``matrix`` with each row scaled to unit L2 norm.

    Zero-norm rows are left unchanged.
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def _payload_to_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a Qdrant point payload into a minimal EventRecord-compatible dict."""
    ts = ensure_utc_iso(payload.get("timestamp"))
    return {
        "event_id": payload.get("event_id", ""),
        "case_id": payload.get("case_id", ""),
        "source_id": payload.get("source_id", ""),
        "message": payload.get("message", ""),
        "timestamp": ts,
        "timestamp_desc": payload.get("timestamp_desc", ""),
        "artifact": payload.get("artifact", ""),
        "artifact_long": payload.get("artifact_long", ""),
        "display_name": payload.get("display_name", ""),
        "tags": payload.get("tags") or [],
        "attributes": {},
        "source_file": payload.get("source_file", ""),
        "byte_offset": payload.get("byte_offset"),
        "line_number": payload.get("line_number"),
        "content_hash": payload.get("content_hash", ""),
        "file_hash": payload.get("file_hash", ""),
        "parser_name": payload.get("parser_name", ""),
        "parser_version": payload.get("parser_version", ""),
        "embedding_model": payload.get("embedding_model", ""),
        "embedding_config_hash": payload.get("embedding_config_hash", ""),
        "vector_id": payload.get("event_id", ""),
        "ingest_time": None,
    }


def _row_to_event(row: dict[str, Any]) -> dict[str, Any]:
    """Serialise a ClickHouse row to an EventRecord-compatible dict."""
    ts = ensure_utc_iso(row.get("timestamp"))
    ingest = ensure_utc_iso(row.get("ingest_time"))
    return {
        "event_id": str(row.get("event_id", "")),
        "case_id": row.get("case_id", ""),
        "source_id": row.get("source_id", ""),
        "message": row.get("message", ""),
        "timestamp": ts,
        "timestamp_desc": row.get("timestamp_desc", ""),
        "artifact": row.get("artifact", ""),
        "artifact_long": row.get("artifact_long", ""),
        "display_name": row.get("display_name", ""),
        "tags": row.get("tags") or [],
        "attributes": row.get("attributes") or {},
        "source_file": str(row.get("source_file", "")),
        "byte_offset": row.get("byte_offset"),
        "line_number": row.get("line_number"),
        "content_hash": row.get("content_hash", ""),
        "file_hash": row.get("file_hash", ""),
        "parser_name": row.get("parser_name", ""),
        "parser_version": row.get("parser_version", ""),
        "embedding_model": row.get("embedding_model", ""),
        "embedding_config_hash": row.get("embedding_config_hash", ""),
        "vector_id": row.get("vector_id", ""),
        "ingest_time": ingest,
    }


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class SimilarityService:
    """Semantic similarity search backed by Qdrant + ClickHouse."""

    def __init__(
        self,
        qdrant: QdrantStore | None = None,
        clickhouse: ClickHouseStore | None = None,
        embedding_model: EmbeddingModel | None = None,
    ) -> None:
        self.qdrant = qdrant or QdrantStore()
        self.clickhouse = clickhouse or ClickHouseStore()
        # Lazily loaded on first free-text query so anchor-event searches
        # (the common case) never pay the model-load cost.
        self._embedding_model = embedding_model

    def _get_embedding_model(self) -> EmbeddingModel:
        if self._embedding_model is None:
            self._embedding_model = EmbeddingModel()
        return self._embedding_model

    def find_similar(
        self,
        case_id: str,
        source_ids: list[str],
        event_id: str,
        limit: int = 10,
    ) -> SimilaritySearchResult:
        """Return the ``limit`` events most semantically similar to ``event_id``.

        The query event itself is excluded from results.  Scores are cosine
        similarity (0–1; higher = more similar).

        Returns ``status="not_embedded"`` when the sources have no vectors, or
        ``status="vector_not_found"`` when the specific event has no vector.
        """
        collection = self.qdrant.find_collection_for_sources(case_id, source_ids)
        if collection is None:
            return SimilaritySearchResult(status="not_embedded")

        query_vector = self.qdrant.retrieve_vector(collection, event_id)
        if query_vector is None:
            return SimilaritySearchResult(status="vector_not_found")

        return self._search_and_hydrate(
            case_id,
            collection,
            query_vector,
            source_ids,
            limit,
            exclude_event_id=event_id,
        )

    def find_similar_by_text(
        self,
        case_id: str,
        source_ids: list[str],
        query: str,
        limit: int = 10,
    ) -> SimilaritySearchResult:
        """Return the ``limit`` events most semantically similar to free-text ``query``.

        The query text is embedded with the same model used at ingest time.
        Returns ``status="not_embedded"`` when the sources have no vectors.
        """
        collection = self.qdrant.find_collection_for_sources(case_id, source_ids)
        if collection is None:
            return SimilaritySearchResult(status="not_embedded")

        query_vector = self._get_embedding_model().encode([query])[0]
        return self._search_and_hydrate(case_id, collection, query_vector, source_ids, limit)

    def _search_and_hydrate(
        self,
        case_id: str,
        collection: str,
        query_vector: list[float],
        source_ids: list[str],
        limit: int,
        exclude_event_id: str | None = None,
    ) -> SimilaritySearchResult:
        """Run a vector search and hydrate hits into event dicts.

        Shared by :py:meth:`find_similar` (anchor-event query) and
        :py:meth:`find_similar_by_text` (free-text query).
        """
        # Fetch limit+1 so dropping the anchor event (if any) still leaves `limit` results.
        hits = self.qdrant.search(
            collection_name=collection,
            query_vector=query_vector,
            source_ids=source_ids,
            limit=limit + 1,
            with_vectors=False,
        )
        if exclude_event_id is not None:
            hits = [h for h in hits if str(h.id) != exclude_event_id]
        hits = hits[:limit]

        if not hits:
            return SimilaritySearchResult(status="ok", results=[])

        event_ids = [str(h.id) for h in hits]
        ch_rows = self.clickhouse.get_events_by_ids(case_id, source_ids, event_ids)

        results: list[SimilarResult] = []
        for hit in hits:
            eid = str(hit.id)
            score = round(float(hit.score), 6)
            if eid in ch_rows:
                event = _row_to_event(ch_rows[eid])
            else:
                payload = hit.payload or {}
                event = _payload_to_event(payload)
            results.append(SimilarResult(event_id=eid, score=score, event=event))

        return SimilaritySearchResult(status="ok", results=results)
