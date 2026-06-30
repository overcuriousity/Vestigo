"""Vector-backed outlier detection and similarity search.

Anomaly detection — two modes depending on analyst feedback:

**Baseline mode** (analyst has marked ≥1 events as "normal"):

One-class novelty detection scored by distance to the *nearest* normal event,
not to the normal-set average.  This is deliberate: in DFIR "normal operation"
is multimodal (logons, scheduled tasks, DNS, service starts, ... each form
their own cluster in embedding space), so a single centroid lands in the empty
space between those clusters and mislabels routine events as outliers.  A
candidate only has to resemble *one* normal exemplar to count as routine.

1. Collect the IDs of all events annotated ``annotation_type="normal"`` via
   the PostgresStore helper.
2. Retrieve the stored vectors for those normal events (dropping any that have
   no vector yet) and L2-normalise them into a baseline matrix.
3. Scroll up to ``sample_size`` candidate vectors for the sources and exclude
   the normal events themselves.
4. For each candidate compute the maximum cosine similarity to any normal
   vector; the anomaly score is ``1 - max_similarity`` (cosine distance to the
   nearest normal).  Sort descending and hydrate from ClickHouse.
   Details carry ``method="normal-baseline"`` and ``baseline_size``.

**Centroid mode** (no normal annotations, or fallback):

1. Discover which Qdrant collection holds the source IDs' vectors.
   Return ``status="not_embedded"`` when none exist.
2. Scroll up to ``sample_size`` points to compute an approximate centroid
   of the sources' embedding space.  On huge timelines this is a
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
2. Query for the K+1 nearest neighbours (filtered to the timeline's sources).
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


def _l2_normalize_rows(matrix: np.ndarray) -> np.ndarray:
    """Return ``matrix`` with each row scaled to unit L2 norm.

    Stored embedding vectors are already normalised, but normalising defensively
    means a plain dot product between two rows is their cosine similarity.
    Zero-norm rows are left unchanged (their similarity to anything is 0).
    """
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


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
        "source_id": payload.get("source_id", ""),
        "message": payload.get("message", ""),
        "timestamp": ts,
        "timestamp_desc": payload.get("timestamp_desc", ""),
        "artifact": payload.get("artifact", ""),
        "artifact_long": payload.get("artifact_long", ""),
        "display_name": payload.get("display_name", ""),
        "tags": payload.get("tags") or [],
        "attributes": {},
        # Provenance
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
        source_ids: list[str],
        limit: int = 50,
        sample_size: int = 5000,
        normal_ids: list[str] | None = None,
    ) -> AnomalyResult:
        """Return the ``limit`` most unusual events across the given sources.

        Operates in one of two modes depending on analyst feedback:

        **Baseline mode** — when ``normal_ids`` is a non-empty list of event IDs
        marked as "normal" by the analyst, scores each candidate by its cosine
        distance to the *nearest* normal event (one-class novelty detection).
        Normal events are excluded from results.  Details carry
        ``method="normal-baseline"``.

        **Centroid mode** — when ``normal_ids`` is empty or ``None``, uses distance-
        to-centroid scoring over a random sample.  Rare ≠ malicious; this is
        statistical triage only.  Details carry ``method="centroid-distance"``.

        ``normal_ids`` must be resolved by the caller (typically the async route
        handler via ``await postgres.list_event_ids_by_annotation_type(...)``).

        Returns :class:`AnomalyResult` with ``status="not_embedded"`` when
        the sources have no stored vectors.
        """
        collection = self.qdrant.find_timeline_collection(case_id, source_ids)
        if collection is None:
            return AnomalyResult(status="not_embedded")

        # Derive embedding_config_hash from the collection name suffix.
        config_hash = collection.rsplit("_", 1)[-1]

        normal_ids = normal_ids or []

        if normal_ids:
            return self._find_anomalies_baseline(
                collection=collection,
                case_id=case_id,
                source_ids=source_ids,
                limit=limit,
                sample_size=sample_size,
                config_hash=config_hash,
                normal_ids=normal_ids,
            )
        return self._find_anomalies_centroid(
            collection=collection,
            case_id=case_id,
            source_ids=source_ids,
            limit=limit,
            sample_size=sample_size,
            config_hash=config_hash,
        )

    def _find_anomalies_baseline(
        self,
        collection: str,
        case_id: str,
        source_ids: list[str],
        limit: int,
        sample_size: int,
        config_hash: str,
        normal_ids: list[str],
    ) -> AnomalyResult:
        """Anomaly detection by distance to the *nearest* analyst-marked normal.

        One-class novelty detection: each candidate is scored by ``1 - s`` where
        ``s`` is its maximum cosine similarity to any normal event.  Scoring
        against the nearest normal (rather than the normal-set centroid) keeps a
        multimodal baseline honest — a candidate only has to resemble one normal
        exemplar to be treated as routine.  Normal events are excluded from the
        returned results.
        """
        # Retrieve stored vectors for the analyst-marked normal events.  IDs come
        # from Postgres annotations and may predate embedding, so drop any that
        # have no vector in this collection yet.
        normal_points = self.qdrant.client.retrieve(
            collection_name=collection,
            ids=normal_ids,
            with_vectors=True,
            with_payload=False,
        )
        normal_vecs = [
            np.asarray(p.vector, dtype=np.float32)
            for p in normal_points
            if p.vector is not None
        ]
        normal_id_set = {str(p.id) for p in normal_points if p.vector is not None}
        baseline_size = len(normal_vecs)

        if not normal_vecs:
            return AnomalyResult(
                status="ok",
                results=[],
                sample_size=0,
                embedding_config_hash=config_hash,
                baseline_size=0,
                method="normal-baseline",
            )

        # Unit-normalise the baseline so a dot product is cosine similarity.
        normal_matrix = _l2_normalize_rows(np.vstack(normal_vecs))

        # Scan candidate vectors for the sources, excluding the normals themselves.
        # Bounded by sample_size for performance; raise it to scan a larger slice
        # of very large timelines.
        records = self.qdrant.scroll_vectors(
            collection, source_ids, limit=sample_size, with_vectors=True
        )
        candidates = [
            r
            for r in records
            if r.vector is not None and str(r.id) not in normal_id_set
        ]

        if not candidates:
            return AnomalyResult(
                status="ok",
                results=[],
                sample_size=len(records),
                embedding_config_hash=config_hash,
                baseline_size=baseline_size,
                method="normal-baseline",
            )

        cand_matrix = _l2_normalize_rows(
            np.vstack([np.asarray(r.vector, dtype=np.float32) for r in candidates])
        )
        # Cosine similarity of every candidate to every normal exemplar, then the
        # nearest-normal similarity per candidate.  Distance = 1 - nearest_sim.
        sims = cand_matrix @ normal_matrix.T
        nearest_sim = np.clip(sims.max(axis=1), -1.0, 1.0)
        distances = 1.0 - nearest_sim

        # Rank by distance descending and keep the top `limit`.
        order = np.argsort(-distances, kind="stable")[:limit]
        top = [(candidates[i], float(distances[i])) for i in order]

        event_ids = [str(rec.id) for rec, _ in top]
        ch_rows = self.clickhouse.get_events_by_ids(case_id, source_ids, event_ids)

        results: list[OutlierResult] = []
        for rank, (rec, distance) in enumerate(top, start=1):
            eid = str(rec.id)
            if eid in ch_rows:
                event = _row_to_event(ch_rows[eid])
            else:
                event = _payload_to_event(rec.payload or {})

            details: dict[str, Any] = {
                "method": "normal-baseline",
                "distance": round(distance, 6),
                "rank": rank,
                "of": len(top),
                "baseline_size": baseline_size,
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

        return AnomalyResult(
            status="ok",
            results=results,
            sample_size=len(records),
            embedding_config_hash=config_hash,
            baseline_size=baseline_size,
            method="normal-baseline",
        )

    def _find_anomalies_centroid(
        self,
        collection: str,
        case_id: str,
        source_ids: list[str],
        limit: int,
        sample_size: int,
        config_hash: str,
    ) -> AnomalyResult:
        """Anomaly detection via distance-to-global-centroid (no baseline)."""
        # 1. Sample vectors to compute an approximate centroid.
        records = self.qdrant.scroll_vectors(
            collection, source_ids, limit=sample_size, with_vectors=True
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
            source_ids=source_ids,
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
        ch_rows = self.clickhouse.get_events_by_ids(case_id, source_ids, event_ids)

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
            # Qdrant returns cosine similarity directly (0–1 for normalised vecs).
            score = round(float(hit.score), 6)
            if eid in ch_rows:
                event = _row_to_event(ch_rows[eid])
            else:
                payload = hit.payload or {}
                event = _payload_to_event(payload)
            results.append(SimilarResult(event_id=eid, score=score, event=event))

        return SimilaritySearchResult(status="ok", results=results)
