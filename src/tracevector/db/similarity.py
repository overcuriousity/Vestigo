"""Vector-backed similarity search.

Finds events semantically similar to a query event using stored Qdrant vectors.

Statistical anomaly detection (value novelty, frequency spikes) lives in
:mod:`tracevector.db.anomaly_stats` and operates on ClickHouse fields without
requiring embeddings.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from tracevector.db.clickhouse import ClickHouseStore  # noqa: I001
from tracevector.db.qdrant import QdrantStore

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
    results: list[SimilarResult]

    def __init__(self, status: str, results: list[SimilarResult] | None = None) -> None:
        self.status = status
        self.results = results if results is not None else []


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
    ts = payload.get("timestamp")
    if ts is not None and not isinstance(ts, str):
        try:
            ts = ts.isoformat()
        except AttributeError:
            ts = str(ts)
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
    ts = row.get("timestamp")
    if ts is not None and not isinstance(ts, str):
        try:
            ts = ts.isoformat()
        except AttributeError:
            ts = str(ts)
    ingest = row.get("ingest_time")
    if ingest is not None and not isinstance(ingest, str):
        try:
            ingest = ingest.isoformat()
        except AttributeError:
            ingest = str(ingest)
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
    ) -> None:
        self.qdrant = qdrant or QdrantStore()
        self.clickhouse = clickhouse or ClickHouseStore()

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
        collection = self.qdrant.find_timeline_collection(case_id, source_ids)
        if collection is None:
            return SimilaritySearchResult(status="not_embedded")

        query_vector = self.qdrant.retrieve_vector(collection, event_id)
        if query_vector is None:
            return SimilaritySearchResult(status="vector_not_found")

        # Fetch limit+1 and drop the query event itself.
        hits = self.qdrant.search(
            collection_name=collection,
            query_vector=query_vector,
            source_ids=source_ids,
            limit=limit + 1,
            with_vectors=False,
        )
        hits = [h for h in hits if str(h.id) != event_id][:limit]

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
