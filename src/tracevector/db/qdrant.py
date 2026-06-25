"""Qdrant connection and vector storage.

One collection is created per case.  The collection name embeds the
embedding-config hash so that vectors produced with different models or
normalisation settings are never mixed.
"""

from __future__ import annotations

from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

from tracevector.core.config import get_settings
from tracevector.models.event import Event


class QdrantStore:
    """Qdrant vector store wrapper with config-stability checks."""

    DEFAULT_DISTANCE = Distance.COSINE

    def __init__(self) -> None:
        settings = get_settings()
        self.collection_prefix = settings.qdrant_collection_prefix
        if settings.qdrant_path:
            self.client = QdrantClient(path=settings.qdrant_path)
        else:
            self.client = QdrantClient(url=settings.qdrant_url)

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

    def health(self) -> dict[str, Any]:
        """Return a simple health status for the Qdrant connection."""
        try:
            self.client.get_collections()
            return {"status": "ok"}
        except Exception as exc:  # noqa: BLE001
            return {"status": "error", "error": str(exc)}
