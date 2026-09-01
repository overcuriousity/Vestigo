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

- **The vector store.** A server Qdrant answering ``get_collections()``.
  Configuration alone cannot stand in for this: ``qdrant_url`` has a non-null
  default, so every instance looks configured whether or not anything is
  listening. The *embedded on-disk* mode (``VESTIGO_QDRANT_PATH``) is the one
  arm that stays a check rather than a call, because qdrant-client locks the
  storage folder exclusively and this process already holds that lock whenever
  an embedding job or the similarity service is alive — see
  :func:`_probe_local_store`.
- **The embedding model.** A remote endpoint is probed with a one-token
  ``/embeddings`` request — reachable, authenticated and actually serving the
  configured model. The *local* arm never loads the model, because proving it
  works that way means a ~90 MB load no health poll can afford; it asks
  whether the extra is importable and, on a host that is not allowed online,
  whether the weights are already in the local cache. That second half is the
  airgap fix: the reference Docker bundle shipped the extra and ran Qdrant, so
  it advertised the whole embedding UI for a model it had never been able to
  download. An online host is not asked, since it fetches the weights on first
  use.

Ahead of both arms sits an operator switch, ``embeddings_enabled``, off by
default. It is checked first and short-circuits everything: a subsystem the
operator has turned off must not open a socket to report that it is off, and
no probe result can turn it back on.

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
import os
import time
from pathlib import Path

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
        str(bool(settings.embeddings_enabled)),
        str(bool(settings.allow_online)),
        settings.qdrant_url or "",
        settings.qdrant_path or "",
        str(bool(settings.qdrant_api_key)),
        settings.embedding_api_base_url or "",
        settings.embedding_model or "",
        str(bool(settings.embedding_api_key)),
    )
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()[:16]


def subsystem_enabled() -> bool:
    """The operator's master switch for embeddings.

    Off by default, and checked before anything else: a switched-off subsystem
    must not open a socket to report that it is off, and no probe result can
    turn it back on. It exists alongside the probe rather than instead of it:
    the probe answers "is anything listening", which an operator cannot state
    for a subsystem they have deliberately not set up. See
    ``docs/DEPLOYMENT.md`` on optional subsystems.
    """
    return bool(get_settings().embeddings_enabled)


def _local_stack_importable() -> bool:
    """Whether the ``embeddings`` extra is installed. Never imports it."""
    import importlib.util

    return importlib.util.find_spec("sentence_transformers") is not None


def _local_weights_present() -> bool:
    """Whether the configured local model's weights are already on this host.

    Checked, never fetched, and never loaded: a bare ``config.json`` lookup in
    the Hugging Face cache costs a stat or two, which is what makes it
    affordable on the synchronous request path that ``embeddings_available()``
    sits on. A model name that is an existing directory is a local model
    already — sentence-transformers accepts a path — so it needs no cache at
    all.

    Bare names are resolved the way sentence-transformers resolves them: a
    name with no owner is looked up under ``sentence-transformers/`` too, which
    is where the default ``all-MiniLM-L6-v2`` actually lives.
    """
    name = get_settings().embedding_model
    if not name:
        return False
    if Path(name).is_dir():
        return True
    try:
        from huggingface_hub import try_to_load_from_cache
    except ImportError:  # no hub, so no cache to find weights in
        return False
    candidates = [name] if "/" in name else [name, f"sentence-transformers/{name}"]
    for repo in candidates:
        for filename in ("config.json", "modules.json"):
            try:
                hit = try_to_load_from_cache(repo, filename)
            except Exception:  # a malformed repo id is not a crash, it is a miss
                continue
            if isinstance(hit, str):
                return True
    return False


def model_configured() -> bool:
    """Whether anything on this instance can turn text into a vector.

    True when a remote OpenAI-compatible endpoint is configured, or when the
    local sentence-transformers stack is importable (the ``embeddings`` extra)
    *and* its weights can actually be obtained.

    That last clause is the airgap fix. The extra being importable used to be
    the whole local answer, and the module docstring conceded the gap in so
    many words: "an airgapped box that never fetched the weights would fail
    anyway". It did — the reference Docker bundle shipped the extra and ran
    Qdrant, so every timeline offered "Improve search quality" for a model the
    host had never been online to download. Weights are therefore required
    exactly where they cannot appear on their own: with ``allow_online`` off.
    An online host is left alone, because it downloads them on first use and
    hiding the feature until someone embeds once would be its own bug.

    Still cheap, and still never loads the model.
    """
    if get_settings().embedding_api_base_url:
        return True
    if not _local_stack_importable():
        return False
    if get_settings().allow_online:
        return True
    return _local_weights_present()


def vector_store_configured() -> bool:
    """Whether a vector store is declared at all.

    ``qdrant_url`` defaults to ``http://localhost:6333``, so this is true for a
    stock install and false only where the operator cleared both it and
    ``qdrant_path`` — the supported way to say "this deployment has no vector
    store" without waiting for a probe to time out.
    """
    settings = get_settings()
    return bool(settings.qdrant_url or settings.qdrant_path)


def _probe_local_store(path: str) -> bool:
    """Whether the embedded on-disk Qdrant at ``path`` is usable.

    Deliberately a directory check and **not** a client construction.
    qdrant-client's local mode takes an exclusive lock on the storage folder
    for the lifetime of the client, and this process already holds one
    whenever a :class:`~vestigo.db.qdrant.QdrantStore` is alive — the
    similarity service is a process-lifetime singleton, and every embedding
    job builds a store for its duration. A probe client would therefore start
    raising as soon as anyone used embeddings once, flipping the capability
    off on a perfectly healthy instance until restart; and in the window it
    *did* hold the lock it would make a starting embed job fail. Neither is a
    trade a health poll gets to make.

    So the local arm claims only what it can check for free: the storage
    directory exists and is writable, or its parent is, so the client can
    create it. That is weaker than the server arm's "it answered", and the
    module docstring says so.
    """
    target = Path(path)
    probe = target if target.exists() else target.parent
    return probe.is_dir() and os.access(probe, os.W_OK | os.X_OK)


def _probe_vector_store() -> bool:
    """Ask Qdrant to list its collections. Blocking; run in a thread.

    Constructed here rather than through :class:`~vestigo.db.qdrant.QdrantStore`
    so the probe can impose its own timeout: the store is built for ingest and
    query paths that may legitimately wait, while a probe that hangs would hold
    the cold-cache path of ``/api/health`` open for as long as the socket does.

    Local (on-disk) mode cannot be probed this way at all — see
    :func:`_probe_local_store`.
    """
    settings = get_settings()
    try:
        if settings.qdrant_path:
            return _probe_local_store(settings.qdrant_path)

        from qdrant_client import QdrantClient

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
    except Exception as exc:
        # Not just httpx.HTTPError: httpx.InvalidURL is not one of those, and a
        # misconfigured base URL must read as "unavailable", never as a 500 on
        # /api/health.
        logger.warning("Embedding endpoint probe failed (%s): %s", url, exc)
        return False
    if response.status_code >= 400:
        logger.warning("Embedding endpoint probe got HTTP %s from %s", response.status_code, url)
        return False
    return True


async def _probe() -> bool:
    """Both arms. Either one missing means no embedding job can complete.

    Nothing escapes: this runs behind ``get_capabilities()``, so an exception
    here would take ``/api/health`` to a 500 and cost the frontend its gating
    for *every* subsystem — agent, OIDC, transfer, MCP — over one optional one.
    A failure to answer is an answer: unavailable.
    """
    try:
        if not model_configured() or not vector_store_configured():
            return False
        if get_settings().embedding_api_base_url and not await _probe_embedding_endpoint():
            return False
        return await asyncio.to_thread(_probe_vector_store)
    except Exception:
        logger.warning("Embeddings availability probe failed", exc_info=True)
        return False


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
    """Start a background re-probe unless one is already running *on this loop*.

    The loop check is not paranoia: a task created on a loop that is torn down
    before it ever ran stays ``done() == False`` forever, and a bare
    "is one in flight?" guard would then refuse to schedule another for the
    life of the process — silently downgrading stale-while-revalidate to
    "only the synchronous paths ever refresh". Test clients and any loop
    replacement hit exactly that.
    """
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        try:
            same_loop = _refresh_task.get_loop() is asyncio.get_running_loop()
        except RuntimeError:  # no running loop — nothing to schedule on anyway
            return
        if same_loop:
            return
    _refresh_task = asyncio.create_task(_refresh_cache(fingerprint))


async def embeddings_operational(*, force: bool = False) -> bool:
    """Whether an embedding job on this instance could actually complete.

    The full answer, and the one ``/api/health`` reports: the operator switch
    on, both arms configured, and both probed. Cached for ``embedding_probe_ttl_seconds``; ``force``
    bypasses the cache, and a fingerprint change bypasses the TTL.

    Stale-while-revalidate: a same-fingerprint entry that has merely outlived
    its TTL is returned immediately while a background task re-probes, so a
    hung Qdrant cannot make health slow. Only a cold cache or a settings edit
    probes synchronously — which is what makes the capability correct on the
    poll right after an admin changes something.
    """
    global _cache
    if not subsystem_enabled():
        return False
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
    if not subsystem_enabled():
        return (
            "Embeddings are switched off for this instance. An administrator can turn "
            "them on under Settings \u2192 Embeddings (or with "
            "VESTIGO_EMBEDDINGS_ENABLED=true); everything else about the subsystem is "
            "checked only after that."
        )
    if not model_configured():
        if not settings.embedding_api_base_url and _local_stack_importable():
            # The extra is here; what is missing is the weights, and this host
            # is not allowed to fetch them. Saying "install the extra" would
            # send an airgapped operator a long way in the wrong direction.
            return (
                f"The local embedding model ({settings.embedding_model}) has no weights "
                "on this host, and VESTIGO_ALLOW_ONLINE is false so they cannot be "
                "downloaded. Pre-populate the Hugging Face cache, point "
                "VESTIGO_EMBEDDING_MODEL at a local model directory, or configure "
                "VESTIGO_EMBEDDING_API_BASE_URL for a remote embedding endpoint."
            )
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
    target = settings.qdrant_url or settings.qdrant_path
    # cached_result(), not _cache: a record left over from the settings that
    # were in force before an admin's PUT would name the wrong endpoint.
    if cached_result() is False:
        if settings.embedding_api_base_url:
            return (
                "The embedding backend did not answer. Check that the vector store "
                f"({target}) and the embedding endpoint "
                f"({settings.embedding_api_base_url}) are reachable from this host."
            )
        return (
            f"The vector store did not answer. Check that Qdrant ({target}) is "
            "reachable from this host."
        )
    # Nothing has probed these settings yet, so name the parts without
    # claiming a probe result this function does not have.
    endpoint = (
        f" and the embedding endpoint ({settings.embedding_api_base_url})"
        if settings.embedding_api_base_url
        else ""
    )
    return (
        "Embedding features are not available right now. Check that the vector "
        f"store ({target}){endpoint} are reachable from this host."
    )


def reset_probe_cache() -> None:
    """Forget the cached probe result and any in-flight refresh (test helper)."""
    global _cache, _refresh_task
    _cache = None
    _refresh_task = None
