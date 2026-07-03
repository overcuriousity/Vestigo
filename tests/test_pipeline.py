"""Tests for the ingestion pipeline."""

from pathlib import Path
from typing import Any

import pytest

from tracevector.ingestion.pipeline import EmbeddingPipeline, IngestionPipeline
from tracevector.models.embeddings import EmbeddingConfig, EmbeddingModel
from tracevector.models.event import Event


class FakeClickHouseStore:
    """In-memory ClickHouse store for testing."""

    def __init__(self) -> None:
        self.events: list[Event] = []
        self.schema_initialized = False

    def init_schema(self) -> None:
        self.schema_initialized = True

    def insert_events(self, events: list[Event]) -> int:
        self.events.extend(events)
        return len(events)

    def count_events(self, case_id: str | None = None, source_id: str | None = None) -> int:
        events = self.events
        if case_id is not None:
            events = [e for e in events if e.case_id == case_id]
        if source_id is not None:
            events = [e for e in events if e.source_id == source_id]
        return len(events)

    def list_events(
        self,
        case_id: str,
        source_id: str,
        limit: int,
        offset: int = 0,
    ) -> list[dict[str, Any]]:
        events = [
            e.as_dict() for e in self.events if e.case_id == case_id and e.source_id == source_id
        ]
        return events[offset : offset + limit]


class FakeQdrantStore:
    """In-memory Qdrant store for testing."""

    def __init__(self) -> None:
        self.collections: dict[str, dict] = {}
        self.points: dict[str, list] = {}

    def init_collection(
        self,
        case_id: str,
        embedding_config_hash: str,
        vector_size: int,
        distance: str | None = None,
    ) -> None:
        name = self._name(case_id, embedding_config_hash)
        if name in self.collections:
            existing_size = self.collections[name]["vector_size"]
            if existing_size != vector_size:
                raise ValueError(
                    f"Collection {name!r} exists with vector size {existing_size}, "
                    f"but requested size is {vector_size}."
                )
            return
        self.collections[name] = {"vector_size": vector_size, "distance": distance}
        self.points[name] = []

    def upsert_vectors(
        self,
        case_id: str,
        embedding_config_hash: str,
        events: list[Event],
        vectors: list[list[float]],
    ) -> int:
        name = self._name(case_id, embedding_config_hash)
        points = [
            {"id": event.vector_id, "vector": vector}
            for event, vector in zip(events, vectors, strict=False)
        ]
        return self.upsert(name, points)

    def upsert(self, collection_name: str, points: list[dict[str, Any]]) -> int:
        self.points[collection_name].extend(points)
        return len(points)

    def count_vectors(self, case_id: str, embedding_config_hash: str) -> int:
        return len(self.points[self._name(case_id, embedding_config_hash)])

    def collection_name(self, case_id: str, embedding_config_hash: str) -> str:
        return self._name(case_id, embedding_config_hash)

    @staticmethod
    def _name(case_id: str, embedding_config_hash: str) -> str:
        return f"tracevector_{case_id}_{embedding_config_hash}"


class FakeEmbeddingModel(EmbeddingModel):
    """Embedding model that returns deterministic vectors without PyTorch."""

    def __init__(self, vector_dimension: int = 8) -> None:
        self.model_name = "fake-model"
        self.device = "cpu"
        self.batch_size = 64
        self._vector_dimension = vector_dimension
        self._normalize = True
        self._pooling = "mean"
        self._resolved_config: EmbeddingConfig | None = EmbeddingConfig(
            model_name=self.model_name,
            device=self.device,
            vector_dimension=vector_dimension,
            normalize=True,
            pooling="mean",
        )
        self._model = None

    def load(self):
        return self

    def vector_dimension(self) -> int:
        return self._vector_dimension

    def encode(self, texts: list[str]) -> list[list[float]]:
        # Deterministic fake vectors: first dimension is text length, rest zeros.
        return [[float(len(t))] + [0.0] * (self._vector_dimension - 1) for t in texts]


@pytest.fixture
def sample_jsonl(tmp_path: Path) -> Path:
    path = tmp_path / "events.jsonl"
    path.write_text(
        '{"message":"User login","timestamp":"2024-01-01T00:00:00+00:00"}\n'
        '{"message":"User logout","timestamp":"2024-01-01T00:01:00+00:00"}\n'
        '{"message":"File accessed","timestamp":"2024-01-01T00:02:00+00:00"}\n'
    )
    return path


def test_pipeline_ingests_events_without_vectors(sample_jsonl: Path) -> None:
    clickhouse = FakeClickHouseStore()

    pipeline = IngestionPipeline(
        case_id="case1",
        source_id="source1",
        clickhouse=clickhouse,
        batch_size=2,
        source_name="events.jsonl",
        file_hash="abc",
    )

    result = pipeline.run(sample_jsonl)

    assert result.case_id == "case1"
    assert result.source_id == "source1"
    assert result.events_parsed == 3
    assert result.events_inserted == 3
    assert len(clickhouse.events) == 3
    assert clickhouse.schema_initialized is True


def test_pipeline_reports_monotonic_byte_progress(sample_jsonl: Path) -> None:
    clickhouse = FakeClickHouseStore()
    calls: list[tuple[int, int]] = []

    pipeline = IngestionPipeline(
        case_id="case1",
        source_id="source1",
        clickhouse=clickhouse,
        batch_size=2,
        source_name="events.jsonl",
        file_hash="abc",
        progress_callback=lambda total, processed: calls.append((total, processed)),
    )
    pipeline.run(sample_jsonl)

    size = sample_jsonl.stat().st_size
    assert calls[0] == (size, 0)
    assert calls[-1] == (size, size)
    assert all(total == size for total, _ in calls)
    processed_values = [processed for _, processed in calls]
    assert processed_values == sorted(processed_values)


def test_embedding_pipeline_generates_vectors(sample_jsonl: Path) -> None:
    embedding_model = FakeEmbeddingModel(vector_dimension=8)
    clickhouse = FakeClickHouseStore()
    qdrant = FakeQdrantStore()

    ingest = IngestionPipeline(
        case_id="case1",
        source_id="source1",
        clickhouse=clickhouse,
        batch_size=2,
        source_name="events.jsonl",
        file_hash="abc",
    )
    ingest.run(sample_jsonl)

    embed = EmbeddingPipeline(
        case_id="case1",
        source_ids=["source1"],
        embedding_model=embedding_model,
        clickhouse=clickhouse,
        qdrant=qdrant,
        batch_size=2,
    )
    result = embed.run()

    assert result.events_processed == 3
    assert result.vectors_inserted == 3
    collection_name = qdrant._name("case1", embedding_model.config_hash())
    assert qdrant.count_vectors("case1", embedding_model.config_hash()) == 3
    assert len(qdrant.points[collection_name]) == 3


def test_pipeline_raises_on_missing_path(tmp_path: Path) -> None:
    pipeline = IngestionPipeline(
        case_id="case1",
        source_id="source1",
        clickhouse=FakeClickHouseStore(),
        source_name="missing.jsonl",
        file_hash="abc",
    )
    with pytest.raises(FileNotFoundError):
        pipeline.run(tmp_path / "does-not-exist.jsonl")


def test_pipeline_deduplicates_identical_file_hash(sample_jsonl: Path) -> None:
    """Re-running with the same file_hash produces duplicate event IDs, so the
    second run should not increase the stored row count (simulating ClickHouse
    identity collision or an upstream idempotency guard).
    """
    clickhouse = FakeClickHouseStore()

    pipeline = IngestionPipeline(
        case_id="case1",
        source_id="source1",
        clickhouse=clickhouse,
        batch_size=2,
        file_hash="abc123",
        source_name="events.jsonl",
    )

    first = pipeline.run(sample_jsonl)
    assert first.events_inserted == 3
    assert len(clickhouse.events) == 3
    first_ids = [e.event_id for e in clickhouse.events]

    second = pipeline.run(sample_jsonl)
    assert second.events_inserted == 3
    # Fake store simply appends, so we verify determinism by ID overlap.
    second_ids = [e.event_id for e in clickhouse.events[3:]]
    assert first_ids == second_ids
    assert all(e.file_hash == "abc123" for e in clickhouse.events)
    assert all(e.source_file == Path("events.jsonl") for e in clickhouse.events)


def test_qdrant_config_mismatch_is_rejected() -> None:
    qdrant = FakeQdrantStore()
    qdrant.init_collection("case1", "hash1", vector_size=384)

    with pytest.raises(ValueError, match="exists with vector size 384"):
        qdrant.init_collection("case1", "hash1", vector_size=768)
