"""Tests for the SimilarityService (outlier detection and similarity search).

All tests use in-memory fakes for Qdrant and ClickHouse so they run without
any external services.
"""

from __future__ import annotations

import math
from typing import Any
from unittest.mock import MagicMock

import pytest

from tracevector.db.similarity import SimilarityService


# ---------------------------------------------------------------------------
# Fake Qdrant store (extends the pipeline fake with search/scroll/retrieve)
# ---------------------------------------------------------------------------


class FakeScoredPoint:
    """Minimal stand-in for qdrant_client.models.ScoredPoint."""

    def __init__(
        self,
        id: str,
        vector: list[float],
        payload: dict[str, Any],
        score: float = 1.0,
    ) -> None:
        self.id = id
        self.vector = vector
        self.payload = payload
        self.score = score


class FakeQdrantStore:
    """In-memory Qdrant store that supports similarity service methods."""

    def __init__(self) -> None:
        # collection_name -> list of FakeScoredPoint
        self._points: dict[str, list[FakeScoredPoint]] = {}

    def _add_point(
        self,
        collection: str,
        event_id: str,
        vector: list[float],
        timeline_id: str,
        message: str = "test event",
    ) -> None:
        if collection not in self._points:
            self._points[collection] = []
        self._points[collection].append(
            FakeScoredPoint(
                id=event_id,
                vector=vector,
                payload={"event_id": event_id, "timeline_id": timeline_id, "message": message},
            )
        )

    def case_collections(self, case_id: str) -> list[str]:
        return list(self._points.keys())

    def find_timeline_collection(self, case_id: str, timeline_id: str) -> str | None:
        for name, points in self._points.items():
            for p in points:
                if p.payload.get("timeline_id") == timeline_id:
                    return name
        return None

    def scroll_vectors(
        self,
        collection_name: str,
        timeline_id: str,
        limit: int,
        with_vectors: bool = True,
    ) -> list[FakeScoredPoint]:
        points = self._points.get(collection_name, [])
        filtered = [
            p for p in points if p.payload.get("timeline_id") == timeline_id
        ]
        return filtered[:limit]

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        timeline_id: str,
        limit: int,
        with_vectors: bool = True,
    ) -> list[FakeScoredPoint]:
        points = self._points.get(collection_name, [])
        filtered = [
            p for p in points if p.payload.get("timeline_id") == timeline_id
        ]
        # Compute cosine similarity between query and each point vector.
        import numpy as np

        q = np.array(query_vector, dtype=np.float32)
        scored = []
        for p in filtered:
            v = np.array(p.vector, dtype=np.float32)
            norm_q = np.linalg.norm(q)
            norm_v = np.linalg.norm(v)
            if norm_q == 0 or norm_v == 0:
                score = 0.0
            else:
                score = float(np.dot(q, v) / (norm_q * norm_v))
            scored.append(
                FakeScoredPoint(
                    id=p.id,
                    vector=p.vector,
                    payload=p.payload,
                    score=score,
                )
            )
        scored.sort(key=lambda x: x.score, reverse=True)
        return scored[:limit]

    def retrieve_vector(
        self, collection_name: str, event_id: str
    ) -> list[float] | None:
        for p in self._points.get(collection_name, []):
            if p.id == event_id:
                return p.vector
        return None


class FakeClickHouseStore:
    """ClickHouse fake that returns rows for known event_ids."""

    def __init__(self, rows: dict[str, dict] | None = None) -> None:
        self._rows = rows or {}

    def init_schema(self) -> None:
        pass

    def get_events_by_ids(
        self, case_id: str, timeline_id: str, event_ids: list[str]
    ) -> dict[str, dict]:
        return {eid: self._rows[eid] for eid in event_ids if eid in self._rows}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _unit(v: list[float]) -> list[float]:
    """Return L2-normalised vector."""
    import numpy as np

    a = np.array(v, dtype=np.float32)
    n = np.linalg.norm(a)
    return (a / n).tolist() if n > 0 else v


# ---------------------------------------------------------------------------
# find_anomalies tests
# ---------------------------------------------------------------------------


def test_find_anomalies_not_embedded_returns_status():
    """find_anomalies returns not_embedded when the timeline has no vectors."""
    qdrant = FakeQdrantStore()  # empty
    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_anomalies("case1", "tl1")
    assert result.status == "not_embedded"
    assert result.results == []


def test_find_anomalies_insufficient_vectors():
    """find_anomalies returns insufficient_vectors with only one embedded event."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "evt1", _unit([1.0, 0.0, 0.0]), "tl1")
    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_anomalies("case1", "tl1")
    assert result.status == "insufficient_vectors"


def test_find_anomalies_plants_outlier_first():
    """The most distant vector from the centroid should rank first."""
    qdrant = FakeQdrantStore()
    # Three events aligned along x-axis (these form the "normal" bulk).
    for i in range(3):
        qdrant._add_point("col1", f"normal_{i}", _unit([1.0, 0.0, 0.0]), "tl1", f"normal {i}")
    # One obvious outlier: anti-parallel to the bulk.
    qdrant._add_point("col1", "outlier", _unit([-1.0, 0.0, 0.0]), "tl1", "outlier event")

    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_anomalies("case1", "tl1", limit=4, sample_size=100)

    assert result.status == "ok"
    assert len(result.results) > 0
    assert result.results[0].event_id == "outlier"
    # Score should be high (close to 2.0 in cosine distance, normalised here).
    assert result.results[0].score > 0.5


def test_find_anomalies_details_shape():
    """Each OutlierResult carries the expected math in details."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "a", _unit([1.0, 0.0]), "tl1")
    qdrant._add_point("col1", "b", _unit([0.0, 1.0]), "tl1")
    qdrant._add_point("col1", "c", _unit([-1.0, 0.0]), "tl1")

    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_anomalies("case1", "tl1", limit=3)

    assert result.status == "ok"
    r = result.results[0]
    d = r.details
    assert d["method"] == "centroid-distance"
    assert "distance" in d
    assert "rank" in d
    assert "of" in d
    assert "sample_size" in d
    assert "embedding_config_hash" in d
    assert isinstance(d["distance"], float)


def test_find_anomalies_hydrates_from_clickhouse():
    """Events found in ClickHouse should have richer attributes than payload-only."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "evt_a", _unit([1.0, 0.0]), "tl1", "payload msg")
    qdrant._add_point("col1", "evt_b", _unit([-1.0, 0.0]), "tl1", "payload outlier")

    ch_row = {
        "event_id": "evt_b",
        "case_id": "case1",
        "timeline_id": "tl1",
        "message": "CH-sourced outlier message",
        "timestamp": "2024-01-01T00:00:00",
        "timestamp_desc": "File Modified",
        "source": "test",
        "source_long": "",
        "display_name": "",
        "tags": [],
        "attributes": {"key": "value"},
        "source_file": "/tmp/f",
        "byte_offset": 0,
        "line_number": 1,
        "content_hash": "abc",
        "parser_name": "p",
        "parser_version": "1",
        "embedding_model": "m",
        "embedding_config_hash": "h",
        "vector_id": "evt_b",
        "ingest_time": None,
    }
    ch = FakeClickHouseStore(rows={"evt_b": ch_row})
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_anomalies("case1", "tl1", limit=2)

    assert result.status == "ok"
    # The outlier (evt_b) should appear and have the rich ClickHouse message.
    outlier = next((r for r in result.results if r.event_id == "evt_b"), None)
    assert outlier is not None
    assert outlier.event["message"] == "CH-sourced outlier message"
    assert outlier.event["attributes"] == {"key": "value"}


def test_find_anomalies_fallback_to_payload_when_ch_missing():
    """Events not in ClickHouse fall back to the Qdrant payload."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "evt_a", _unit([1.0, 0.0]), "tl1", "msg a")
    qdrant._add_point("col1", "evt_b", _unit([-1.0, 0.0]), "tl1", "outlier msg")

    ch = FakeClickHouseStore(rows={})  # nothing in CH
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_anomalies("case1", "tl1", limit=2)

    assert result.status == "ok"
    outlier = next((r for r in result.results if r.event_id == "evt_b"), None)
    assert outlier is not None
    # Should fall back to the Qdrant payload message.
    assert outlier.event["message"] == "outlier msg"


# ---------------------------------------------------------------------------
# find_similar tests
# ---------------------------------------------------------------------------


def test_find_similar_not_embedded():
    """find_similar returns not_embedded when no vectors exist."""
    qdrant = FakeQdrantStore()
    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", "tl1", "evt_x")
    assert result.status == "not_embedded"


def test_find_similar_vector_not_found():
    """find_similar returns vector_not_found when event has no stored vector."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "evt_other", _unit([1.0, 0.0]), "tl1")
    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", "tl1", "evt_missing")
    assert result.status == "vector_not_found"


def test_find_similar_excludes_query_event():
    """The query event itself must not appear in similarity results."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "query", _unit([1.0, 0.0]), "tl1", "query event")
    qdrant._add_point("col1", "close1", _unit([0.99, 0.14]), "tl1", "similar event")
    qdrant._add_point("col1", "far1", _unit([0.0, 1.0]), "tl1", "dissimilar event")

    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", "tl1", "query", limit=5)

    assert result.status == "ok"
    returned_ids = [r.event_id for r in result.results]
    assert "query" not in returned_ids


def test_find_similar_returns_nearest_first():
    """Nearest neighbour should have a higher score than distant events."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "query", _unit([1.0, 0.0]), "tl1")
    qdrant._add_point("col1", "close", _unit([0.99, 0.14]), "tl1")  # very similar
    qdrant._add_point("col1", "far", _unit([0.0, 1.0]), "tl1")     # orthogonal

    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", "tl1", "query", limit=2)

    assert result.status == "ok"
    assert len(result.results) == 2
    # "close" should rank before "far".
    assert result.results[0].event_id == "close"
    assert result.results[0].score > result.results[1].score
