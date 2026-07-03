"""Embedding model wrapper and anomaly utilities."""

from __future__ import annotations

import os
from typing import Any

import httpx
from sentence_transformers import SentenceTransformer

from tracesignal.core.config import get_settings
from tracesignal.models.event import EmbeddingConfig


class EmbeddingModel:
    """Embedding model for log lines with forensic config tracking.

    Backed by a local sentence-transformer by default, or by a remote
    OpenAI-compatible ``/embeddings`` endpoint when ``embedding_api_base_url``
    is configured.
    """

    def __init__(
        self, model_name: str | None = None, config: EmbeddingConfig | None = None
    ) -> None:
        settings = get_settings()
        self.model_name = (
            (config.model_name if config else None) or model_name or settings.embedding_model
        )
        self.device = (config.device if config else None) or settings.embedding_device
        self.batch_size = settings.embedding_batch_size
        self._normalize = (config.normalize if config else None) or True
        self._pooling = (config.pooling if config else None) or "mean"
        self._vector_dimension: int | None = config.vector_dimension if config else None
        self._resolved_config: EmbeddingConfig | None = None
        self._model: SentenceTransformer | None = None
        self._api_base_url = settings.embedding_api_base_url
        self._api_key = settings.embedding_api_key
        self._client: httpx.Client | None = None

    @property
    def is_remote(self) -> bool:
        """Whether this instance uses a remote OpenAI-compatible embeddings endpoint."""
        return bool(self._api_base_url)

    @property
    def config(self) -> EmbeddingConfig:
        """Return the immutable, resolved embedding configuration."""
        if self._resolved_config is None:
            self._resolved_config = EmbeddingConfig(
                model_name=self.model_name,
                device="remote" if self.is_remote else self.device,
                vector_dimension=self.vector_dimension(),
                normalize=self._normalize,
                pooling=self._pooling,
            )
        return self._resolved_config

    def config_hash(self) -> str:
        """Return the configuration hash used for provenance checks."""
        return self.config.config_hash()

    def load(self) -> SentenceTransformer:
        """Lazy-load the local sentence-transformer model.

        Unless ``TS_ALLOW_ONLINE`` is set, the HuggingFace hub is forced into
        offline mode before the model is constructed — SentenceTransformer
        otherwise downloads uncached weights from the network by default,
        which violates the airgapped/offline-by-default design goal
        (docs/TECH_STACK.md §6). Operators must pre-cache the model weights
        (see the airgapped install docs); a missing cache fails loudly here
        instead of silently reaching out.
        """
        if self.is_remote:
            raise RuntimeError("load() is not available when using a remote embedding endpoint")
        if self._model is None:
            offline = not get_settings().allow_online
            offline_vars = ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE")
            # Scoped to this call (save/restore) rather than setdefault(), which
            # would leave the process permanently forced offline even after a
            # later call with allow_online=True — e.g. across cache_clear() in
            # tests, or a hypothetical settings hot-reload.
            previous = {var: os.environ.get(var) for var in offline_vars}
            try:
                if offline:
                    for var in offline_vars:
                        os.environ[var] = "1"
                try:
                    self._model = SentenceTransformer(self.model_name, device=self.device)
                except OSError as exc:
                    raise RuntimeError(
                        f"Embedding model {self.model_name!r} is not available locally and "
                        "TS_ALLOW_ONLINE is disabled. Pre-cache the model weights on this "
                        "machine (see the airgapped install docs) or set TS_ALLOW_ONLINE=true."
                    ) from exc
            finally:
                for var, value in previous.items():
                    if value is None:
                        os.environ.pop(var, None)
                    else:
                        os.environ[var] = value
        return self._model

    def _get_client(self) -> httpx.Client:
        if self._client is None:
            headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else {}
            self._client = httpx.Client(base_url=self._api_base_url, headers=headers, timeout=60.0)
        return self._client

    def vector_dimension(self) -> int:
        """Return the model's output vector dimension."""
        if self._vector_dimension is not None:
            return self._vector_dimension
        if self.is_remote:
            dimension: int | None = len(self._encode_remote(["dimension probe"])[0])
        else:
            model = self.load()
            dimension = self._get_embedding_dimension(model)
        if dimension is None:
            raise RuntimeError(f"Could not determine vector dimension for {self.model_name}")
        self._vector_dimension = dimension
        return dimension

    @staticmethod
    def _get_embedding_dimension(model: SentenceTransformer) -> int | None:
        """Return the embedding dimension, supporting old and new API names."""
        if hasattr(model, "get_embedding_dimension"):
            return model.get_embedding_dimension()
        return model.get_sentence_embedding_dimension()

    def encode(self, texts: list[str]) -> list[list[float]]:
        """Encode a batch of log lines into vectors."""
        if self.is_remote:
            return self._encode_remote(texts)
        model = self.load()
        embeddings = model.encode(
            texts,
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=self._normalize,
        )
        return [emb.tolist() for emb in embeddings]

    def _encode_remote(self, texts: list[str]) -> list[list[float]]:
        """Encode a batch of texts via an OpenAI-compatible ``/embeddings`` endpoint."""
        client = self._get_client()
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            response = client.post(
                "/embeddings",
                json={"model": self.model_name, "input": batch, "encoding_format": "float"},
            )
            response.raise_for_status()
            data = sorted(response.json()["data"], key=lambda item: item["index"])
            vectors.extend(item["embedding"] for item in data)
        if self._normalize:
            vectors = [_l2_normalize(vector) for vector in vectors]
        return vectors

    def as_config(self) -> EmbeddingConfig:
        """Return an :py:class:`EmbeddingConfig` with vector dimension resolved."""
        return self.config


def _l2_normalize(vector: list[float]) -> list[float]:
    """L2-normalize a vector, matching sentence-transformers' local behaviour."""
    norm = sum(x * x for x in vector) ** 0.5
    if norm == 0:
        return vector
    return [x / norm for x in vector]


def make_embedding_config(**overrides: Any) -> EmbeddingConfig:
    """Build an :py:class:`EmbeddingConfig` from settings and overrides."""
    settings = get_settings()
    device = overrides.get("device") or (
        "remote" if settings.embedding_api_base_url else settings.embedding_device
    )
    return EmbeddingConfig(
        model_name=overrides.get("model_name") or settings.embedding_model,
        device=device,
        vector_dimension=overrides.get("vector_dimension"),
        normalize=overrides.get("normalize", True),
        pooling=overrides.get("pooling", "mean"),
    )
