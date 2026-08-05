"""Tests for the generic admin enricher-asset endpoints (GeoIP as reference).

A real GeoLite2 .mmdb fixture isn't vendored in this repo (MaxMind's
distributable test databases aren't available offline here), so tests that
need a readable database monkeypatch ``geoip2.database.Reader`` with a fake.
Covered: RBAC, unknown-key/asset-less rejection, invalid-file rejection,
wrong-flavor (non-City) rejection, the City happy path including the metadata
sidecar, and the asset block folded into ``GET /admin/enrichers/config``.
"""

from __future__ import annotations

import io

from tests.conftest import as_admin, login


def test_non_admin_cannot_upload_enricher_asset(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    client.post("/api/admin/users", json={"username": "plain", "password": "abcdefgh12"})

    plain_client = client.__class__(client.app)
    login(plain_client, "plain", "abcdefgh12")
    resp = plain_client.post(
        "/api/admin/enrichers/geoip/asset",
        files={
            "file": (
                "GeoLite2-City.mmdb",
                io.BytesIO(b"not a real database"),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 403


def test_upload_asset_unknown_enricher_404(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/enrichers/nope/asset",
        files={"file": ("x.bin", io.BytesIO(b"x"), "application/octet-stream")},
    )
    assert resp.status_code == 404


def test_upload_asset_assetless_enricher_400(client, admin_bootstrap, store, monkeypatch):
    from vestigo.enrichers import registry
    from vestigo.enrichers.base import AvailabilityResult, Enricher

    class Stub(Enricher):
        key = "stub-no-asset"
        display_name = "Stub"
        description = ""
        eligibility_regex = ".*"
        output_fields = ("x",)

        def check_availability(self):
            return AvailabilityResult(True)

        def enrich_value(self, raw_value):
            return None

    monkeypatch.setitem(registry._REGISTRY, "stub-no-asset", Stub())

    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/enrichers/stub-no-asset/asset",
        files={"file": ("x.bin", io.BytesIO(b"x"), "application/octet-stream")},
    )
    assert resp.status_code == 400
    assert "no uploaded asset" in resp.json()["detail"]


def test_invalid_geoip_database_rejected(client, admin_bootstrap, store, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "vestigo.enrichers.geoip.geoip_database_path", lambda: tmp_path / "GeoLite2-City.mmdb"
    )
    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/enrichers/geoip/asset",
        files={
            "file": (
                "GeoLite2-City.mmdb",
                io.BytesIO(b"not a real database"),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 400
    assert "Invalid GeoLite2 database" in resp.json()["detail"]


class _FakeReader:
    """Stands in for geoip2.database.Reader — no real .mmdb is vendored."""

    database_type = "GeoLite2-City"

    def __init__(self, path):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def metadata(self):
        meta = type("Meta", (), {})()
        meta.database_type = self.database_type
        meta.build_epoch = 1700000000
        return meta


def test_country_flavored_database_rejected_with_actionable_message(
    client, admin_bootstrap, store, tmp_path, monkeypatch
):
    import geoip2.database

    class _CountryReader(_FakeReader):
        database_type = "GeoLite2-Country"

    monkeypatch.setattr(geoip2.database, "Reader", _CountryReader)
    monkeypatch.setattr(
        "vestigo.enrichers.geoip.geoip_database_path", lambda: tmp_path / "GeoLite2-City.mmdb"
    )

    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/enrichers/geoip/asset",
        files={
            "file": (
                "GeoLite2-Country.mmdb",
                io.BytesIO(b"country db bytes"),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 400
    assert "GeoLite2-Country" in resp.json()["detail"]
    assert "City database" in resp.json()["detail"]
    # Nothing installed.
    assert not (tmp_path / "GeoLite2-City.mmdb").exists()


def test_city_database_upload_installs_and_writes_sidecar(
    client, admin_bootstrap, store, tmp_path, monkeypatch
):
    import hashlib

    import geoip2.database

    from vestigo.enrichers import registry
    from vestigo.enrichers.geoip import GeoIPEnricher, read_geoip_sidecar

    target = tmp_path / "GeoLite2-City.mmdb"
    monkeypatch.setattr(geoip2.database, "Reader", _FakeReader)
    monkeypatch.setattr("vestigo.enrichers.geoip.geoip_database_path", lambda: target)
    # Other tests may have registered a GeoIP instance pinned to their own
    # tmp path; pin a fresh default-path instance for this test only.
    monkeypatch.setitem(registry._REGISTRY, "geoip", GeoIPEnricher())

    payload = b"city db bytes"
    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/enrichers/geoip/asset",
        files={"file": ("GeoLite2-City.mmdb", io.BytesIO(payload), "application/octet-stream")},
    )
    assert resp.status_code == 200
    body = resp.json()
    expected_sha = hashlib.sha256(payload).hexdigest()
    assert body["detail"]["sha256"] == expected_sha
    assert body["detail"]["build_epoch"] == 1700000000
    assert body["detail"]["database_type"] == "GeoLite2-City"

    assert target.read_bytes() == payload
    sidecar = read_geoip_sidecar(target)
    assert sidecar["sha256"] == expected_sha
    assert sidecar["database_type"] == "GeoLite2-City"


def test_config_list_reports_asset_state(client, admin_bootstrap, store, tmp_path, monkeypatch):
    from vestigo.enrichers import registry
    from vestigo.enrichers.geoip import GeoIPEnricher

    missing_path = tmp_path / "missing.mmdb"
    monkeypatch.setattr("vestigo.enrichers.geoip.geoip_database_path", lambda: missing_path)
    monkeypatch.setitem(registry._REGISTRY, "geoip", GeoIPEnricher())
    registry.refresh_availability("geoip")

    as_admin(client, admin_bootstrap)
    resp = client.get("/api/admin/enrichers/config")
    assert resp.status_code == 200
    geoip_entry = next(e for e in resp.json()["enrichers"] if e["key"] == "geoip")
    assert geoip_entry["available"] is False
    asset = geoip_entry["asset"]
    assert asset is not None
    assert asset["uploaded"] is False
    assert asset["size_bytes"] is None
    assert asset["accepted_extensions"] == [".mmdb"]
    assert asset["name"]


def test_list_enrichers_reports_geoip_unavailable(client, admin_bootstrap, store, tmp_path):
    from vestigo.enrichers import registry
    from vestigo.enrichers.geoip import GeoIPEnricher

    registry.register(GeoIPEnricher(db_path=tmp_path / "missing.mmdb"))
    registry.refresh_availability()

    as_admin(client, admin_bootstrap)
    resp = client.get("/api/enrichers")
    assert resp.status_code == 200
    enrichers = resp.json()["enrichers"]
    geoip_entry = next((e for e in enrichers if e["key"] == "geoip"), None)
    assert geoip_entry is not None
    assert geoip_entry["available"] is False


class _FakeASNReader(_FakeReader):
    database_type = "GeoLite2-ASN"


def test_city_database_rejected_for_asn_enricher(
    client, admin_bootstrap, store, tmp_path, monkeypatch
):
    import geoip2.database

    monkeypatch.setattr(geoip2.database, "Reader", _FakeReader)  # City-flavored
    monkeypatch.setattr(
        "vestigo.enrichers.asn.asn_database_path", lambda: tmp_path / "GeoLite2-ASN.mmdb"
    )

    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/enrichers/asn/asset",
        files={
            "file": (
                "GeoLite2-City.mmdb",
                io.BytesIO(b"city db bytes"),
                "application/octet-stream",
            )
        },
    )
    assert resp.status_code == 400
    assert "GeoLite2-City" in resp.json()["detail"]
    assert "ASN database" in resp.json()["detail"]
    # Nothing installed.
    assert not (tmp_path / "GeoLite2-ASN.mmdb").exists()


def test_asn_database_upload_installs_and_writes_sidecar(
    client, admin_bootstrap, store, tmp_path, monkeypatch
):
    import hashlib

    import geoip2.database

    from vestigo.enrichers import registry
    from vestigo.enrichers.asn import ASNEnricher, read_asn_sidecar

    target = tmp_path / "GeoLite2-ASN.mmdb"
    monkeypatch.setattr(geoip2.database, "Reader", _FakeASNReader)
    monkeypatch.setattr("vestigo.enrichers.asn.asn_database_path", lambda: target)
    # Other tests may have registered an ASN instance pinned to their own
    # tmp path; pin a fresh default-path instance for this test only.
    monkeypatch.setitem(registry._REGISTRY, "asn", ASNEnricher())

    payload = b"asn db bytes"
    as_admin(client, admin_bootstrap)
    resp = client.post(
        "/api/admin/enrichers/asn/asset",
        files={"file": ("GeoLite2-ASN.mmdb", io.BytesIO(payload), "application/octet-stream")},
    )
    assert resp.status_code == 200
    body = resp.json()
    expected_sha = hashlib.sha256(payload).hexdigest()
    assert body["detail"]["sha256"] == expected_sha
    assert body["detail"]["build_epoch"] == 1700000000
    assert body["detail"]["database_type"] == "GeoLite2-ASN"

    assert target.read_bytes() == payload
    sidecar = read_asn_sidecar(target)
    assert sidecar["sha256"] == expected_sha
    assert sidecar["database_type"] == "GeoLite2-ASN"
