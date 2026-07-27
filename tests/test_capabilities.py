"""Subsystem capability gating: /api/health, and the enforcement behind it.

An unconfigured subsystem must be *invisible* — no capability, no UI entry
point — and still refuse its own endpoints, so hiding the button is never the
only thing standing between a user and a half-configured feature.
"""

from __future__ import annotations

import pytest

from tests.conftest import as_admin
from vestigo.core.capabilities import CAPABILITY_KEYS, get_capabilities
from vestigo.core.config import get_settings


def test_health_reports_every_capability(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    caps = client.get("/api/health").json()["capabilities"]
    assert set(caps) == set(CAPABILITY_KEYS)
    assert all(isinstance(v, bool) for v in caps.values())


def test_capabilities_require_a_session(client, admin_bootstrap):
    """Which subsystems an instance runs is an inventory of its attack surface.

    The route stays exempt from the auth gate because the login page needs it,
    so the body is split rather than the route closed: liveness and the
    available login methods are public, everything else is not.
    """
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert "version" in body
    assert body["oidc_enabled"] is False
    assert "capabilities" not in body
    assert not {"embeddings_available", "agent_available", "mcp_enabled"} & set(body)

    as_admin(client, admin_bootstrap)
    assert "capabilities" in client.get("/api/health").json()


def test_legacy_flat_flags_mirror_capabilities(client, admin_bootstrap):
    """Older clients read the flat keys; they must not disagree."""
    as_admin(client, admin_bootstrap)
    body = client.get("/api/health").json()
    caps = body["capabilities"]
    assert body["embeddings_available"] == caps["embeddings"]
    assert body["agent_available"] == caps["agent"]
    assert body["mcp_enabled"] == caps["mcp"]


def test_oidc_capability_requires_a_complete_registration(client, admin_bootstrap, monkeypatch):
    as_admin(client, admin_bootstrap)
    monkeypatch.setenv("VESTIGO_OIDC_ENABLED", "true")
    get_settings.cache_clear()
    # Enabled but with no issuer/client — not usable, so not advertised.
    assert client.get("/api/health").json()["capabilities"]["oidc"] is False


@pytest.mark.asyncio
async def test_enrichers_capability_survives_a_cold_availability_cache(monkeypatch):
    """A cache nobody filled means "not checked yet", not "nothing available".

    The startup sweep can be skipped entirely (it shares a ``try`` with steps
    that are expected to fail against an unreachable ClickHouse), and reporting
    ``enrichers: false`` there removed the whole Enrichment UI from an
    installation whose asset was in place.
    """
    from vestigo.enrichers import registry
    from vestigo.enrichers.base import AvailabilityResult, Enricher

    class Stub(Enricher):
        key = "stub"
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return None

    monkeypatch.setattr(registry, "_REGISTRY", {"stub": Stub()})
    monkeypatch.setattr(registry, "_AVAILABILITY_CACHE", {})

    assert (await get_capabilities())["enrichers"] is True
    # Filled as a side effect, so later polls stay a dict read.
    assert registry.get_cached_availability("stub").available is True


@pytest.mark.asyncio
async def test_enrichers_capability_false_when_no_asset_is_installed(monkeypatch):
    from vestigo.enrichers import registry
    from vestigo.enrichers.base import AvailabilityResult, Enricher

    class Stub(Enricher):
        key = "stub"
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)

        def check_availability(self):
            return AvailabilityResult(False, "no asset")

        def enrich_value(self, raw_value):
            return None

    monkeypatch.setattr(registry, "_REGISTRY", {"stub": Stub()})
    monkeypatch.setattr(registry, "_AVAILABILITY_CACHE", {})

    assert (await get_capabilities())["enrichers"] is False


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
