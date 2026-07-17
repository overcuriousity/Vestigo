"""Sigma router: rule CRUD, RBAC, run launch validation, run records."""

from __future__ import annotations

from tests.conftest import as_admin, login
from vestigo.core.config import get_settings

VALID_RULE = """
title: API test rule
id: 5013ef44-f37f-4b1f-99e0-0dcb0e5d3ac2
level: high
logsource: {product: test}
detection:
    sel:
        f: v
    condition: sel
"""


def _make_case(client, name="sigma-case") -> dict:
    resp = client.post("/api/cases/", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["case"]


def test_rule_upload_list_toggle_delete(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case = _make_case(client)

    resp = client.post(f"/api/cases/{case['id']}/sigma/rules", json={"yaml_content": VALID_RULE})
    assert resp.status_code == 200, resp.text
    rule = resp.json()["rule"]
    assert rule["title"] == "API test rule"
    assert rule["rule_key"] == "5013ef44f37f4b1f99e00dcb0e5d3ac2"
    assert rule["level"] == "high"
    assert len(rule["content_hash"]) == 64

    # Duplicate content → 409.
    resp = client.post(f"/api/cases/{case['id']}/sigma/rules", json={"yaml_content": VALID_RULE})
    assert resp.status_code == 409

    # Invalid YAML → 422.
    resp = client.post(
        f"/api/cases/{case['id']}/sigma/rules", json={"yaml_content": "title: [broken"}
    )
    assert resp.status_code == 422

    listing = client.get(f"/api/cases/{case['id']}/sigma/rules").json()
    assert len(listing["case_rules"]) == 1
    assert listing["case_rules"][0]["ref"] == rule["id"]

    resp = client.patch(
        f"/api/cases/{case['id']}/sigma/rules/{rule['id']}", json={"enabled": False}
    )
    assert resp.status_code == 200
    listing = client.get(f"/api/cases/{case['id']}/sigma/rules").json()
    assert listing["case_rules"][0]["enabled"] is False

    resp = client.delete(f"/api/cases/{case['id']}/sigma/rules/{rule['id']}")
    assert resp.status_code == 200
    listing = client.get(f"/api/cases/{case['id']}/sigma/rules").json()
    assert listing["case_rules"] == []


def test_global_rules_from_directory(client, admin_bootstrap, store, tmp_path, monkeypatch):
    (tmp_path / "one.yml").write_text(VALID_RULE)
    (tmp_path / "broken.yml").write_text("title: [unclosed")
    monkeypatch.setenv("VESTIGO_SIGMA_RULES_PATH", str(tmp_path))
    get_settings.cache_clear()
    try:
        as_admin(client, admin_bootstrap)
        data = client.get("/api/sigma/rules").json()
        assert data["rules_path_configured"] is True
        by_ref = {r["ref"]: r for r in data["rules"]}
        assert by_ref["one.yml"]["title"] == "API test rule"
        assert by_ref["broken.yml"]["error"] is not None
    finally:
        get_settings.cache_clear()


def test_run_requires_sources_and_valid_timeline(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case = _make_case(client, "sigma-run-case")

    resp = client.post(f"/api/cases/{case['id']}/timelines/nope/sigma/run", json={"rules": None})
    assert resp.status_code == 404

    timelines = client.get(f"/api/cases/{case['id']}/timelines").json()["timelines"]
    assert timelines, "expected the default timeline"
    tl_id = timelines[0]["id"]
    resp = client.post(f"/api/cases/{case['id']}/timelines/{tl_id}/sigma/run", json={"rules": None})
    # No sources ingested yet.
    assert resp.status_code == 422

    assert client.get(f"/api/cases/{case['id']}/sigma/runs").json()["runs"] == []


def test_non_member_denied(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case = _make_case(client, "sigma-rbac-case")
    resp = client.post(
        "/api/admin/users", json={"username": "sigmastranger", "password": "abcdefgh12"}
    )
    assert resp.status_code == 200
    other = client.__class__(client.app)
    login(other, "sigmastranger", "abcdefgh12")
    # Platform convention (deps.require_case): existing-but-forbidden → 403.
    resp = other.get(f"/api/cases/{case['id']}/sigma/rules")
    assert resp.status_code == 403
    resp = other.post(f"/api/cases/{case['id']}/sigma/rules", json={"yaml_content": VALID_RULE})
    assert resp.status_code == 403
