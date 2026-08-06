"""In-memory registry of built-in enrichers.

Not a pip entry_points/plugin-discovery system — Vestigo is a local-first
monorepo, so registration is a plain module-level list. Availability is
checked once at startup and re-checked on demand (e.g. after an admin
uploads a required database file) via ``refresh_availability()``.
"""

from __future__ import annotations

from vestigo.enrichers.asn import ASNEnricher
from vestigo.enrichers.base import AvailabilityResult, Enricher
from vestigo.enrichers.geoip import GeoIPEnricher

_REGISTRY: dict[str, Enricher] = {}
_AVAILABILITY_CACHE: dict[str, AvailabilityResult] = {}


def register(enricher: Enricher) -> None:
    """Register an enricher instance by its key."""
    _REGISTRY[enricher.key] = enricher


def all_enrichers() -> list[Enricher]:
    """Return every registered enricher."""
    return list(_REGISTRY.values())


def get_enricher(key: str) -> Enricher | None:
    """Return a registered enricher by key, or None if unknown."""
    return _REGISTRY.get(key)


def refresh_availability(key: str | None = None) -> dict[str, AvailabilityResult]:
    """Recompute and cache ``check_availability()`` for one enricher, or all.

    With ``key`` given, only that enricher is re-checked (an unknown key is a
    no-op returning ``{}``); with ``key=None`` every registered enricher is
    swept. Called at app startup and by any endpoint that changes an
    enricher's runtime requirements (e.g. an asset upload), so
    ``get_cached_availability`` always reflects current state without every
    caller re-running a filesystem/DB check.
    """
    if key is not None:
        enricher = _REGISTRY.get(key)
        if enricher is None:
            return {}
        _AVAILABILITY_CACHE[key] = enricher.check_availability()
        return {key: _AVAILABILITY_CACHE[key]}
    for reg_key, enricher in _REGISTRY.items():
        _AVAILABILITY_CACHE[reg_key] = enricher.check_availability()
    return dict(_AVAILABILITY_CACHE)


def get_cached_availability(key: str) -> AvailabilityResult | None:
    """Return the last computed availability for an enricher, or None if never checked."""
    return _AVAILABILITY_CACHE.get(key)


def _register_builtins() -> None:
    register(GeoIPEnricher())
    register(ASNEnricher())


_register_builtins()
