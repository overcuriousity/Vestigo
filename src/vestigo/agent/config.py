"""Effective agent configuration, resolved from the instance settings.

The agent has no configuration store of its own. Every knob it reads is a
``VESTIGO_AGENT_*`` field on :class:`~vestigo.core.config.Settings`, so it
inherits the instance-wide merge unchanged: the environment wins where the
operator pinned a field, then the admin-edited ``app_settings`` override,
then the field's own default (``core/config.py``, ``core/runtime_settings``).

It used to own a purpose-built ``agent_settings`` singleton row and a second
resolver that merged it against the same env layer by hand — two storage
shapes, two save endpoints and two secret policies for one configuration.
Migration ``0033`` folded that row into ``app_settings`` and dropped it;
:func:`resolve_agent_config` is now a projection of the already-merged
settings object onto :class:`AgentConfig`, which is why it performs no I/O
and cannot fail on an unreachable metadata store.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

from vestigo.agent.fidelity import DEFAULT_FIDELITY
from vestigo.core.config import Settings, env_pinned, get_settings, runtime_overrides

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


#: Read off the settings field rather than restated, so the two cannot drift.
DEFAULT_MAX_TURNS: int = Settings.model_fields["agent_max_turns"].default
_DEFAULT_REASONING_EFFORT = "off"

#: (AgentConfig field, Settings field). The names differ only by the
#: ``agent_`` prefix the instance-wide namespace requires; the mapping is
#: explicit rather than derived so a renamed setting fails loudly here.
_FIELD_MAP: tuple[tuple[str, str], ...] = (
    ("model", "agent_model"),
    ("provider", "agent_provider"),
    ("api_base_url", "agent_api_base_url"),
    ("api_key", "agent_api_key"),
    ("user_agent", "agent_user_agent"),
    ("extra_headers", "agent_extra_headers"),
    ("max_turns", "agent_max_turns"),
    ("reasoning_effort", "agent_reasoning_effort"),
    ("context_window", "agent_context_window"),
    ("tool_fidelity", "agent_tool_fidelity"),
    ("disabled_tools", "agent_disabled_tools"),
)

#: Fields whose ``Settings`` default is ``None`` — meaning "the admin has no
#: opinion" — but whose :class:`AgentConfig` contract is a concrete value.
#: The substitution happens here so every consumer sees a resolved string
#: while the console still renders the field as unset.
_UNSET_FALLBACKS: dict[str, Any] = {
    # Reasoning is off unless asked for; a model that ignores the parameter
    # must not be charged for it by default.
    "reasoning_effort": _DEFAULT_REASONING_EFFORT,
    # Assume the deployment has room unless the admin says otherwise; the
    # sliding window costs a retry at worst, not the turn. See agent/window.py.
    "tool_fidelity": DEFAULT_FIDELITY.value,
}


@dataclass(frozen=True)
class AgentConfig:
    """Fully-resolved agent configuration for one request/probe.

    ``sources`` records, per field, which layer won: ``"env"``, ``"db"``, or
    ``"default"`` — used by the admin console to show "pinned by environment"
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
    tool_fidelity: str = DEFAULT_FIDELITY.value
    disabled_tools: list[str] | None = None
    sources: dict[str, str] = field(default_factory=dict)


def _source_of(settings_field: str) -> str:
    """Which layer supplied this field: ``env``, ``db``, or ``default``.

    Asks the instance-wide layers directly rather than re-deriving them:
    ``env_pinned`` consults the *unmerged* settings object (an override would
    pollute ``model_fields_set``), and an applied override is by construction
    a field the environment did not pin.
    """
    if env_pinned(settings_field):
        return "env"
    return "db" if settings_field in runtime_overrides() else "default"


def resolve_agent_config() -> AgentConfig:
    """Project the effective instance settings onto :class:`AgentConfig`.

    Performs no I/O: the env/DB/default merge already happened in
    ``core/config.py``, so this cannot fail on an unreachable metadata store
    and needs no best-effort fallback of its own.

    Takes no settings object on purpose. ``sources`` describes the process-wide
    layers — which of them supplied each field — and :func:`_source_of` can
    only ask those; a caller-supplied ``Settings`` would get values from one
    object and provenance from another, and ``sources`` is what the model
    endpoint's env-pin guard trusts.
    """
    settings = get_settings()
    resolved: dict[str, Any] = {}
    sources: dict[str, str] = {}
    for config_field, settings_field in _FIELD_MAP:
        value = getattr(settings, settings_field)
        source = _source_of(settings_field)
        if value is None and config_field in _UNSET_FALLBACKS:
            value = _UNSET_FALLBACKS[config_field]
            source = "default"
        resolved[config_field] = value
        sources[config_field] = source
    return AgentConfig(**resolved, sources=sources)


def config_fingerprint(config: AgentConfig) -> str:
    """Stable sha256 over every effective field except ``sources``.

    Used to key the availability probe cache: a changed fingerprint (an
    admin edited the agent settings, or the process restarted with different
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
        "tool_fidelity": config.tool_fidelity,
        "disabled_tools": config.disabled_tools,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()
