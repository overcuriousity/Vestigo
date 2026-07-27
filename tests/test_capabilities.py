"""Subsystem capability gating: /api/health, and the enforcement behind it.

An unconfigured subsystem must be *invisible* — no capability, no UI entry
point — and still refuse its own endpoints, so hiding the button is never the
only thing standing between a user and a half-configured feature.
"""

from __future__ import annotations

from tests.conftest import as_admin
from vestigo.core.capabilities import CAPABILITY_KEYS
from vestigo.core.config import get_settings


def test_health_reports_every_capability(client):
    caps = client.get("/api/health").json()["capabilities"]
    assert set(caps) == set(CAPABILITY_KEYS)
    assert all(isinstance(v, bool) for v in caps.values())


def test_legacy_flat_flags_mirror_capabilities(client):
    """Older clients read the flat keys; they must not disagree."""
    body = client.get("/api/health").json()
    caps = body["capabilities"]
    assert body["embeddings_available"] == caps["embeddings"]
    assert body["agent_available"] == caps["agent"]
    assert body["mcp_enabled"] == caps["mcp"]


def test_oidc_capability_requires_a_complete_registration(client, monkeypatch):
    monkeypatch.setenv("VESTIGO_OIDC_ENABLED", "true")
    get_settings.cache_clear()
    # Enabled but with no issuer/client — not usable, so not advertised.
    assert client.get("/api/health").json()["capabilities"]["oidc"] is False


def test_disabling_transfer_hides_and_refuses_it(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id = client.post("/api/cases/", json={"name": "xfer"}).json()["case"]["id"]
    assert client.get("/api/health").json()["capabilities"]["transfer"] is True

    resp = client.put("/api/admin/settings", json={"values": {"transfer_enabled": False}})
    assert resp.status_code == 200, resp.text

    assert client.get("/api/health").json()["capabilities"]["transfer"] is False
    export = client.post(f"/api/cases/{case_id}/export")
    assert export.status_code == 503
    assert "disabled" in export.json()["detail"].lower()
