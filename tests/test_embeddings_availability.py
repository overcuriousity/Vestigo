"""The two-arm embeddings availability probe (``models/availability.py``).

The bug these pin: ``embeddings_available()`` used to answer "can this instance
compute a vector" and never asked whether anything could store one, so an
operator who removed Qdrant from their stack kept the whole embedding UI —
"Improve search quality" on every timeline, a wizard that opens, and a job that
fails at the vector store. Availability is now both arms, each probed.
"""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import as_admin
from vestigo.core.config import get_settings
from vestigo.models import availability
from vestigo.models.embeddings import embeddings_available

#: Captured at import, before conftest's autouse stub can replace it.
_REAL_PROBE = availability._probe


@pytest.fixture(autouse=True)
def real_probe(monkeypatch):
    """Undo conftest's stub — these tests are about the probe itself.

    Both arms are stubbed per test instead, so nothing here opens a socket.
    """
    monkeypatch.setattr(availability, "_probe", _REAL_PROBE)
    availability.reset_probe_cache()


def _arms(monkeypatch, *, store: bool = True, endpoint: bool = True) -> None:
    """Stub both arms. The model side is asserted *configured* rather than
    left to the environment: whether this venv has the `embeddings` extra says
    nothing about the vector-store logic under test, and CI syncs it while a
    developer's venv may not."""
    monkeypatch.setattr(availability, "model_configured", lambda: True)
    monkeypatch.setattr(availability, "_probe_vector_store", lambda: store)

    async def _endpoint() -> bool:
        return endpoint

    monkeypatch.setattr(availability, "_probe_embedding_endpoint", _endpoint)


async def test_unreachable_vector_store_makes_embeddings_unavailable(monkeypatch):
    """The reported bug: Qdrant removed, everything else untouched."""
    _arms(monkeypatch, store=False)
    assert await availability.embeddings_operational(force=True) is False


async def test_both_arms_answering_makes_embeddings_available(monkeypatch):
    _arms(monkeypatch)
    assert await availability.embeddings_operational(force=True) is True


async def test_a_dead_remote_endpoint_makes_embeddings_unavailable(monkeypatch):
    """A configured endpoint that does not serve is not a configured endpoint."""
    monkeypatch.setenv("VESTIGO_EMBEDDING_API_BASE_URL", "http://embedder.local/v1")
    get_settings.cache_clear()
    try:
        _arms(monkeypatch, endpoint=False)
        assert await availability.embeddings_operational(force=True) is False
    finally:
        get_settings.cache_clear()


async def test_no_vector_store_configured_needs_no_probe(monkeypatch):
    """Clearing both Qdrant settings is the declarative way to say "no embeddings"."""

    def _explode() -> bool:
        raise AssertionError("must not probe a store the operator did not declare")

    monkeypatch.setattr(availability, "_probe_vector_store", _explode)
    monkeypatch.setenv("VESTIGO_QDRANT_URL", "")
    monkeypatch.setenv("VESTIGO_QDRANT_PATH", "")
    get_settings.cache_clear()
    try:
        assert availability.vector_store_configured() is False
        assert await availability.embeddings_operational(force=True) is False
    finally:
        get_settings.cache_clear()


async def test_a_settings_change_re_probes_without_waiting_out_the_ttl(monkeypatch):
    """What makes an admin's settings PUT visible on the next poll."""
    _arms(monkeypatch, store=False)
    assert await availability.embeddings_operational() is False
    _arms(monkeypatch, store=True)
    # Same settings: the cached "no" stands, TTL not expired.
    assert await availability.embeddings_operational() is False
    monkeypatch.setenv("VESTIGO_QDRANT_URL", "http://qdrant.elsewhere:6333")
    get_settings.cache_clear()
    try:
        assert await availability.embeddings_operational() is True
    finally:
        get_settings.cache_clear()


def test_the_sync_predicate_does_not_hide_a_subsystem_on_a_cold_cache(monkeypatch):
    """A cold cache means nobody looked yet — not that the answer is no.

    The sync predicate runs in request paths and in the agent's tool
    registration, where it cannot probe; treating "not yet known" as
    unavailable would make embedding features vanish for the first seconds of
    every process.
    """
    monkeypatch.setattr(availability, "model_configured", lambda: True)
    availability.reset_probe_cache()
    assert availability.cached_result() is None
    assert embeddings_available() is True


def test_the_sync_predicate_follows_the_probe_once_it_has_answered(monkeypatch):
    monkeypatch.setattr(availability, "model_configured", lambda: True)
    availability._cache = (False, availability.time.monotonic(), availability._fingerprint())
    try:
        assert embeddings_available() is False
    finally:
        availability.reset_probe_cache()


def test_a_probe_answer_for_other_settings_is_not_reused(monkeypatch):
    """The cache is keyed on the settings the answer depends on."""
    availability._cache = (False, availability.time.monotonic(), "not-this-fingerprint")
    assert availability.cached_result() is None


def test_vector_store_probe_reports_failure_rather_than_raising(monkeypatch):
    """Every failure shape collapses to "unavailable" — the probe never raises."""
    import qdrant_client

    class _Boom:
        def __init__(self, *a, **kw):
            raise OSError("connection refused")

    monkeypatch.setattr(qdrant_client, "QdrantClient", _Boom)
    assert availability._probe_vector_store() is False


async def test_endpoint_probe_reports_failure_rather_than_raising(monkeypatch):
    monkeypatch.setenv("VESTIGO_EMBEDDING_API_BASE_URL", "http://embedder.local/v1")
    get_settings.cache_clear()
    try:

        async def _boom(*a, **kw):
            raise httpx.ConnectError("nope")

        monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
        assert await availability._probe_embedding_endpoint() is False
    finally:
        get_settings.cache_clear()


async def test_health_hides_embeddings_when_the_vector_store_is_gone(
    client, admin_bootstrap, monkeypatch
):
    """End to end: the capability the frontend gates every entry point on."""
    as_admin(client, admin_bootstrap)
    _arms(monkeypatch, store=False)
    availability.reset_probe_cache()
    body = client.get("/api/health").json()
    assert body["capabilities"]["embeddings"] is False
    assert body["embeddings_available"] is False


def test_the_refusal_names_the_arm_that_is_missing(monkeypatch):
    """An operator whose Qdrant is gone must not be sent to install a 2 GB extra."""
    monkeypatch.setattr(availability, "model_configured", lambda: True)
    monkeypatch.setenv("VESTIGO_QDRANT_URL", "")
    monkeypatch.setenv("VESTIGO_QDRANT_PATH", "")
    get_settings.cache_clear()
    try:
        detail = availability.unavailable_detail()
    finally:
        get_settings.cache_clear()
    assert "vector store" in detail
    assert "embeddings' extra" not in detail


def test_the_refusal_still_names_the_extra_when_that_is_what_is_missing(monkeypatch):
    monkeypatch.setattr(availability, "model_configured", lambda: False)
    assert "embeddings' extra" in availability.unavailable_detail()


def test_a_configured_but_unreachable_store_says_so(monkeypatch):
    """Configured and dead is a different sentence from not configured."""
    monkeypatch.setattr(availability, "model_configured", lambda: True)
    availability._cache = (False, availability.time.monotonic(), availability._fingerprint())
    try:
        detail = availability.unavailable_detail()
    finally:
        availability.reset_probe_cache()
    assert "did not answer" in detail
