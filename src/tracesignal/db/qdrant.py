"""Qdrant connection and vector storage.

One collection is created per case.  The collection name embeds the
embedding-config hash so that vectors produced with different models or
normalisation settings are never mixed.

Points are scoped by ``source_id`` (one ingested file) so a Source can be
reused across multiple Timelines without duplicating vectors.
"""

from __future__ import annotations

import contextlib
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchAny,
    MatchValue,
    PointStruct,
    ScoredPoint,
    VectorParams,
)

from tracesignal.core.config import get_settings
from tracesignal.models.event import Event


class QdrantStore:
    """Qdrant vector store wrapper with config-stability checks."""

    DEFAULT_DISTANCE = Distance.COSINE

    def __init__(self) -> None:
        settings = get_settings()
        self.collection_prefix = settings.qdrant_collection_prefix
        if settings.qdrant_path:
            self.client = QdrantClient(path=settings.qdrant_path)
        else:
            self.client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key)

    def collection_name(self, case_id: str, embedding_config_hash: str) -> str:
        """Return the deterministic Qdrant collection name for a case."""
        safe_case = "".join(c if c.isalnum() else "_" for c in case_id)
        return f"{self.collection_prefix}_{safe_case}_{embedding_config_hash}"

    def init_collection(
        self,
        case_id: str,
        embedding_config_hash: str,
        vector_size: int,
        distance: Distance | None = None,
    ) -> None:
        """Create a Qdrant collection if it does not already exist.

        If the collection exists, its vector size must match ``vector_size``;
        otherwise a :py:class:`ValueError` is raised to prevent mixing
        incompatible embeddings.
        """
        name = self.collection_name(case_id, embedding_config_hash)
        distance = distance or self.DEFAULT_DISTANCE

        collections = self.client.get_collections()
        exists = any(c.name == name for c in collections.collections)
        if exists:
            info = self.client.get_collection(name)
            actual_size = info.config.params.vectors.size
            if actual_size != vector_size:
                raise ValueError(
                    f"Collection {name!r} exists with vector size {actual_size}, "
                    f"but requested size is {vector_size}. "
                    "The embedding configuration does not match the existing collection."
                )
            return

        self.client.create_collection(
            collection_name=name,
            vectors_config=VectorParams(size=vector_size, distance=distance),
        )

    def upsert_vectors(
        self,
        case_id: str,
        embedding_config_hash: str,
        events: list[Event],
        vectors: list[list[float]],
    ) -> int:
        """Upsert a batch of event vectors into Qdrant.

        Args:
            case_id: Investigation case identifier.
            embedding_config_hash: Hash of the embedding configuration.
            events: Events whose vectors are being stored.
            vectors: One vector per event; length must match ``events``.

        Returns:
            Number of points upserted.

        Raises:
            ValueError: If ``events`` and ``vectors`` have different lengths.
        """
        if len(events) != len(vectors):
            raise ValueError(
                f"Event count ({len(events)}) does not match vector count ({len(vectors)})"
            )
        if not events:
            return 0

        name = self.collection_name(case_id, embedding_config_hash)
        points: list[PointStruct] = []
        for event, vector in zip(events, vectors, strict=False):
            points.append(
                PointStruct(
                    id=event.vector_id,
                    vector=vector,
                    payload=event.to_qdrant_payload(),
                )
            )

        return self.upsert(name, points)

    def upsert(self, collection_name: str, points: list[PointStruct]) -> int:
        """Upsert raw points into a Qdrant collection.

        Args:
            collection_name: Target Qdrant collection.
            points: Points to upsert.

        Returns:
            Number of points upserted.
        """
        if not points:
            return 0
        self.client.upsert(collection_name=collection_name, points=points, wait=True)
        return len(points)

    def count_vectors(self, case_id: str, embedding_config_hash: str) -> int:
        """Return the number of vectors stored for a case collection."""
        name = self.collection_name(case_id, embedding_config_hash)
        try:
            result = self.client.count(collection_name=name, exact=True)
        except Exception as exc:  # noqa: BLE001
            raise RuntimeError(f"Failed to count vectors in {name!r}") from exc
        return result.count

    def case_collections(self, case_id: str) -> list[str]:
        """Return the names of all Qdrant collections that belong to ``case_id``.

        A case can have multiple collections (one per distinct embedding
        configuration hash).  All share the prefix
        ``{collection_prefix}_{safe_case}_``.
        """
        safe_case = "".join(c if c.isalnum() else "_" for c in case_id)
        prefix = f"{self.collection_prefix}_{safe_case}_"
        collections = self.client.get_collections()
        return [c.name for c in collections.collections if c.name.startswith(prefix)]

    def _source_filter(self, source_id: str) -> Filter:
        """Return a Qdrant filter matching a single source_id."""
        return Filter(
            must=[
                FieldCondition(
                    key="source_id",
                    match=MatchValue(value=source_id),
                )
            ]
        )

    def _sources_filter(self, source_ids: list[str]) -> Filter:
        """Return a Qdrant filter matching any of the given source IDs."""
        return Filter(
            must=[
                FieldCondition(
                    key="source_id",
                    match=MatchAny(any=list(source_ids)),
                )
            ]
        )

    def delete_source_points(self, case_id: str, source_id: str) -> None:
        """Delete all vector points for ``source_id`` across all case collections."""
        for name in self.case_collections(case_id):
            with contextlib.suppress(Exception):
                self.client.delete(
                    collection_name=name,
                    points_selector=self._source_filter(source_id),
                )

    def delete_points_for_sources(self, case_id: str, source_ids: list[str]) -> None:
        """Delete all vector points for a set of sources across all case collections."""
        for name in self.case_collections(case_id):
            with contextlib.suppress(Exception):
                self.client.delete(
                    collection_name=name,
                    points_selector=self._sources_filter(source_ids),
                )

    def find_source_collection(self, case_id: str, source_id: str) -> str | None:
        """Return the Qdrant collection name that holds vectors for ``source_id``.

        Iterates collections for the case and returns the first (by vector count,
        largest first) that contains at least one point with the given source_id
        in its payload.  Returns ``None`` when no embeddings exist for the source.
        """
        names = self.case_collections(case_id)
        if not names:
            return None

        best: tuple[int, str] | None = None
        source_filter = self._source_filter(source_id)
        for name in names:
            with contextlib.suppress(Exception):
                result = self.client.count(
                    collection_name=name,
                    count_filter=source_filter,
                    exact=False,
                )
                count = result.count
                if count > 0 and (best is None or count > best[0]):
                    best = (count, name)

        return best[1] if best is not None else None

    def find_collection_for_sources(self, case_id: str, source_ids: list[str]) -> str | None:
        """Return the Qdrant collection name that holds vectors for these sources.

        Returns the collection with the most points matching any of the given
        source IDs. Returns ``None`` when no embeddings exist. Not tied to any
        particular Timeline — callers may pass a timeline's sources, a whole
        case's sources, or any other subset.
        """
        names = self.case_collections(case_id)
        if not names or not source_ids:
            return None

        best: tuple[int, str] | None = None
        sources_filter = self._sources_filter(source_ids)
        for name in names:
            with contextlib.suppress(Exception):
                result = self.client.count(
                    collection_name=name,
                    count_filter=sources_filter,
                    exact=False,
                )
                count = result.count
                if count > 0 and (best is None or count > best[0]):
                    best = (count, name)

        return best[1] if best is not None else None

    def scroll_vectors(
        self,
        collection_name: str,
        source_ids: list[str],
        limit: int,
        with_vectors: bool = True,
    ) -> list[ScoredPoint]:
        """Return up to ``limit`` points for source IDs, with vectors.

        Uses the Qdrant ``scroll`` API which returns arbitrary (non-ranked) points —
        suitable for centroid computation where ranking does not matter.

        Returns a list of :class:`qdrant_client.models.Record` objects.
        """
        records, _next = self.client.scroll(
            collection_name=collection_name,
            scroll_filter=self._sources_filter(source_ids),
            limit=limit,
            with_vectors=with_vectors,
            with_payload=True,
        )
        return list(records)  # type: ignore[return-value]

    def search(
        self,
        collection_name: str,
        query_vector: list[float],
        source_ids: list[str],
        limit: int,
        with_vectors: bool = True,
    ) -> list[ScoredPoint]:
        """Return up to ``limit`` nearest neighbours of ``query_vector``.

        Results are filtered to ``source_ids`` via a payload filter and returned
        in descending score (similarity) order.
        """
        return self.client.query_points(
            collection_name=collection_name,
            query=query_vector,
            query_filter=self._sources_filter(source_ids),
            limit=limit,
            with_vectors=with_vectors,
            with_payload=True,
        ).points

    def retrieve_vector(self, collection_name: str, event_id: str) -> list[float] | None:
        """Retrieve the stored vector for a single point by its event_id.

        Returns ``None`` if the point does not exist in the collection.
        """
        results = self.client.retrieve(
            collection_name=collection_name,
            ids=[event_id],
            with_vectors=True,
            with_payload=False,
        )
        if not results:
            return None
        vec = results[0].vector
        if isinstance(vec, list):
            return vec
        # Named vectors dict (shouldn't occur for single unnamed vector collections)
        if isinstance(vec, dict):
            return next(iter(vec.values()), None)
        return None

    def delete_case_collections(self, case_id: str) -> None:
        """Delete all Qdrant collections that belong to ``case_id``."""
        for name in self.case_collections(case_id):
            with contextlib.suppress(Exception):
                self.client.delete_collection(name)

    def health(self) -> dict[str, Any]:
        """Return a simple health status for the Qdrant connection."""
        try:
            self.client.get_collections()
            return {"status": "ok"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}
