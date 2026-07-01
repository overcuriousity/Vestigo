"""Tests for the SimilarityService (semantic similarity search).

All tests use in-memory fakes for Qdrant and ClickHouse so they run without
any external services.
"""

from __future__ import annotations

from typing import Any

from tracevector.db.similarity import SimilarityService

# ---------------------------------------------------------------------------
# Fake Qdrant store
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


class _FakeRetrieveResult:
    """Minimal stand-in for qdrant_client retrieve results."""

    def __init__(
        self,
        id: str,
        vector: list[float] | None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        self.id = id
        self.vector = vector
        self.payload: dict[str, Any] = payload or {}


class FakeQdrantStore:
    """In-memory Qdrant store that supports similarity service methods."""

    def __init__(self) -> None:
        self._points: dict[str, list[FakeScoredPoint]] = {}
        self.client = self

    def retrieve(
        self,
        collection_name: str,
        ids: list[str],
        with_vectors: bool = True,
        with_payload: bool = True,
    ) -> list[_FakeRetrieveResult]:
        points = self._points.get(collection_name, [])
        id_set = set(ids)
        results = []
        for p in points:
            if p.id in id_set:
                vec = p.vector if with_vectors else None
                payload = p.payload if with_payload else {}
                results.append(_FakeRetrieveResult(p.id, vec, payload))
        return results

    def _add_point(
        self,
        collection: str,
        event_id: str,
        vector: list[float],
        source_id: str,
        message: str = "test event",
    ) -> None:
        if collection not in self._points:
            self._points[collection] = []
        self._points[collection].append(
            FakeScoredPoint(
                id=event_id,
                vector=vector,
                payload={
                    "event_id": event_id,
                    "source_id": source_id,
                    "message": message,
                },
            )
        )

    def case_collections(self, case_id: str) -> list[str]:
        return list(self._points.keys())

    def find_collection_for_sources(
        self, case_id: str, source_ids: list[str]
    ) -> str | None:
        source_set = set(source_ids)
        for name, points in self._points.items():
            for p in points:
                if p.payload.get("source_id") in source_set:
                    return name
        return None

    def scroll_vectors(
        self,
        collection_name: str,
        source_ids: list[str],
        limit: int,
        with_vectors: bool = True,
    ) -> list[FakeScoredPoint]:
        points = self._points.get(collection_name, [])
        source_set = set(source_ids)
        filtered = [
            p for p in points if p.payload.get("source_id") in source_set
        ]
        return filtered[:limit]

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        source_ids: list[str],
        limit: int,
        with_vectors: bool = True,
    ) -> list[FakeScoredPoint]:
        import numpy as np

        points = self._points.get(collection_name, [])
        source_set = set(source_ids)
        filtered = [
            p for p in points if p.payload.get("source_id") in source_set
        ]
        q = np.array(query_vector, dtype=np.float32)
        scored = []
        for p in filtered:
            v = np.array(p.vector, dtype=np.float32)
            norm_q = np.linalg.norm(q)
            norm_v = np.linalg.norm(v)
            score = (
                0.0
                if norm_q == 0 or norm_v == 0
                else float(np.dot(q, v) / (norm_q * norm_v))
            )
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
        self, case_id: str, source_ids: list[str], event_ids: list[str]
    ) -> dict[str, dict]:
        return {eid: self._rows[eid] for eid in event_ids if eid in self._rows}


class FakeEmbeddingModel:
    """Stand-in for EmbeddingModel that maps fixed strings to fixed vectors."""

    def __init__(self, vectors_by_text: dict[str, list[float]]) -> None:
        self._vectors_by_text = vectors_by_text

    def load(self) -> None:
        pass

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [self._vectors_by_text[t] for t in texts]


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
# find_similar tests
# ---------------------------------------------------------------------------


def test_find_similar_not_embedded():
    """find_similar returns not_embedded when no vectors exist."""
    qdrant = FakeQdrantStore()
    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", ["s1"], "evt_x")
    assert result.status == "not_embedded"


def test_find_similar_vector_not_found():
    """find_similar returns vector_not_found when event has no stored vector."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "evt_other", _unit([1.0, 0.0]), "s1")
    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", ["s1"], "evt_missing")
    assert result.status == "vector_not_found"


def test_find_similar_excludes_query_event():
    """The query event itself must not appear in similarity results."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "query", _unit([1.0, 0.0]), "s1", "query event")
    qdrant._add_point("col1", "close1", _unit([0.99, 0.14]), "s1", "similar event")
    qdrant._add_point("col1", "far1", _unit([0.0, 1.0]), "s1", "dissimilar event")

    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", ["s1"], "query", limit=5)

    assert result.status == "ok"
    returned_ids = [r.event_id for r in result.results]
    assert "query" not in returned_ids


def test_find_similar_returns_nearest_first():
    """Nearest neighbour should have a higher score than distant events."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "query", _unit([1.0, 0.0]), "s1")
    qdrant._add_point("col1", "close", _unit([0.99, 0.14]), "s1")  # very similar
    qdrant._add_point("col1", "far", _unit([0.0, 1.0]), "s1")     # orthogonal

    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", ["s1"], "query", limit=2)

    assert result.status == "ok"
    assert len(result.results) == 2
    assert result.results[0].event_id == "close"
    assert result.results[0].score > result.results[1].score


def test_find_similar_hydrates_from_clickhouse():
    """Events found in ClickHouse carry the richer ClickHouse attributes."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "query", _unit([1.0, 0.0]), "s1")
    qdrant._add_point("col1", "similar", _unit([0.98, 0.2]), "s1", "payload msg")

    ch_row = {
        "event_id": "similar",
        "case_id": "case1",
        "source_id": "s1",
        "message": "CH-sourced message",
        "timestamp": "2024-01-01T00:00:00",
        "timestamp_desc": "File Modified",
        "artifact": "test",
        "artifact_long": "",
        "display_name": "",
        "tags": [],
        "attributes": {"key": "value"},
        "source_file": "/tmp/f",
        "byte_offset": 0,
        "line_number": 1,
        "content_hash": "abc",
        "file_hash": "xyz",
        "parser_name": "p",
        "parser_version": "1",
        "embedding_model": "m",
        "embedding_config_hash": "h",
        "vector_id": "similar",
        "ingest_time": None,
    }
    ch = FakeClickHouseStore(rows={"similar": ch_row})
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", ["s1"], "query", limit=2)

    assert result.status == "ok"
    sim = next((r for r in result.results if r.event_id == "similar"), None)
    assert sim is not None
    assert sim.event["message"] == "CH-sourced message"
    assert sim.event["attributes"] == {"key": "value"}


def test_find_similar_falls_back_to_payload():
    """Events absent from ClickHouse fall back to the Qdrant payload."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "query", _unit([1.0, 0.0]), "s1")
    qdrant._add_point("col1", "similar", _unit([0.98, 0.2]), "s1", "payload msg")

    ch = FakeClickHouseStore(rows={})
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar("case1", ["s1"], "query", limit=2)

    assert result.status == "ok"
    sim = next((r for r in result.results if r.event_id == "similar"), None)
    assert sim is not None
    assert sim.event["message"] == "payload msg"


# ---------------------------------------------------------------------------
# find_similar_by_text tests
# ---------------------------------------------------------------------------


def test_find_similar_by_text_not_embedded():
    """find_similar_by_text returns not_embedded when no vectors exist."""
    qdrant = FakeQdrantStore()
    ch = FakeClickHouseStore()
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch)
    result = svc.find_similar_by_text("case1", ["s1"], "some query")
    assert result.status == "not_embedded"


def test_find_similar_by_text_returns_nearest_first():
    """The event closest to the embedded query text should score highest."""
    qdrant = FakeQdrantStore()
    qdrant._add_point("col1", "close", _unit([0.99, 0.14]), "s1")
    qdrant._add_point("col1", "far", _unit([0.0, 1.0]), "s1")

    ch = FakeClickHouseStore()
    embedding_model = FakeEmbeddingModel({"login failure": _unit([1.0, 0.0])})
    svc = SimilarityService(qdrant=qdrant, clickhouse=ch, embedding_model=embedding_model)
    result = svc.find_similar_by_text("case1", ["s1"], "login failure", limit=2)

    assert result.status == "ok"
    assert len(result.results) == 2
    assert result.results[0].event_id == "close"
    assert result.results[0].score > result.results[1].score
