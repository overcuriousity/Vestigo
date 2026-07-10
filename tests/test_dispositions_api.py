"""Unified finding-disposition taxonomy: API CRUD, scope invariants, RBAC,
audit trail, and the reproducibility hash (see routers/dispositions.py and
db/postgres.py::FindingDisposition)."""

from __future__ import annotations

import pytest

from tests.conftest import as_admin
from tracesignal.db.postgres import FindingDisposition, dispositions_hash


def _setup_case(client) -> tuple[str, str]:
    case = client.post("/api/cases/", json={"name": "dispo-case"}).json()["case"]
    timelines = client.get(f"/api/cases/{case['id']}/timelines").json()["timelines"]
    return case["id"], timelines[0]["id"]


def _base(case_id: str, tl_id: str) -> str:
    return f"/api/cases/{case_id}/timelines/{tl_id}/dispositions"


def test_disposition_crud_and_idempotent_create(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    body = {
        "kind": "normal",
        "detector": "value_novelty",
        "field": "attr:user",
        "value": "svc_backup",
        "note": "known service account",
    }
    row = client.post(base, json=body).json()["disposition"]
    assert row["kind"] == "normal"
    assert row["timeline_id"] == tl_id

    # Identical declaration is idempotent — same row, no duplicate.
    again = client.post(base, json=body).json()["disposition"]
    assert again["id"] == row["id"]
    assert len(client.get(base).json()["dispositions"]) == 1

    resp = client.delete(f"{base}/{row['id']}")
    assert resp.status_code == 200
    assert client.get(base).json()["dispositions"] == []
    assert client.delete(f"{base}/{row['id']}").status_code == 404


def test_disposition_scope_invariants(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    # No scope at all.
    assert client.post(base, json={"kind": "normal"}).status_code == 422
    # Half a value scope.
    assert client.post(base, json={"kind": "normal", "field": "attr:x"}).status_code == 422
    # Half an event scope.
    assert client.post(base, json={"kind": "dismissed", "event_id": "e1"}).status_code == 422
    # Both scopes at once.
    assert (
        client.post(
            base,
            json={
                "kind": "normal",
                "field": "attr:x",
                "value": "v",
                "source_id": "s1",
                "event_id": "e1",
            },
        ).status_code
        == 422
    )
    # confirmed requires event scope…
    assert (
        client.post(
            base,
            json={"kind": "confirmed", "detector": "charset", "field": "attr:x", "value": "v"},
        ).status_code
        == 422
    )
    # …and a concrete detector.
    assert (
        client.post(
            base, json={"kind": "confirmed", "source_id": "s1", "event_id": "e1"}
        ).status_code
        == 422
    )
    # Unknown kind rejected by the model.
    assert (
        client.post(base, json={"kind": "meh", "field": "attr:x", "value": "v"}).status_code
        == 422
    )


def test_event_scoped_rows_have_no_timeline_and_list_by_source(client, admin_bootstrap, store):
    """Event-scoped rows carry timeline_id=NULL; the list endpoint surfaces
    them for a timeline via its sources."""
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    row = client.post(
        base,
        json={"kind": "dismissed", "detector": "timestamp_order", "source_id": "s-none", "event_id": "e1"},
    ).json()["disposition"]
    assert row["timeline_id"] is None
    # Not listed: source "s-none" isn't attached to the timeline.
    assert client.get(base).json()["dispositions"] == []


def test_list_filters_by_kind_and_detector(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    client.post(base, json={"kind": "normal", "detector": "charset", "field": "f", "value": "v1"})
    client.post(base, json={"kind": "dismissed", "detector": "entropy", "field": "f", "value": "v2"})
    client.post(base, json={"kind": "normal", "detector": "*", "field": "f", "value": "v3"})

    assert len(client.get(base).json()["dispositions"]) == 3
    assert len(client.get(base, params={"kind": "normal"}).json()["dispositions"]) == 2
    # detector filter matches concrete + wildcard rows.
    charset = client.get(base, params={"detector": "charset"}).json()["dispositions"]
    assert {d["value"] for d in charset} == {"v1", "v3"}
    assert client.get(base, params={"kind": "nope"}).status_code == 422


def test_disposition_mutations_are_audited(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    row = client.post(
        base, json={"kind": "normal", "detector": "charset", "field": "f", "value": "v"}
    ).json()["disposition"]
    bulk = client.post(
        f"{base}/bulk",
        json={
            "items": [
                {"kind": "dismissed", "detector": "entropy", "field": "f", "value": "v2"},
                {"kind": "dismissed", "detector": "entropy", "field": "f", "value": "v3"},
            ]
        },
    ).json()["dispositions"]
    assert len(bulk) == 2
    client.delete(f"{base}/{row['id']}")

    audit = client.get("/api/admin/audit", params={"action": "disposition.create"}).json()
    assert any(r["target_id"] == row["id"] for r in audit["audit"])
    audit = client.get("/api/admin/audit", params={"action": "disposition.bulk_create"}).json()
    assert any(r["detail"]["count"] == 2 for r in audit["audit"])
    audit = client.get("/api/admin/audit", params={"action": "disposition.delete"}).json()
    assert any(r["target_id"] == row["id"] for r in audit["audit"])


def test_bulk_validates_everything_before_writing(client, admin_bootstrap, store):
    """A bulk request with one invalid item writes nothing."""
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _setup_case(client)
    base = _base(case_id, tl_id)

    resp = client.post(
        f"{base}/bulk",
        json={
            "items": [
                {"kind": "normal", "detector": "charset", "field": "f", "value": "ok"},
                {"kind": "normal", "field": "half-scope-only"},
            ]
        },
    )
    assert resp.status_code == 422
    assert client.get(base).json()["dispositions"] == []


def test_dispositions_hash_covers_normal_only_and_event_scope():
    """dismissed/confirmed never change the hash; event-scoped normals do —
    the reproducibility gap the old allowlist_hash left open."""
    value_normal = FindingDisposition(
        kind="normal", detector="charset", field="f", value="v"
    )
    event_normal = FindingDisposition(
        kind="normal", detector="*", source_id="s1", event_id="e1"
    )
    dismissed = FindingDisposition(
        kind="dismissed", detector="charset", field="f", value="x"
    )
    confirmed = FindingDisposition(
        kind="confirmed", detector="charset", source_id="s1", event_id="e2"
    )

    base = dispositions_hash([value_normal])
    assert len(base) == 64
    # Deterministic and order-independent.
    assert dispositions_hash([event_normal, value_normal]) == dispositions_hash(
        [value_normal, event_normal]
    )
    # Presentation-only / escalation rows don't affect it.
    assert dispositions_hash([value_normal, dismissed, confirmed]) == base
    # An event-scoped normal DOES affect it.
    assert dispositions_hash([value_normal, event_normal]) != base


@pytest.mark.asyncio
async def test_store_create_disposition_idempotent(store):
    await store.init_schema()
    a = await store.create_disposition(
        case_id="c1", kind="normal", detector="charset", timeline_id="t1", field="f", value="v"
    )
    b = await store.create_disposition(
        case_id="c1", kind="normal", detector="charset", timeline_id="t1", field="f", value="v"
    )
    assert a.id == b.id
    # Same key with a different kind is a distinct verdict, not a duplicate.
    c = await store.create_disposition(
        case_id="c1", kind="dismissed", detector="charset", timeline_id="t1", field="f", value="v"
    )
    assert c.id != a.id
