"""Tests for the admin GeoIP database upload endpoint.

A real GeoLite2 .mmdb fixture isn't vendored in this repo (MaxMind's
distributable test databases aren't available offline here), so the
happy-path "valid upload flips availability" case isn't covered end-to-end —
see docs/ROADMAP or the enricher plan for that follow-up. These tests cover
the guardrails: RBAC and invalid-file rejection.
"""

from __future__ import annotations

import io

from tests.conftest import as_admin, login


def test_non_admin_cannot_upload_geoip_database(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    client.post("/api/admin/users", json={"username": "plain", "password": "abcdefgh12"})

    plain_client = client.__class__(client.app)
    login(plain_client, "plain", "abcdefgh12")
    resp = plain_client.post(
        "/api/admin/enrichers/geoip/database",
        files={
            "file": (
                "GeoLite2-City.mmdb",
                io.BytesIO(b"not a real database"),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 403


def test_invalid_geoip_database_rejected(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/enrichers/geoip/database",
        files={
            "file": (
                "GeoLite2-City.mmdb",
                io.BytesIO(b"not a real database"),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 400


def test_geoip_status_reports_unavailable_before_upload(
    client, admin_bootstrap, store, tmp_path, monkeypatch
):
    from tracesignal.enrichers import registry
    from tracesignal.enrichers.geoip import GeoIPEnricher

    missing_path = tmp_path / "missing.mmdb"
    # The status endpoint resolves the path via geoip_database_path()
    # directly (not through the registry), so both must point at the same
    # (non-existent, test-isolated) location.
    monkeypatch.setattr("tracesignal.enrichers.geoip.geoip_database_path", lambda: missing_path)
    registry.register(GeoIPEnricher(db_path=missing_path))
    registry.refresh_availability()

    as_admin(client, admin_bootstrap)
    resp = client.get("/api/admin/enrichers/geoip/database")
    assert resp.status_code == 200
    body = resp.json()
    assert body["uploaded"] is False
    assert body["available"] is False


def test_list_enrichers_reports_geoip_unavailable(client, admin_bootstrap, store, tmp_path):
    from tracesignal.enrichers import registry
    from tracesignal.enrichers.geoip import GeoIPEnricher

    registry.register(GeoIPEnricher(db_path=tmp_path / "missing.mmdb"))
    registry.refresh_availability()

    as_admin(client, admin_bootstrap)
    resp = client.get("/api/enrichers")
    assert resp.status_code == 200
    enrichers = resp.json()["enrichers"]
    geoip_entry = next((e for e in enrichers if e["key"] == "geoip"), None)
    assert geoip_entry is not None
    assert geoip_entry["available"] is False
