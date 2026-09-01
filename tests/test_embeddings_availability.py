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
    # CI syncs --all-extras, so without this the local stack really is
    # importable and the refusal would instead be about missing weights.
    monkeypatch.setattr(availability, "_local_stack_importable", lambda: False)
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


def test_the_local_store_probe_does_not_take_qdrants_lock(monkeypatch, tmp_path):
    """The probe must survive the app already holding the on-disk lock.

    qdrant-client's local mode locks the storage folder exclusively for the
    lifetime of a client, and this process holds one whenever the similarity
    service (a process-lifetime singleton) or an embedding job is alive. A
    probe that constructed its own client would start failing after the first
    semantic search and flip the capability off on a healthy instance — and,
    in the window it held the lock itself, would break a starting embed job.
    """
    pytest.importorskip("qdrant_client")
    from qdrant_client import QdrantClient

    store = tmp_path / "qdrant"
    monkeypatch.setenv("VESTIGO_QDRANT_URL", "")
    monkeypatch.setenv("VESTIGO_QDRANT_PATH", str(store))
    get_settings.cache_clear()
    holder = QdrantClient(path=str(store))
    try:
        assert availability._probe_vector_store() is True
        # And the app's own client can still be built while the probe runs.
        assert availability._probe_local_store(str(store)) is True
    finally:
        holder.close()
        get_settings.cache_clear()


def test_the_local_store_probe_reports_a_missing_directory(monkeypatch, tmp_path):
    missing = tmp_path / "gone" / "qdrant"
    assert availability._probe_local_store(str(missing)) is False
    # A creatable path (parent exists and is writable) is usable.
    assert availability._probe_local_store(str(tmp_path / "qdrant")) is True


async def test_a_probe_that_raises_cannot_take_down_health(monkeypatch):
    """One optional subsystem may not cost /api/health its whole capability map."""

    def _boom() -> bool:
        raise RuntimeError("qdrant-client exploded on import")

    monkeypatch.setattr(availability, "model_configured", lambda: True)
    monkeypatch.setattr(availability, "_probe_vector_store", _boom)
    assert await availability.embeddings_operational(force=True) is False


async def test_an_invalid_endpoint_url_reads_as_unavailable(monkeypatch):
    """httpx.InvalidURL is not an httpx.HTTPError — catching only that 500s health."""
    monkeypatch.setenv("VESTIGO_EMBEDDING_API_BASE_URL", "http://embedder.local/v1")
    get_settings.cache_clear()
    try:

        async def _boom(*a, **kw):
            raise httpx.InvalidURL("not a url")

        monkeypatch.setattr(httpx.AsyncClient, "post", _boom)
        assert await availability._probe_embedding_endpoint() is False
    finally:
        get_settings.cache_clear()


def test_a_refresh_task_from_a_dead_loop_does_not_block_later_refreshes(monkeypatch):
    """Otherwise stale-while-revalidate silently stops for the whole process.

    A task created on a loop that is torn down before it ran stays
    ``done() == False`` forever, so a bare in-flight guard never schedules
    another refresh again — most visible under TestClient, which drives the
    app from its own portal loop.
    """
    import asyncio

    dead_loop = asyncio.new_event_loop()
    dead_loop.close()

    class _Stranded:
        """A task belonging to a loop that will never run it."""

        def done(self) -> bool:
            return False

        def get_loop(self):
            return dead_loop

    availability._refresh_task = _Stranded()

    async def _noop(_fingerprint: str) -> None:
        return None

    async def _drive() -> None:
        monkeypatch.setattr(availability, "_refresh_cache", _noop)
        availability._schedule_refresh("fp")
        assert not isinstance(availability._refresh_task, _Stranded)
        await availability._refresh_task

    try:
        asyncio.run(_drive())
    finally:
        availability.reset_probe_cache()


def test_the_refusal_does_not_reuse_a_result_from_other_settings(monkeypatch):
    """A record from the settings in force before an admin's PUT names the wrong host."""
    monkeypatch.setattr(availability, "model_configured", lambda: True)
    availability._cache = (False, availability.time.monotonic(), "not-this-fingerprint")
    try:
        detail = availability.unavailable_detail()
    finally:
        availability.reset_probe_cache()
    assert "did not answer" not in detail


def test_the_refusal_claims_no_probe_on_a_cold_cache(monkeypatch):
    monkeypatch.setattr(availability, "model_configured", lambda: True)
    availability.reset_probe_cache()
    detail = availability.unavailable_detail()
    assert "did not answer" not in detail
    assert "not available right now" in detail


# ---------------------------------------------------------------------------
# The operator switch and the local-weights arm
# ---------------------------------------------------------------------------
#
# Both pin the same reported bug from opposite ends: an airgapped Docker
# install shipped the `embeddings` extra, ran Qdrant, and therefore advertised
# the whole embedding UI — "Improve search quality" on every timeline — for a
# model whose weights the box had never been online to fetch.


def _settings(monkeypatch, **env: str):
    for key, value in env.items():
        monkeypatch.setenv(f"VESTIGO_{key.upper()}", value)
    get_settings.cache_clear()
    availability.reset_probe_cache()


async def test_the_switch_off_hides_embeddings_however_healthy_both_arms_are(monkeypatch):
    _arms(monkeypatch, store=True, endpoint=True)
    _settings(monkeypatch, embeddings_enabled="false")
    try:
        assert await availability.embeddings_operational() is False
        assert embeddings_available() is False
        assert "switched off" in availability.unavailable_detail()
    finally:
        get_settings.cache_clear()
        availability.reset_probe_cache()


async def test_the_switch_on_leaves_the_probe_in_charge(monkeypatch):
    _arms(monkeypatch, store=False)
    _settings(monkeypatch, embeddings_enabled="true")
    try:
        assert await availability.embeddings_operational() is False
        _arms(monkeypatch, store=True)
        availability.reset_probe_cache()
        assert await availability.embeddings_operational() is True
    finally:
        get_settings.cache_clear()
        availability.reset_probe_cache()


def test_the_switch_off_never_probes(monkeypatch):
    """Not just the answer: an off switch must not open a socket to say so."""

    def _boom() -> bool:
        raise AssertionError("probed a switched-off subsystem")

    monkeypatch.setattr(availability, "_probe_vector_store", _boom)
    _settings(monkeypatch, embeddings_enabled="false")
    try:
        assert embeddings_available() is False
    finally:
        get_settings.cache_clear()
        availability.reset_probe_cache()


def test_local_weights_missing_offline_is_not_a_configured_model(monkeypatch):
    """The airgap case: extra installed, weights never fetched, online refused."""
    _settings(monkeypatch, embeddings_enabled="true", allow_online="false")
    # Asserted rather than left to the venv: whether this checkout installed the
    # `embeddings` extra says nothing about the weights logic under test.
    monkeypatch.setattr(availability, "_local_stack_importable", lambda: True)
    monkeypatch.setattr(availability, "_local_weights_present", lambda: False)
    try:
        assert availability.model_configured() is False
        assert "weights" in availability.unavailable_detail()
    finally:
        get_settings.cache_clear()


def test_local_weights_missing_online_is_still_configured(monkeypatch):
    """An online host downloads them on first use — hiding the feature would be wrong."""
    _settings(monkeypatch, embeddings_enabled="true", allow_online="true")
    monkeypatch.setattr(availability, "_local_stack_importable", lambda: True)
    monkeypatch.setattr(availability, "_local_weights_present", lambda: False)
    try:
        assert availability.model_configured() is True
    finally:
        get_settings.cache_clear()


def test_a_remote_endpoint_needs_no_local_weights(monkeypatch):
    _settings(
        monkeypatch,
        embeddings_enabled="true",
        allow_online="false",
        embedding_api_base_url="http://embed.local/v1",
    )
    monkeypatch.setattr(availability, "_local_weights_present", lambda: False)
    try:
        assert availability.model_configured() is True
    finally:
        get_settings.cache_clear()


def test_a_model_named_as_a_local_directory_counts_as_present(monkeypatch, tmp_path):
    model_dir = tmp_path / "all-MiniLM-L6-v2"
    model_dir.mkdir()
    _settings(
        monkeypatch,
        embeddings_enabled="true",
        allow_online="false",
        embedding_model=str(model_dir),
    )
    try:
        assert availability._local_weights_present() is True
    finally:
        get_settings.cache_clear()
