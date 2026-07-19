"""Agent availability: configuration check plus a cached endpoint probe.

Mirrors the ``embeddings_available()`` idiom (models/embeddings.py) but goes
one step further: the agent UI must stay invisible unless the configured LLM
endpoint actually answers, so a cheap model-listing probe runs behind a TTL
cache instead of trusting configuration alone.
"""

from __future__ import annotations

import asyncio
import logging
import time

import httpx

from vestigo.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 5.0

# (result, monotonic timestamp) of the last probe; guarded by _probe_lock so
# concurrent /api/health polls don't stampede the endpoint.
_cache: tuple[bool, float] | None = None
_probe_lock = asyncio.Lock()


def agent_configured(settings: Settings | None = None) -> bool:
    """Whether the operator configured the agent at all (no network I/O).

    Requires a model name and — for the ``openai`` provider — a base URL
    (there is no sensible default endpoint). The ``anthropic`` provider falls
    back to Anthropic's own API when no base URL is set, so the key suffices.
    """
    settings = settings or get_settings()
    if not settings.agent_model:
        return False
    if settings.agent_provider == "anthropic":
        return bool(settings.agent_api_base_url or settings.agent_api_key)
    return bool(settings.agent_api_base_url)


def probe_headers(settings: Settings) -> dict[str, str]:
    """HTTP headers for probe and inference requests (UA gate + extras)."""
    headers: dict[str, str] = {}
    if settings.agent_extra_headers:
        headers.update(settings.agent_extra_headers)
    if settings.agent_user_agent:
        headers["User-Agent"] = settings.agent_user_agent
    return headers


def _models_probe_url(settings: Settings) -> str:
    """Model-listing URL used as the availability probe target.

    - openai provider: ``GET {base}/models`` (OpenAI-compatible).
    - anthropic provider: ``GET {base}/v1/models`` — Anthropic's Messages API
      exposes it, and Kimi's coding endpoint serves an OpenAI-compatible list
      at ``{base}/v1/models`` (verified against the Kimi CLI docs and the
      hermes-agent kimi-coding provider).
    """
    base = (settings.agent_api_base_url or "https://api.anthropic.com").rstrip("/")
    if settings.agent_provider == "anthropic":
        return f"{base}/v1/models"
    return f"{base}/models"


async def _probe(settings: Settings) -> bool:
    headers = probe_headers(settings)
    if settings.agent_api_key:
        if settings.agent_provider == "anthropic":
            headers.setdefault("x-api-key", settings.agent_api_key)
            headers.setdefault("anthropic-version", "2023-06-01")
            # Kimi's coding endpoint (Anthropic protocol) authenticates the
            # OpenAI-compatible /v1/models surface with Bearer auth.
            headers.setdefault("Authorization", f"Bearer {settings.agent_api_key}")
        else:
            headers.setdefault("Authorization", f"Bearer {settings.agent_api_key}")
    url = _models_probe_url(settings)
    try:
        async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        logger.warning("Agent endpoint probe failed (%s): %s", url, exc)
        return False
    if response.status_code >= 400:
        logger.warning("Agent endpoint probe got HTTP %s from %s", response.status_code, url)
        return False
    return True


async def agent_available(*, force: bool = False) -> bool:
    """Whether the agent is configured AND its endpoint answers.

    The probe result is cached for ``agent_probe_ttl_seconds``; ``force``
    bypasses the cache (used after config changes in tests).
    """
    global _cache
    settings = get_settings()
    if not agent_configured(settings):
        return False
    async with _probe_lock:
        now = time.monotonic()
        if not force and _cache is not None and now - _cache[1] < settings.agent_probe_ttl_seconds:
            return _cache[0]
        result = await _probe(settings)
        _cache = (result, time.monotonic())
        return result


def reset_probe_cache() -> None:
    """Forget the cached probe result (test helper)."""
    global _cache
    _cache = None
