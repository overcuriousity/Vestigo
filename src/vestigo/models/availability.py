"""Embeddings availability: configuration check plus cached probes of both arms.

``embeddings_available()`` used to answer "can this instance *compute* a
vector" — the ``embeddings`` extra importable, or a remote endpoint
configured — and never asked whether anything could *store* one. An operator
who removes Qdrant from their stack therefore kept the whole embedding UI:
"Improve search quality" on every timeline, a wizard that opens, and a job
that fails at the vector store instead of a button that was never offered.
That is precisely what ``core/capabilities.py`` exists to prevent, in its own
words — an unconfigured subsystem renders *no* entry point rather than a
disabled one.

So availability is both arms, each actually probed:

- **The vector store.** Qdrant answering ``get_collections()``. Configuration
  alone cannot stand in for this: ``qdrant_url`` has a non-null default, so
  every instance looks configured whether or not anything is listening.
- **The embedding model.** A remote endpoint is probed with a one-token
  ``/embeddings`` request — reachable, authenticated and actually serving the
  configured model. The *local* arm stays a configuration check
  (``sentence_transformers`` importable), because proving it works means
  loading a ~90 MB model, which no health poll can afford and which an
  airgapped box that never fetched the weights would fail anyway. The honest
  claim is "the library is installed", and this module does not pretend
  otherwise.

Structure follows :mod:`vestigo.agent.availability`, for the same reasons it
has that structure: a TTL cache keyed on a fingerprint of the settings the
probe depends on, so an admin's settings edit re-probes immediately instead of
waiting out the TTL, and stale-while-revalidate so ``/api/health`` never blocks
on a hung Qdrant.

The sync :func:`embeddings_available` stays sync and never does I/O — it is
called from request paths and from the agent's tool registration. It reads the
cached record, and a *cold* cache is not the same answer as "unavailable": it
means nobody has looked yet, so it falls back to the configuration check rather
than hiding a working subsystem. Startup fills the cache, and every
``/api/health`` poll keeps it fresh, so the cold window is the first seconds of
a process.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time

import httpx

from vestigo.core.config import get_settings

logger = logging.getLogger(__name__)

#: Wall clock for one arm of the probe. Short on purpose: this runs behind
#: /api/health, and a dead Qdrant should read as dead well inside one poll.
_PROBE_TIMEOUT = 5.0

#: (result, monotonic timestamp, settings fingerprint) of the last probe.
_cache: tuple[bool, float, str] | None = None
_probe_lock = asyncio.Lock()
#: In-flight stale-while-revalidate refresh; at most one at a time.
_refresh_task: asyncio.Task[None] | None = None


def _fingerprint() -> str:
    """Fingerprint of every setting the probe's answer depends on.

    A change here bypasses the TTL, which is what makes an admin settings PUT
    take effect on the next poll rather than up to a minute later. The API key
    contributes only its presence: the fingerprint is not a secret store, and
    rotating a key to another valid one cannot change whether the arm answers.
    """
    settings = get_settings()
    parts = (
        settings.qdrant_url or "",
        settings.qdrant_path or "",
        str(bool(settings.qdrant_api_key)),
        settings.embedding_api_base_url or "",
        settings.embedding_model or "",
        str(bool(settings.embedding_api_key)),
    )
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def model_configured() -> bool:
    """Whether anything on this instance can turn text into a vector.

    True when the local sentence-transformers stack is importable (the
    ``embeddings`` extra is installed) OR a remote OpenAI-compatible endpoint
    is configured. Cheap: checks importability, never loads the model.
    """
    import importlib.util

    if get_settings().embedding_api_base_url:
        return True
    return importlib.util.find_spec("sentence_transformers") is not None


def vector_store_configured() -> bool:
    """Whether a vector store is declared at all.

    ``qdrant_url`` defaults to ``http://localhost:6333``, so this is true for a
    stock install and false only where the operator cleared both it and
    ``qdrant_path`` — the supported way to say "this deployment has no vector
    store" without waiting for a probe to time out.
    """
    settings = get_settings()
    return bool(settings.qdrant_url or settings.qdrant_path)


def _probe_vector_store() -> bool:
    """Ask Qdrant to list its collections. Blocking; run in a thread.

    Constructed here rather than through :class:`~vestigo.db.qdrant.QdrantStore`
    so the probe can impose its own timeout: the store is built for ingest and
    query paths that may legitimately wait, while a probe that hangs would hold
    the cold-cache path of ``/api/health`` open for as long as the socket does.
    """
    from qdrant_client import QdrantClient

    settings = get_settings()
    try:
        if settings.qdrant_path:
            client = QdrantClient(path=settings.qdrant_path)
        else:
            client = QdrantClient(
                url=settings.qdrant_url,
                api_key=settings.qdrant_api_key,
                timeout=int(_PROBE_TIMEOUT),
            )
        try:
            client.get_collections()
        finally:
            client.close()
    except Exception as exc:
        logger.warning("Vector store probe failed (%s): %s", settings.qdrant_url, exc)
        return False
    return True


async def _probe_embedding_endpoint() -> bool:
    """Embed one token against the configured remote endpoint.

    A real ``/embeddings`` call rather than a model listing: this arm is meant
    to answer "will an embedding job get vectors back", and a listing proves
    neither that the configured model exists nor that the key is accepted for
    inference. One token is the cheapest request that does.
    """
    settings = get_settings()
    base = (settings.embedding_api_base_url or "").rstrip("/")
    url = f"{base}/embeddings"
    headers = {}
    if settings.embedding_api_key:
        headers["Authorization"] = f"Bearer {settings.embedding_api_key}"
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.post(
                url,
                headers=headers,
                json={"model": settings.embedding_model, "input": "ping"},
            )
    except httpx.HTTPError as exc:
        logger.warning("Embedding endpoint probe failed (%s): %s", url, exc)
        return False
    if response.status_code >= 400:
        logger.warning("Embedding endpoint probe got HTTP %s from %s", response.status_code, url)
        return False
    return True


async def _probe() -> bool:
    """Both arms. Either one missing means no embedding job can complete."""
    if not model_configured() or not vector_store_configured():
        return False
    if get_settings().embedding_api_base_url and not await _probe_embedding_endpoint():
        return False
    return await asyncio.to_thread(_probe_vector_store)


async def _refresh_cache(fingerprint: str) -> None:
    """Background re-probe: refresh the cache unless someone beat us to it."""
    global _cache
    async with _probe_lock:
        if (
            _cache is not None
            and _cache[2] == fingerprint
            and time.monotonic() - _cache[1] < get_settings().embedding_probe_ttl_seconds
        ):
            return
        _cache = (await _probe(), time.monotonic(), fingerprint)


def _schedule_refresh(fingerprint: str) -> None:
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        return
    _refresh_task = asyncio.create_task(_refresh_cache(fingerprint))


async def embeddings_operational(*, force: bool = False) -> bool:
    """Whether an embedding job on this instance could actually complete.

    The full answer, and the one ``/api/health`` reports: both arms configured,
    and both probed. Cached for ``embedding_probe_ttl_seconds``; ``force``
    bypasses the cache, and a fingerprint change bypasses the TTL.

    Stale-while-revalidate: a same-fingerprint entry that has merely outlived
    its TTL is returned immediately while a background task re-probes, so a
    hung Qdrant cannot make health slow. Only a cold cache or a settings edit
    probes synchronously — which is what makes the capability correct on the
    poll right after an admin changes something.
    """
    global _cache
    fingerprint = _fingerprint()
    ttl = get_settings().embedding_probe_ttl_seconds
    if not force and _cache is not None and _cache[2] == fingerprint:
        if time.monotonic() - _cache[1] < ttl:
            return _cache[0]
        _schedule_refresh(fingerprint)
        return _cache[0]
    async with _probe_lock:
        # Re-check under the lock — a concurrent caller may have probed.
        if (
            not force
            and _cache is not None
            and _cache[2] == fingerprint
            and time.monotonic() - _cache[1] < ttl
        ):
            return _cache[0]
        result = await _probe()
        _cache = (result, time.monotonic(), fingerprint)
        return result


def cached_result() -> bool | None:
    """The last probe's answer, or ``None`` when nobody has probed yet.

    ``None`` is deliberately distinct from ``False``: see the module docstring
    on why a cold cache must not hide a working subsystem.
    """
    if _cache is None or _cache[2] != _fingerprint():
        return None
    return _cache[0]


def unavailable_detail() -> str:
    """Why embedding features are refused right now, in an operator's terms.

    One message per arm, because the arms have nothing to do with each other:
    telling someone whose Qdrant is gone to install a 2 GB ML extra sends them
    a long way in the wrong direction, and it is the message they got before
    the vector store was part of this answer at all.
    """
    settings = get_settings()
    if not model_configured():
        return (
            "Embedding support is not installed. Install the 'embeddings' extra "
            "(uv sync --extra embeddings) or configure VESTIGO_EMBEDDING_API_BASE_URL "
            "to use a remote embedding endpoint."
        )
    if not vector_store_configured():
        return (
            "No vector store is configured, so there is nowhere to put embeddings. "
            "Set VESTIGO_QDRANT_URL (or VESTIGO_QDRANT_PATH for local mode)."
        )
    if settings.embedding_api_base_url and _cache is not None and not _cache[0]:
        return (
            "The embedding backend did not answer. Check that the vector store "
            f"({settings.qdrant_url or settings.qdrant_path}) and the embedding endpoint "
            f"({settings.embedding_api_base_url}) are reachable from this host."
        )
    return (
        "The vector store did not answer. Check that Qdrant "
        f"({settings.qdrant_url or settings.qdrant_path}) is reachable from this host."
    )


def reset_probe_cache() -> None:
    """Forget the cached probe result (test helper)."""
    global _cache
    _cache = None
