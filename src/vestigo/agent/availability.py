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

from vestigo.agent.config import AgentConfig, config_fingerprint, resolve_agent_config
from vestigo.core.config import get_settings

logger = logging.getLogger(__name__)

_PROBE_TIMEOUT = 5.0

# (result, monotonic timestamp, config fingerprint) of the last probe;
# guarded by _probe_lock so concurrent /api/health polls don't stampede the
# endpoint. A fingerprint mismatch bypasses the TTL (see module docstring).
_cache: tuple[bool, float, str] | None = None
_probe_lock = asyncio.Lock()


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


async def _probe(config: AgentConfig) -> bool:
    headers = probe_headers(config)
    if config.api_key:
        if config.provider == "anthropic":
            headers.setdefault("x-api-key", config.api_key)
            headers.setdefault("anthropic-version", "2023-06-01")
            # Kimi's coding endpoint (Anthropic protocol) authenticates the
            # OpenAI-compatible /v1/models surface with Bearer auth.
            headers.setdefault("Authorization", f"Bearer {config.api_key}")
        else:
            headers.setdefault("Authorization", f"Bearer {config.api_key}")
    url = _models_probe_url(config)
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
    bypasses the cache (used after config changes in tests). A change in the
    resolved config's fingerprint also bypasses the TTL — see module
    docstring.
    """
    global _cache
    settings = get_settings()
    config = await resolve_agent_config(settings)
    if not agent_configured(config):
        return False
    fingerprint = config_fingerprint(config)
    async with _probe_lock:
        now = time.monotonic()
        if (
            not force
            and _cache is not None
            and _cache[2] == fingerprint
            and now - _cache[1] < settings.agent_probe_ttl_seconds
        ):
            return _cache[0]
        result = await _probe(config)
        _cache = (result, time.monotonic(), fingerprint)
        return result


def reset_probe_cache() -> None:
    """Forget the cached probe result (test helper)."""
    global _cache
    _cache = None
