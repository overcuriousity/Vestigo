"""Agent availability: configuration check plus a cached endpoint probe.

Mirrors the ``embeddings_available()`` idiom (models/embeddings.py) but goes
one step further: the agent UI must stay invisible unless the configured LLM
endpoint actually answers, so a cheap model-listing probe runs behind a TTL
cache instead of trusting configuration alone.

The probe cache is keyed on the resolved :class:`~vestigo.agent.config.AgentConfig`'s
fingerprint (``config.py``'s env/DB/default merge), not just wall-clock time:
if an admin edits the DB-backed agent settings (or the env layer changes
across a restart), the fingerprint changes and the next ``agent_available()``
call re-probes immediately regardless of how recently the TTL last fired.
This is the probe-invalidation mechanism for the admin settings PUT endpoint
— no manual cache bump required there.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from vestigo.agent.config import (
    AgentConfig,
    config_fingerprint,
    is_kimi_coding_endpoint,
    resolve_agent_config,
)
from vestigo.core.config import get_settings

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 5.0

# (result, monotonic timestamp, config fingerprint) of the last probe;
# guarded by _probe_lock so concurrent /api/health polls don't stampede the
# endpoint. A fingerprint mismatch bypasses the TTL (see module docstring).
_cache: tuple[bool, float, str] | None = None
_probe_lock = asyncio.Lock()
# In-flight stale-while-revalidate refresh; at most one at a time.
_refresh_task: asyncio.Task[None] | None = None


def agent_configured(config: AgentConfig) -> bool:
    """Whether the operator configured the agent at all (no network I/O).

    Requires a model name and — for the ``openai`` provider — a base URL
    (there is no sensible default endpoint). The ``anthropic`` provider falls
    back to Anthropic's own API when no base URL is set, so the key suffices.
    """
    if not config.model:
        return False
    if config.provider == "anthropic":
        return bool(config.api_base_url or config.api_key)
    return bool(config.api_base_url)


def probe_headers(config: AgentConfig) -> dict[str, str]:
    """HTTP headers for probe and inference requests (UA gate + extras)."""
    headers: dict[str, str] = {}
    if config.extra_headers:
        headers.update(config.extra_headers)
    if config.user_agent:
        headers["User-Agent"] = config.user_agent
    return headers


def _models_probe_url(config: AgentConfig) -> str:
    """Model-listing URL used as the availability probe target.

    - openai provider: ``GET {base}/models`` (OpenAI-compatible).
    - anthropic provider: ``GET {base}/v1/models`` — Anthropic's Messages API
      exposes it, and Kimi's coding endpoint serves an OpenAI-compatible list
      at ``{base}/v1/models`` (verified against the Kimi CLI docs and the
      hermes-agent kimi-coding provider).
    """
    base = (config.api_base_url or "https://api.anthropic.com").rstrip("/")
    if config.provider == "anthropic":
        return f"{base}/v1/models"
    return f"{base}/models"


def _models_headers(config: AgentConfig) -> dict[str, str]:
    """Headers for the model-listing request, including auth."""
    headers = probe_headers(config)
    if config.api_key:
        if config.provider == "anthropic":
            headers.setdefault("x-api-key", config.api_key)
            headers.setdefault("anthropic-version", "2023-06-01")
            # Kimi's coding endpoint (Anthropic protocol) authenticates the
            # OpenAI-compatible /v1/models surface with Bearer auth. Only Kimi
            # gets the duplicate — never send the key in a second header to
            # arbitrary anthropic-protocol endpoints.
            if is_kimi_coding_endpoint(config.api_base_url):
                headers.setdefault("Authorization", f"Bearer {config.api_key}")
        else:
            headers.setdefault("Authorization", f"Bearer {config.api_key}")
    return headers


async def _get_models(config: AgentConfig) -> httpx.Response | None:
    """GET the model-listing endpoint. None means it did not answer usably."""
    url = _models_probe_url(config)
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(url, headers=_models_headers(config))
    except httpx.HTTPError as exc:
        logger.warning("Agent endpoint probe failed (%s): %s", url, exc)
        return None
    if response.status_code >= 400:
        logger.warning("Agent endpoint probe got HTTP %s from %s", response.status_code, url)
        return None
    return response


async def list_models(config: AgentConfig) -> list[str]:
    """Model ids the configured endpoint advertises, best-effort.

    Both the OpenAI-compatible and Anthropic listings are ``{"data": [{"id":
    ...}]}``, so one shape covers both. Every failure — unreachable, auth
    rejected, unparseable, or an endpoint that simply serves no listing —
    collapses to an empty list: the caller's fallback is free-text model
    entry, so there is nothing useful to distinguish here.
    """
    response = await _get_models(config)
    if response is None:
        return []
    try:
        body = response.json()
    except ValueError:
        logger.warning("Agent model listing was not JSON (%s)", _models_probe_url(config))
        return []
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        return []
    models = {
        entry["id"]
        for entry in data
        if isinstance(entry, dict) and isinstance(entry.get("id"), str) and entry["id"]
    }
    return sorted(models)


async def _probe(config: AgentConfig) -> bool:
    """The availability probe: the endpoint answering its listing at all."""
    return (await _get_models(config)) is not None


async def _refresh_cache(config: AgentConfig, fingerprint: str) -> None:
    """Background re-probe: refresh the cache unless someone beat us to it."""
    global _cache
    async with _probe_lock:
        if (
            _cache is not None
            and _cache[2] == fingerprint
            and time.monotonic() - _cache[1] < get_settings().agent_probe_ttl_seconds
        ):
            return
        result = await _probe(config)
        _cache = (result, time.monotonic(), fingerprint)


def _schedule_refresh(config: AgentConfig, fingerprint: str) -> None:
    """Start a background re-probe unless one is already running *on this loop*.

    A task created on a loop that is torn down before it ran stays
    ``done() == False`` forever, so a bare in-flight guard would refuse to
    schedule another for the life of the process and quietly downgrade
    stale-while-revalidate to "only the synchronous paths refresh".
    """
    global _refresh_task
    if _refresh_task is not None and not _refresh_task.done():
        try:
            same_loop = _refresh_task.get_loop() is asyncio.get_running_loop()
        except RuntimeError:  # no running loop — nothing to schedule on anyway
            return
        if same_loop:
            return
    _refresh_task = asyncio.create_task(_refresh_cache(config, fingerprint))


async def agent_available(*, force: bool = False) -> bool:
    """Whether the agent is configured AND its endpoint answers.

    The probe result is cached for ``agent_probe_ttl_seconds``; ``force``
    bypasses the cache (used after config changes in tests). A change in the
    resolved config's fingerprint also bypasses the TTL — see module
    docstring.

    Stale-while-revalidate: when a same-fingerprint entry has merely outlived
    its TTL, the stale value is returned immediately and a background task
    re-probes — so ``/api/health`` never blocks up to the probe timeout on a
    hung LLM endpoint. Only a cold cache or a fingerprint change (config
    edit) probes synchronously, keeping availability gating correct right
    after an admin changes settings.
    """
    global _cache
    settings = get_settings()
    config = await resolve_agent_config(settings)
    if not agent_configured(config):
        return False
    fingerprint = config_fingerprint(config)
    if not force and _cache is not None and _cache[2] == fingerprint:
        if time.monotonic() - _cache[1] < settings.agent_probe_ttl_seconds:
            return _cache[0]
        _schedule_refresh(config, fingerprint)
        return _cache[0]
    async with _probe_lock:
        # Re-check under the lock — a concurrent caller may have probed.
        if (
            not force
            and _cache is not None
            and _cache[2] == fingerprint
            and time.monotonic() - _cache[1] < settings.agent_probe_ttl_seconds
        ):
            return _cache[0]
        result = await _probe(config)
        _cache = (result, time.monotonic(), fingerprint)
        return result


def reset_probe_cache() -> None:
    """Forget the cached probe result and any in-flight refresh (test helper)."""
    global _cache, _refresh_task
    _cache = None
    _refresh_task = None
