"""API tests for timeline field mappings (issue #10): create/patch validation,
persistence, and audit entries. ClickHouse-backed field discovery is stubbed."""

from __future__ import annotations

import pytest

from tests.conftest import as_admin
from tracesignal.api.routers import cases as cases_router

MAPPINGS = {"ip_address": ["src_ip", "ip_addr"]}


@pytest.fixture()
def fake_inventory(monkeypatch):
    """Stub the field-stats cache used by mapping validation (M15)."""

    async def fake_ensure(store, clickhouse, case_id, source_ids):
        payload = {
            "top_level": {},
            "attributes": {
                key: {"distinct": 1, "coverage": 1, "samples": []}
                for key in ("src_ip", "ip_addr", "status")
            },
        }
        return dict.fromkeys(source_ids, (3, payload))

    monkeypatch.setattr(cases_router, "ensure_source_field_stats", fake_ensure)


def _create_case(client) -> str:
    resp = client.post("/api/cases/", json={"name": "Mapping case"})
    assert resp.status_code == 200, resp.text
    return resp.json()["case"]["id"]


def test_create_timeline_with_valid_mappings(client, admin_bootstrap, fake_inventory):
    as_admin(client, admin_bootstrap)
    case_id = _create_case(client)

    resp = client.post(
        f"/api/cases/{case_id}/timelines",
        json={"name": "merged", "field_mappings": MAPPINGS},
    )
    assert resp.status_code == 200, resp.text
    timeline = resp.json()["timeline"]
    assert timeline["field_mappings"] == MAPPINGS

    # Round-trips through GET.
    resp = client.get(f"/api/cases/{case_id}/timelines/{timeline['id']}")
    assert resp.json()["timeline"]["field_mappings"] == MAPPINGS


def test_create_timeline_rejects_core_column_collision(client, admin_bootstrap, fake_inventory):
    as_admin(client, admin_bootstrap)
    case_id = _create_case(client)
    resp = client.post(
        f"/api/cases/{case_id}/timelines",
        json={"name": "bad", "field_mappings": {"message": ["src_ip"]}},
    )
    assert resp.status_code == 422
    assert "core event column" in resp.json()["detail"]


async def test_create_timeline_rejects_unknown_raw_field(
    client, admin_bootstrap, fake_inventory, store
):
    as_admin(client, admin_bootstrap)
    case_id = _create_case(client)
    # The unknown-raw-key check only runs against ready sources — a case with
    # no (ready) sources has no inventory to validate against.
    await store.create_source(case_id, "s1", "source one", file_hash="h1", size_bytes=10)
    resp = client.post(
        f"/api/cases/{case_id}/timelines",
        json={"name": "bad", "source_ids": ["s1"], "field_mappings": {"ip": ["nope"]}},
    )
    assert resp.status_code == 422
    assert "does not exist" in resp.json()["detail"]


def test_create_timeline_skips_inventory_check_without_ready_sources(
    client, admin_bootstrap, fake_inventory
):
    """With zero ready sources there is no attribute inventory yet — the
    inventory-dependent checks are skipped (structural rules still apply,
    see test_create_timeline_rejects_core_column_collision)."""
    as_admin(client, admin_bootstrap)
    case_id = _create_case(client)
    resp = client.post(
        f"/api/cases/{case_id}/timelines",
        json={"name": "ok", "field_mappings": {"ip": ["nope"]}},
    )
    assert resp.status_code == 200, resp.text


def test_patch_replaces_and_clears_mappings(client, admin_bootstrap, fake_inventory):
    as_admin(client, admin_bootstrap)
    case_id = _create_case(client)
    tid = client.post(f"/api/cases/{case_id}/timelines", json={"name": "t"}).json()["timeline"][
        "id"
    ]

    resp = client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/field-mappings",
        json={"field_mappings": MAPPINGS},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["timeline"]["field_mappings"] == MAPPINGS

    resp = client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/field-mappings",
        json={"field_mappings": None},
    )
    assert resp.status_code == 200
    assert resp.json()["timeline"]["field_mappings"] is None


async def test_patch_validates_against_timeline_sources(
    client, admin_bootstrap, fake_inventory, store
):
    as_admin(client, admin_bootstrap)
    case_id = _create_case(client)
    await store.create_source(case_id, "s1", "source one", file_hash="h1", size_bytes=10)
    tid = client.post(
        f"/api/cases/{case_id}/timelines", json={"name": "t", "source_ids": ["s1"]}
    ).json()["timeline"]["id"]
    resp = client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/field-mappings",
        json={"field_mappings": {"ip": ["missing_key"]}},
    )
    assert resp.status_code == 422


def test_patch_unknown_timeline_404(client, admin_bootstrap, fake_inventory):
    as_admin(client, admin_bootstrap)
    case_id = _create_case(client)
    resp = client.patch(
        f"/api/cases/{case_id}/timelines/nope/field-mappings",
        json={"field_mappings": MAPPINGS},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_mapping_changes_are_audited(client, admin_bootstrap, fake_inventory, store):
    as_admin(client, admin_bootstrap)
    case_id = _create_case(client)
    tid = client.post(
        f"/api/cases/{case_id}/timelines",
        json={"name": "t", "field_mappings": MAPPINGS},
    ).json()["timeline"]["id"]
    client.patch(
        f"/api/cases/{case_id}/timelines/{tid}/field-mappings",
        json={"field_mappings": None},
    )

    entries = await store.query_audit(case_id=case_id)
    actions = [e.action for e in entries]
    assert "timeline.create" in actions
    assert "timeline.update_field_mappings" in actions
    update = next(e for e in entries if e.action == "timeline.update_field_mappings")
    assert update.detail == {"previous": MAPPINGS, "new": None}
