"""Vector-backed outlier detection and similarity search.

Algorithm — distance-to-centroid (scalable, O(1) ANN queries):

1. Discover which Qdrant collection holds the timeline's vectors.
   Return ``status="not_embedded"`` when none exist.
2. Scroll up to ``sample_size`` points to compute an approximate centroid
   of the timeline's embedding space.  On huge timelines this is a
   representative sample; on small ones it covers everything.
3. Query Qdrant for the nearest points to the *negated* centroid.
   For COSINE collections, closest to ``-centroid`` == farthest from
   ``centroid`` == most unlike the bulk == candidate outliers.
4. Recompute exact cosine distance for each result and sort descending.
5. Hydrate full event records from ClickHouse; fall back to the Qdrant
   payload for any event_id not found in ClickHouse.

For similarity search:
1. Retrieve the stored vector for the query event_id from Qdrant.
2. Query for the K+1 nearest neighbours (timeline-filtered).
3. Drop the query event itself; return the rest with cosine similarity scores.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

from tracevector.db.clickhouse import ClickHouseStore  # noqa: I001
from tracevector.db.qdrant import QdrantStore

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class OutlierResult:
    """One outlier returned by :py:meth:`SimilarityService.find_anomalies`."""

    event_id: str
    score: float
    event: dict[str, Any]
    details: dict[str, Any]


@dataclass
class SimilarResult:
    """One result returned by :py:meth:`SimilarityService.find_similar`."""

    event_id: str
    score: float
    event: dict[str, Any]


@dataclass
class AnomalyResult:
    """Return value of :py:meth:`SimilarityService.find_anomalies`."""

    status: str  # "ok" | "not_embedded" | "insufficient_vectors"
    results: list[OutlierResult] = field(default_factory=list)
    sample_size: int = 0
    embedding_config_hash: str = ""


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
    # Clamp to [-1, 1] for numerical safety.
    dot = max(-1.0, min(1.0, dot))
    return 1.0 - dot


def _payload_to_event(payload: dict[str, Any]) -> dict[str, Any]:
    """Convert a Qdrant point payload into a minimal EventRecord-compatible dict."""
    # The pipeline stores timestamps as datetime objects in some versions;
    # convert to ISO string so the frontend can deserialise uniformly.
    ts = payload.get("timestamp")
    if ts is not None and not isinstance(ts, str):
        try:
            ts = ts.isoformat()
        except AttributeError:
            ts = str(ts)
    return {
        "event_id": payload.get("event_id", ""),
        "case_id": payload.get("case_id", ""),
        "timeline_id": payload.get("timeline_id", ""),
        "message": payload.get("message", ""),
        "timestamp": ts,
        "timestamp_desc": payload.get("timestamp_desc", ""),
        "source": payload.get("source", ""),
        "source_long": payload.get("source_long", ""),
        "display_name": payload.get("display_name", ""),
        "tags": payload.get("tags") or [],
        "attributes": {},
        # Provenance
        "source_file": payload.get("source_file", ""),
        "byte_offset": payload.get("byte_offset"),
        "line_number": payload.get("line_number"),
        "content_hash": payload.get("content_hash", ""),
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
        "timeline_id": row.get("timeline_id", ""),
        "message": row.get("message", ""),
        "timestamp": ts,
        "timestamp_desc": row.get("timestamp_desc", ""),
        "source": row.get("source", ""),
        "source_long": row.get("source_long", ""),
        "display_name": row.get("display_name", ""),
        "tags": row.get("tags") or [],
        "attributes": row.get("attributes") or {},
        "source_file": str(row.get("source_file", "")),
        "byte_offset": row.get("byte_offset"),
        "line_number": row.get("line_number"),
        "content_hash": row.get("content_hash", ""),
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
    """Outlier detection and similarity search backed by Qdrant + ClickHouse."""

    def __init__(
        self,
        qdrant: QdrantStore | None = None,
        clickhouse: ClickHouseStore | None = None,
    ) -> None:
        self.qdrant = qdrant or QdrantStore()
        self.clickhouse = clickhouse or ClickHouseStore()

    # ------------------------------------------------------------------
    # Outlier detection
    # ------------------------------------------------------------------

    def find_anomalies(
        self,
        case_id: str,
        timeline_id: str,
        limit: int = 50,
        sample_size: int = 5000,
    ) -> AnomalyResult:
        """Return the ``limit`` most unusual events in a timeline.

        Uses distance-to-centroid scoring: events furthest from the bulk of
        the timeline's embedding space are returned first.  This is
        *triage*, not threat detection — rare lines surface first, but
        rare ≠ malicious.

        Returns an :class:`AnomalyResult` with ``status="not_embedded"``
        when the timeline has no stored vectors.
        """
        collection = self.qdrant.find_timeline_collection(case_id, timeline_id)
        if collection is None:
            return AnomalyResult(status="not_embedded")

        # 1. Sample vectors to compute an approximate centroid.
        records = self.qdrant.scroll_vectors(
            collection, timeline_id, limit=sample_size, with_vectors=True
        )
        if len(records) < 2:
            return AnomalyResult(status="insufficient_vectors")

        vectors = np.array([r.vector for r in records], dtype=np.float32)
        centroid: np.ndarray = vectors.mean(axis=0)
        # Normalise centroid so cosine distance calculation is correct.
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm

        # Derive embedding_config_hash from the collection name suffix.
        config_hash = collection.rsplit("_", 1)[-1]
        actual_sample = len(records)

        # 2. Query for nearest points to -centroid (= farthest from centroid).
        neg_centroid = (-centroid).tolist()
        hits = self.qdrant.search(
            collection_name=collection,
            query_vector=neg_centroid,
            timeline_id=timeline_id,
            limit=limit,
            with_vectors=True,
        )

        if not hits:
            return AnomalyResult(
                status="ok",
                results=[],
                sample_size=actual_sample,
                embedding_config_hash=config_hash,
            )

        # 3. Recompute exact cosine distance and hydrate events from ClickHouse.
        event_ids = [str(h.id) for h in hits]
        ch_rows = self.clickhouse.get_events_by_ids(case_id, timeline_id, event_ids)

        results: list[OutlierResult] = []
        for rank, hit in enumerate(hits, start=1):
            eid = str(hit.id)
            vec = np.array(hit.vector, dtype=np.float32)
            distance = _cosine_distance(vec, centroid)
            score = distance  # higher = more anomalous

            if eid in ch_rows:
                event = _row_to_event(ch_rows[eid])
            else:
                payload = hit.payload or {}
                event = _payload_to_event(payload)

            details: dict[str, Any] = {
                "method": "centroid-distance",
                "distance": round(distance, 6),
                "rank": rank,
                "of": limit,
                "sample_size": actual_sample,
                "embedding_config_hash": config_hash,
            }
            results.append(
                OutlierResult(
                    event_id=eid,
                    score=round(score, 6),
                    event=event,
                    details=details,
                )
            )

        # Sort by score descending (most anomalous first).
        results.sort(key=lambda r: r.score, reverse=True)

        return AnomalyResult(
            status="ok",
            results=results,
            sample_size=actual_sample,
            embedding_config_hash=config_hash,
        )

    # ------------------------------------------------------------------
    # Similarity search
    # ------------------------------------------------------------------

    def find_similar(
        self,
        case_id: str,
        timeline_id: str,
        event_id: str,
        limit: int = 10,
    ) -> SimilaritySearchResult:
        """Return the ``limit`` events most semantically similar to ``event_id``.

        The query event itself is excluded from results.  Scores are cosine
        similarity (0–1; higher = more similar).

        Returns ``status="not_embedded"`` when the timeline has no vectors, or
        ``status="vector_not_found"`` when the specific event has no vector.
        """
        collection = self.qdrant.find_timeline_collection(case_id, timeline_id)
        if collection is None:
            return SimilaritySearchResult(status="not_embedded")

        query_vector = self.qdrant.retrieve_vector(collection, event_id)
        if query_vector is None:
            return SimilaritySearchResult(status="vector_not_found")

        # Fetch limit+1 and drop the query event itself.
        hits = self.qdrant.search(
            collection_name=collection,
            query_vector=query_vector,
            timeline_id=timeline_id,
            limit=limit + 1,
            with_vectors=False,
        )
        hits = [h for h in hits if str(h.id) != event_id][:limit]

        if not hits:
            return SimilaritySearchResult(status="ok", results=[])

        event_ids = [str(h.id) for h in hits]
        ch_rows = self.clickhouse.get_events_by_ids(case_id, timeline_id, event_ids)

        results: list[SimilarResult] = []
        for hit in hits:
            eid = str(hit.id)
            # Qdrant returns cosine similarity directly (0–1 for normalised vecs).
            score = round(float(hit.score), 6)
            if eid in ch_rows:
                event = _row_to_event(ch_rows[eid])
            else:
                payload = hit.payload or {}
                event = _payload_to_event(payload)
            results.append(SimilarResult(event_id=eid, score=score, event=event))

        return SimilaritySearchResult(status="ok", results=results)
