"""Vector-backed outlier detection and similarity search.

Anomaly detection — two modes depending on analyst feedback:

**Baseline mode** (analyst has marked ≥1 events as "normal"):

1. Collect the IDs of all events annotated ``annotation_type="normal"`` via
   the PostgresStore helper.
2. Pass them as *negative examples* to Qdrant's Recommendation API.
   With negative-only examples Qdrant returns points maximally unlike the
   normal set — the analyst's definition of anomaly.
3. Exclude the normal events themselves from results.
4. Recompute exact cosine distance from the normal-set centroid for each
   result, sort descending, and hydrate from ClickHouse.
   Details carry ``method="normal-baseline"`` and ``baseline_size``.

**Centroid mode** (no normal annotations, or fallback):

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
   Details carry ``method="centroid-distance"``.

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
    # Number of analyst-marked "normal" events used as the baseline.
    # 0 when using the global-centroid fallback mode.
    baseline_size: int = 0
    # "centroid-distance" or "normal-baseline"
    method: str = "centroid-distance"


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
    # Convert to ISO string for consistent serialisation.
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
        normal_ids: list[str] | None = None,
    ) -> AnomalyResult:
        """Return the ``limit`` most unusual events in a timeline.

        Operates in one of two modes depending on analyst feedback:

        **Baseline mode** — when ``normal_ids`` is a non-empty list of event IDs
        marked as "normal" by the analyst, uses Qdrant's Recommendation API with
        those events as negatives (maximally unlike the normal set).  Normal events
        are excluded from results.  Details carry ``method="normal-baseline"``.

        **Centroid mode** — when ``normal_ids`` is empty or ``None``, uses distance-
        to-centroid scoring over a random sample.  Rare ≠ malicious; this is
        statistical triage only.  Details carry ``method="centroid-distance"``.

        ``normal_ids`` must be resolved by the caller (typically the async route
        handler via ``await postgres.list_event_ids_by_annotation_type(...)``).

        Returns :class:`AnomalyResult` with ``status="not_embedded"`` when
        the timeline has no stored vectors.
        """
        collection = self.qdrant.find_timeline_collection(case_id, timeline_id)
        if collection is None:
            return AnomalyResult(status="not_embedded")

        # Derive embedding_config_hash from the collection name suffix.
        config_hash = collection.rsplit("_", 1)[-1]

        normal_ids = normal_ids or []
        normal_id_set = set(normal_ids)

        if normal_ids:
            return self._find_anomalies_baseline(
                collection=collection,
                case_id=case_id,
                timeline_id=timeline_id,
                limit=limit,
                config_hash=config_hash,
                normal_ids=normal_ids,
                normal_id_set=normal_id_set,
            )
        return self._find_anomalies_centroid(
            collection=collection,
            case_id=case_id,
            timeline_id=timeline_id,
            limit=limit,
            sample_size=sample_size,
            config_hash=config_hash,
        )

    def _find_anomalies_baseline(
        self,
        collection: str,
        case_id: str,
        timeline_id: str,
        limit: int,
        config_hash: str,
        normal_ids: list[str],
        normal_id_set: set[str],
    ) -> AnomalyResult:
        """Anomaly detection driven by analyst-defined normal baseline.

        Uses Qdrant's Recommendation API with the normal events as negatives.
        Normal events are excluded from the returned results.
        """
        # Request extra results so we can drop normal events from the list.
        fetch_limit = limit + len(normal_ids)
        hits = self.qdrant.recommend_anomalies(
            collection_name=collection,
            timeline_id=timeline_id,
            negative_ids=normal_ids,
            limit=fetch_limit,
            with_vectors=True,
        )

        # Exclude normal events from results.
        hits = [h for h in hits if str(h.id) not in normal_id_set][:limit]

        if not hits:
            return AnomalyResult(
                status="ok",
                results=[],
                sample_size=0,
                embedding_config_hash=config_hash,
                baseline_size=len(normal_ids),
                method="normal-baseline",
            )

        # Compute the normal-set centroid to derive a meaningful distance score.
        # Fetch the specific normal-event vectors by ID so we always retrieve the
        # right points regardless of timeline size (scroll_vectors returns arbitrary
        # points and would miss the normal events on any large timeline).
        normal_vecs_raw = self.qdrant.client.retrieve(
            collection_name=collection,
            ids=normal_ids,
            with_vectors=True,
            with_payload=False,
        )
        normal_vecs = [
            np.array(r.vector, dtype=np.float32)
            for r in normal_vecs_raw
            if r.vector is not None
        ]
        if normal_vecs:
            centroid: np.ndarray = np.mean(normal_vecs, axis=0)
            norm = np.linalg.norm(centroid)
            if norm > 0:
                centroid = centroid / norm
        else:
            centroid = None

        event_ids = [str(h.id) for h in hits]
        ch_rows = self.clickhouse.get_events_by_ids(case_id, timeline_id, event_ids)

        results: list[OutlierResult] = []
        for rank, hit in enumerate(hits, start=1):
            eid = str(hit.id)
            if eid in ch_rows:
                event = _row_to_event(ch_rows[eid])
            else:
                event = _payload_to_event(hit.payload or {})

            if centroid is not None and hit.vector is not None:
                vec = np.array(hit.vector, dtype=np.float32)
                distance = _cosine_distance(vec, centroid)
            else:
                distance = float(1 - hit.score) if hit.score is not None else 0.0

            details: dict[str, Any] = {
                "method": "normal-baseline",
                "distance": round(distance, 6),
                "rank": rank,
                "of": limit,
                "baseline_size": len(normal_ids),
                "embedding_config_hash": config_hash,
            }
            results.append(
                OutlierResult(
                    event_id=eid,
                    score=round(distance, 6),
                    event=event,
                    details=details,
                )
            )

        results.sort(key=lambda r: r.score, reverse=True)
        return AnomalyResult(
            status="ok",
            results=results,
            sample_size=0,
            embedding_config_hash=config_hash,
            baseline_size=len(normal_ids),
            method="normal-baseline",
        )

    def _find_anomalies_centroid(
        self,
        collection: str,
        case_id: str,
        timeline_id: str,
        limit: int,
        sample_size: int,
        config_hash: str,
    ) -> AnomalyResult:
        """Anomaly detection via distance-to-global-centroid (no baseline)."""
        # 1. Sample vectors to compute an approximate centroid.
        records = self.qdrant.scroll_vectors(
            collection, timeline_id, limit=sample_size, with_vectors=True
        )
        if len(records) < 2:
            return AnomalyResult(status="insufficient_vectors")

        vectors = np.array([r.vector for r in records], dtype=np.float32)
        centroid: np.ndarray = vectors.mean(axis=0)
        norm = np.linalg.norm(centroid)
        if norm > 0:
            centroid = centroid / norm

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

        # 3. Recompute exact cosine distance and hydrate from ClickHouse.
        event_ids = [str(h.id) for h in hits]
        ch_rows = self.clickhouse.get_events_by_ids(case_id, timeline_id, event_ids)

        results: list[OutlierResult] = []
        for rank, hit in enumerate(hits, start=1):
            eid = str(hit.id)
            vec = np.array(hit.vector, dtype=np.float32)
            distance = _cosine_distance(vec, centroid)
            score = distance

            if eid in ch_rows:
                event = _row_to_event(ch_rows[eid])
            else:
                event = _payload_to_event(hit.payload or {})

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

        results.sort(key=lambda r: r.score, reverse=True)
        return AnomalyResult(
            status="ok",
            results=results,
            sample_size=actual_sample,
            embedding_config_hash=config_hash,
            method="centroid-distance",
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
