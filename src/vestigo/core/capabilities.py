"""Which optional subsystems this installation can actually use.

An unconfigured subsystem is *invisible*, not merely disabled: the frontend
renders no entry point for it and the agent's tool server never registers its
tools, so the model is not tempted to call something that can only answer with
an error. The AI agent has behaved this way since it shipped; this module
generalizes the rule to every optional subsystem so the answer comes from one
place (``GET /api/health``) instead of one ad-hoc flag per feature.

"Configured" means *usable*, not *switched on*: embeddings need either the
local extra installed or a remote endpoint; enrichment needs at least one
enricher whose asset is present; case transfer needs a nonzero concurrency
budget. Each predicate is cheap — an import check, a settings read, or a
cached availability record — because health is polled every 15 seconds.
"""

from __future__ import annotations

from typing import Any

from vestigo.core.config import get_settings

#: Capability keys, in the order the admin console lists them.
CAPABILITY_KEYS: tuple[str, ...] = (
    "embeddings",
    "agent",
    "mcp",
    "oidc",
    "enrichers",
    "sigma",
    "transfer",
)


def _enrichers_available() -> bool:
    from vestigo.enrichers.registry import all_enrichers, get_cached_availability

    for enricher in all_enrichers():
        availability = get_cached_availability(enricher.key)
        if availability is not None and availability.available:
            return True
    return False


def _oidc_available(settings: Any) -> bool:
    """OIDC needs the switch *and* a complete client registration."""
    return bool(
        settings.oidc_enabled
        and settings.oidc_issuer
        and settings.oidc_client_id
        and settings.oidc_client_secret
    )


async def get_capabilities() -> dict[str, bool]:
    """Resolve every optional subsystem's availability for this instance."""
    from vestigo.agent.availability import agent_available
    from vestigo.models.embeddings import embeddings_available

    settings = get_settings()
    return {
        "embeddings": embeddings_available(),
        "agent": await agent_available(),
        "mcp": settings.mcp_enabled,
        "oidc": _oidc_available(settings),
        "enrichers": _enrichers_available(),
        # Sigma needs no configuration: rules can be uploaded per case, and the
        # pysigma backend is a hard dependency. The capability exists so the
        # frontend gates every subsystem the same way, not because it varies.
        "sigma": True,
        "transfer": settings.transfer_enabled,
    }
