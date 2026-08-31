"""Graceful degradation when the 'embeddings' extra is not installed.

The dev environment always has sentence-transformers (CI syncs --all-extras),
so absence is simulated by patching ``embeddings_available`` at each
consumer's import site.
"""

from __future__ import annotations

from tests.conftest import as_admin
from vestigo.api.routers import cases as cases_router
from vestigo.api.routers import events as events_router
from vestigo.models import availability as availability_module
from vestigo.models.embeddings import embeddings_available


def test_embeddings_available_true_in_dev_env():
    # The test environment installs the extra, and no remote endpoint is set.
    assert embeddings_available() is True


def test_health_reports_embeddings_available(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    assert client.get("/api/health").json()["embeddings_available"] is True


def test_health_reports_embeddings_unavailable(client, admin_bootstrap, monkeypatch):
    # /api/health resolves every optional subsystem through core.capabilities,
    # which now asks `models.availability` for a probed answer rather than the
    # configuration check — so the patch lands on the probe.
    as_admin(client, admin_bootstrap)

    async def _unavailable() -> bool:
        return False

    monkeypatch.setattr(availability_module, "_probe", _unavailable)
    availability_module.reset_probe_cache()
    body = client.get("/api/health").json()
    assert body["embeddings_available"] is False
    assert body["capabilities"]["embeddings"] is False


def test_embed_start_returns_503_without_embeddings(client, admin_bootstrap, monkeypatch):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "capcase"}).json()["case"]["id"]

    monkeypatch.setattr(cases_router, "embeddings_available", lambda: False)
    # Keep the *reason* deterministic too: the 503 text now comes from
    # `unavailable_detail()`, which reads the real configuration, and CI syncs
    # --all-extras — so without this the message would be about the vector
    # store on CI and about the missing extra locally.
    monkeypatch.setattr(availability_module, "model_configured", lambda: False)
    # The capability pre-check runs before the timeline lookup, deliberately.
    resp = client.post(f"/api/cases/{case_id}/timelines/whatever/embed")
    assert resp.status_code == 503
    assert "embeddings" in resp.json()["detail"].lower()


def test_semantic_search_returns_503_without_embeddings(client, admin_bootstrap, monkeypatch):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "capcase2"}).json()["case"]["id"]

    monkeypatch.setattr(events_router, "embeddings_available", lambda: False)
    monkeypatch.setattr(availability_module, "model_configured", lambda: False)
    resp = client.get(f"/api/cases/{case_id}/events/semantic-search", params={"q": "login"})
    assert resp.status_code == 503
    assert "embeddings" in resp.json()["detail"].lower()


def test_remote_endpoint_counts_as_available(monkeypatch):
    """Remote embedding mode needs no local torch stack."""
    import importlib.util

    from vestigo.core.config import get_settings
    from vestigo.models import embeddings as embeddings_module

    monkeypatch.setenv("VESTIGO_EMBEDDING_API_BASE_URL", "http://embedder.local/v1")
    get_settings.cache_clear()
    try:
        real_find_spec = importlib.util.find_spec
        monkeypatch.setattr(
            importlib.util,
            "find_spec",
            lambda name: None if name == "sentence_transformers" else real_find_spec(name),
        )
        assert embeddings_module.embeddings_available() is True
    finally:
        get_settings.cache_clear()
