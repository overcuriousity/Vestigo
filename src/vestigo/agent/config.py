"""Effective agent configuration: env, DB, and default layers resolved per field.

The AI agent can be configured two ways: the ``VESTIGO_AGENT_*`` environment
variables (the deploy-time layer, set once by whoever runs the container) and
the DB-backed ``agent_settings`` singleton row (the runtime layer, editable by
an admin from the UI without a restart — see ``PostgresStore.get_agent_settings``
and the admin router that lands in a later task). :func:`resolve_agent_config`
merges the two **per field**, not as a whole-object override: env wins if the
operator set it, else the DB value if an admin set it, else a hardcoded
default. This lets an operator env-pin just the parts they don't want
touched (e.g. ``api_base_url``) while leaving the rest admin-editable.

Pydantic-settings only adds a field to ``Settings.model_fields_set`` when it
was actually supplied (by an env var, in this app's case) rather than filled
in from the field's own default — that distinction is what lets us tell "env
wins" apart from "the env layer happens to hold its built-in default", e.g.
``agent_provider``'s default of ``"openai"`` must not masquerade as an
explicit env override when nothing set ``VESTIGO_AGENT_PROVIDER``.

The DB read is best-effort: at early startup, or if Postgres is briefly
unreachable, :func:`resolve_agent_config` logs at debug and falls back to
resolving from the env/default layers only, rather than raising. Callers
(availability probing, model construction) must keep working even when the
metadata store is down.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from vestigo.agent.fidelity import DEFAULT_FIDELITY
from vestigo.core.config import Settings, get_settings

logger = logging.getLogger(__name__)

EFFORT_VALUES = ("off", "low", "medium", "high", "max")


def is_kimi_coding_endpoint(base_url: str | None) -> bool:
    """True for Kimi's coding-plan endpoint (Anthropic protocol, UA-gated).

    Lives here (not runtime.py) so both the availability probe and the model
    builder can use it without importing pydantic-ai machinery.
    """
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host == "api.kimi.com" and parsed.path.rstrip("/").startswith("/coding")


_DEFAULT_PROVIDER = "openai"
DEFAULT_MAX_TURNS = 15
_DEFAULT_REASONING_EFFORT = "off"
# Shared with agent/compaction.py's should_compact fallback — one constant so
# the resolver default and the runtime fallback can never drift apart.
DEFAULT_COMPACT_THRESHOLD = 0.85

# (AgentConfig field, Settings attribute, AgentSettingsRow attribute)
_FIELD_MAP: tuple[tuple[str, str, str], ...] = (
    ("model", "agent_model", "model"),
    ("provider", "agent_provider", "provider"),
    ("api_base_url", "agent_api_base_url", "api_base_url"),
    ("api_key", "agent_api_key", "api_key"),
    ("user_agent", "agent_user_agent", "user_agent"),
    ("extra_headers", "agent_extra_headers", "extra_headers"),
    ("max_turns", "agent_max_turns", "max_turns"),
    ("reasoning_effort", "agent_reasoning_effort", "reasoning_effort"),
    ("context_window", "agent_context_window", "context_window"),
    ("compact_threshold", "agent_compact_threshold", "compact_threshold"),
    ("tool_fidelity", "agent_tool_fidelity", "tool_fidelity"),
    ("disabled_tools", "agent_disabled_tools", "disabled_tools"),
)

_DEFAULTS: dict[str, Any] = {
    "model": None,
    "provider": _DEFAULT_PROVIDER,
    "api_base_url": None,
    "api_key": None,
    "user_agent": None,
    "extra_headers": None,
    "max_turns": DEFAULT_MAX_TURNS,
    "reasoning_effort": _DEFAULT_REASONING_EFFORT,
    # None = auto-compaction off; the right window is model-specific.
    "context_window": None,
    "compact_threshold": DEFAULT_COMPACT_THRESHOLD,
    # Assume the deployment has room unless the admin says otherwise; the
    # overflow backstop costs a retry, not the turn. See agent/fidelity.py.
    "tool_fidelity": DEFAULT_FIDELITY.value,
    "disabled_tools": None,
}


@dataclass(frozen=True)
class AgentConfig:
    """Fully-resolved agent configuration for one request/probe.

    ``sources`` records, per field, which layer won: ``"env"``, ``"db"``, or
    ``"default"`` — used by the admin UI to show "pinned by environment"
    badges and by :func:`config_fingerprint` (excluded there, since it's
    metadata about the resolution, not part of the effective config).
    """

    model: str | None
    provider: str
    api_base_url: str | None
    api_key: str | None
    user_agent: str | None
    extra_headers: dict[str, str] | None
    max_turns: int
    reasoning_effort: str
    context_window: int | None = None
    compact_threshold: float | None = None
    tool_fidelity: str = DEFAULT_FIDELITY.value
    disabled_tools: list[str] | None = None
    sources: dict[str, str] = field(default_factory=dict)


def _env_value(settings: Settings, attr: str) -> Any:
    """The env-layer value for one field, or None if never explicitly set.

    Distinguishes "the operator set VESTIGO_AGENT_X" from "the Settings
    field just carries its own hardcoded default" via ``model_fields_set``
    (see module docstring).
    """
    if attr not in settings.model_fields_set:
        return None
    return getattr(settings, attr, None)


async def resolve_agent_config(settings: Settings | None = None) -> AgentConfig:
    """Resolve the effective agent config: env wins, then DB, then default.

    The DB read is best-effort — see module docstring — so this never raises
    on a down/unreachable metadata store; it just resolves from env/defaults.
    """
    settings = settings or get_settings()

    db_row = None
    try:
        from vestigo.api.deps import get_store

        db_row = await get_store().get_agent_settings()
    except Exception:
        logger.debug("Agent settings DB read failed; using env/defaults only", exc_info=True)

    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for config_field, settings_attr, db_attr in _FIELD_MAP:
        env_value = _env_value(settings, settings_attr)
        if env_value is not None:
            resolved[config_field] = env_value
            sources[config_field] = "env"
            continue
        if config_field == "api_key" and settings.agent_secret_mode == "env-only":
            # A10: a key stored before env-only mode was enabled must not be
            # silently used — env (handled above) is the only source.
            db_value = None
        else:
            db_value = getattr(db_row, db_attr, None) if db_row is not None else None
        if db_value is not None:
            resolved[config_field] = db_value
            sources[config_field] = "db"
            continue
        resolved[config_field] = _DEFAULTS[config_field]
        sources[config_field] = "default"

    return AgentConfig(
        model=resolved["model"],
        provider=resolved["provider"],
        api_base_url=resolved["api_base_url"],
        api_key=resolved["api_key"],
        user_agent=resolved["user_agent"],
        extra_headers=resolved["extra_headers"],
        max_turns=resolved["max_turns"],
        reasoning_effort=resolved["reasoning_effort"],
        context_window=resolved["context_window"],
        compact_threshold=resolved["compact_threshold"],
        tool_fidelity=resolved["tool_fidelity"],
        disabled_tools=resolved["disabled_tools"],
        sources=sources,
    )


def config_fingerprint(config: AgentConfig) -> str:
    """Stable sha256 over every effective field except ``sources``.

    Used to key the availability probe cache: a changed fingerprint (an
    admin edited ``agent_settings``, or the process restarted with different
    env) bypasses the probe TTL instead of waiting for it to expire.
    """
    payload = {
        "model": config.model,
        "provider": config.provider,
        "api_base_url": config.api_base_url,
        "api_key": config.api_key,
        "user_agent": config.user_agent,
        "extra_headers": config.extra_headers,
        "max_turns": config.max_turns,
        "reasoning_effort": config.reasoning_effort,
        "context_window": config.context_window,
        "compact_threshold": config.compact_threshold,
        "tool_fidelity": config.tool_fidelity,
        "disabled_tools": config.disabled_tools,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
