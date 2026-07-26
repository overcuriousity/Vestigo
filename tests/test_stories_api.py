"""Stories router: CRUD, block ops with optimistic concurrency, RBAC, audit."""

from __future__ import annotations

from tests.conftest import as_admin, login


def _setup_case(client) -> str:
    case = client.post("/api/cases/", json={"name": "story-case"}).json()["case"]
    return case["id"]


def _create_story(client, case_id: str, title: str = "Report") -> dict:
    resp = client.post(f"/api/cases/{case_id}/stories", json={"title": title})
    assert resp.status_code == 200, resp.text
    return resp.json()["story"]


def test_story_crud_flow(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)

    story = _create_story(client, case_id, "Intrusion 42")
    assert story["title"] == "Intrusion 42"

    listed = client.get(f"/api/cases/{case_id}/stories").json()["stories"]
    assert [s["id"] for s in listed] == [story["id"]]

    patched = client.patch(
        f"/api/cases/{case_id}/stories/{story['id']}", json={"title": "Final"}
    ).json()["story"]
    assert patched["title"] == "Final"

    detail = client.get(f"/api/cases/{case_id}/stories/{story['id']}").json()
    assert detail["story"]["id"] == story["id"]
    assert detail["blocks"] == []

    assert client.delete(f"/api/cases/{case_id}/stories/{story['id']}").status_code == 200
    assert client.get(f"/api/cases/{case_id}/stories/{story['id']}").status_code == 404


def test_story_create_requires_title(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    assert client.post(f"/api/cases/{case_id}/stories", json={"title": "  "}).status_code == 422


def test_block_create_validates_content(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}/blocks"

    assert client.post(base, json={"kind": "markdown", "content": {}}).status_code == 422
    assert client.post(base, json={"kind": "gif", "content": {}}).status_code == 422
    resp = client.post(base, json={"kind": "markdown", "content": {"text": "# hi"}})
    assert resp.status_code == 200
    block = resp.json()["block"]
    assert block["kind"] == "markdown"
    assert block["origin"] == "user"
    assert block["version"] == 1

    # Unknown after_block_id is a client error, not a 500.
    resp = client.post(
        base, json={"kind": "markdown", "content": {"text": "x"}, "after_block_id": "ghost"}
    )
    assert resp.status_code == 422


def test_block_update_conflict_409(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}/blocks"
    block = client.post(base, json={"kind": "markdown", "content": {"text": "v1"}}).json()["block"]

    ok = client.patch(
        f"{base}/{block['id']}", json={"content": {"text": "v2"}, "version": 1}
    )
    assert ok.status_code == 200
    assert ok.json()["block"]["version"] == 2

    stale = client.patch(
        f"{base}/{block['id']}", json={"content": {"text": "v3"}, "version": 1}
    )
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["block"]["version"] == 2
    assert detail["block"]["content"] == {"text": "v2"}


def test_block_move_and_order(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}/blocks"
    ids = []
    for text in ("one", "two", "three"):
        ids.append(
            client.post(base, json={"kind": "markdown", "content": {"text": text}}).json()[
                "block"
            ]["id"]
        )

    resp = client.post(
        f"{base}/{ids[2]}/move", json={"after_block_id": None, "version": 1}
    )
    assert resp.status_code == 200

    detail = client.get(f"/api/cases/{case_id}/stories/{story['id']}").json()
    assert [b["id"] for b in detail["blocks"]] == [ids[2], ids[0], ids[1]]

    # Move with a stale version 409s.
    stale = client.post(f"{base}/{ids[2]}/move", json={"after_block_id": ids[1], "version": 1})
    assert stale.status_code == 409

    # Block ids are validated against the story in the path.
    other = _create_story(client, case_id, "Other")
    cross = client.delete(f"/api/cases/{case_id}/stories/{other['id']}/blocks/{ids[0]}")
    assert cross.status_code == 404


def test_block_delete(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}/blocks"
    block = client.post(base, json={"kind": "markdown", "content": {"text": "x"}}).json()["block"]
    assert client.delete(f"{base}/{block['id']}").status_code == 200
    assert client.delete(f"{base}/{block['id']}").status_code == 404


def test_rbac_non_member_denied(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    client.post("/api/admin/users", json={"username": "outsider", "password": "abcdefgh12"})

    login(client, "outsider", "abcdefgh12")
    assert client.get(f"/api/cases/{case_id}/stories").status_code == 403
    assert (
        client.post(f"/api/cases/{case_id}/stories", json={"title": "nope"}).status_code == 403
    )
    assert (
        client.post(
            f"/api/cases/{case_id}/stories/{story['id']}/blocks",
            json={"kind": "markdown", "content": {"text": "x"}},
        ).status_code
        == 403
    )


def test_export_flow(client, admin_bootstrap, store):
    from vestigo.stories.schemas import canonical_hash

    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}"
    client.post(f"{base}/blocks", json={"kind": "markdown", "content": {"text": "# frozen"}})
    # A dangling view ref must not fail the export — it freezes as an error.
    client.post(
        f"{base}/blocks",
        json={"kind": "view_ref", "content": {"view_id": "ghost", "timeline_id": "t1"}},
    )

    resp = client.post(f"{base}/exports")
    assert resp.status_code == 200, resp.text
    export = resp.json()["export"]
    snapshot = export["snapshot"]
    assert snapshot["v"] == 1
    md, view = snapshot["blocks"]
    assert md["data"] == {"text": "# frozen"}
    assert view["resolution"]["error"] is not None
    assert export["snapshot_hash"] == canonical_hash(snapshot)
    assert export["has_artifact"] is False

    # Seal the artifact exactly once.
    ok = client.post(f"{base}/exports/{export['id']}/artifact", json={"html": "<p>r</p>"})
    assert ok.status_code == 200
    assert ok.json()["export"]["has_artifact"] is True
    again = client.post(f"{base}/exports/{export['id']}/artifact", json={"html": "<p>x</p>"})
    assert again.status_code == 409

    # Downloads round-trip.
    snap = client.get(f"{base}/exports/{export['id']}/snapshot")
    assert snap.status_code == 200
    assert snap.json()["v"] == 1
    art = client.get(f"{base}/exports/{export['id']}/artifact")
    assert art.status_code == 200
    assert art.text == "<p>r</p>"

    listed = client.get(f"{base}/exports").json()["exports"]
    assert [e["id"] for e in listed] == [export["id"]]
    assert "snapshot" not in listed[0]

    rows = client.get("/api/admin/audit", params={"case_id": case_id}).json()["audit"]
    assert "story.export" in [r["action"] for r in rows]


def test_export_artifact_missing_404(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}"
    export = client.post(f"{base}/exports").json()["export"]
    assert client.get(f"{base}/exports/{export['id']}/artifact").status_code == 404
    assert client.get(f"{base}/exports/ghost/snapshot").status_code == 404


def test_export_delete_admin_only(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}"
    export = client.post(f"{base}/exports").json()["export"]

    client.post("/api/admin/users", json={"username": "worker", "password": "abcdefgh12"})
    login(client, "worker", "abcdefgh12")
    assert client.delete(f"{base}/exports/{export['id']}").status_code == 403

    # as_admin already rotated the bootstrap password; log back in directly.
    login(client, admin_bootstrap["username"], "rotated-pass-456")
    assert client.delete(f"{base}/exports/{export['id']}").status_code == 200
    rows = client.get("/api/admin/audit", params={"case_id": case_id}).json()["audit"]
    assert "story.export_delete" in [r["action"] for r in rows]


def test_story_lifecycle_audited(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    client.delete(f"/api/cases/{case_id}/stories/{story['id']}")

    rows = client.get("/api/admin/audit", params={"case_id": case_id}).json()["audit"]
    actions = [r["action"] for r in rows]
    assert "story.create" in actions
    assert "story.delete" in actions
