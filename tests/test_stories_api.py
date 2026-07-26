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


def _default_timeline(client, case_id: str) -> str:
    timelines = client.get(f"/api/cases/{case_id}/timelines").json()["timelines"]
    return timelines[0]["id"]


def _create_view(client, case_id: str, name: str = "SSH hits") -> dict:
    resp = client.post(f"/api/cases/{case_id}/views", json={"name": name, "query": "ssh"})
    assert resp.status_code == 200, resp.text
    return resp.json()["view"]


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

    ok = client.patch(f"{base}/{block['id']}", json={"content": {"text": "v2"}, "version": 1})
    assert ok.status_code == 200
    assert ok.json()["block"]["version"] == 2

    stale = client.patch(f"{base}/{block['id']}", json={"content": {"text": "v3"}, "version": 1})
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
            client.post(base, json={"kind": "markdown", "content": {"text": text}}).json()["block"][
                "id"
            ]
        )

    resp = client.post(f"{base}/{ids[2]}/move", json={"after_block_id": None, "version": 1})
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
    assert client.post(f"/api/cases/{case_id}/stories", json={"title": "nope"}).status_code == 403
    assert (
        client.post(
            f"/api/cases/{case_id}/stories/{story['id']}/blocks",
            json={"kind": "markdown", "content": {"text": "x"}},
        ).status_code
        == 403
    )


def test_rbac_read_access_cannot_write(client, admin_bootstrap, store, monkeypatch):
    """READ access reads the story but cannot change it.

    ``test_rbac_non_member_denied`` only covers a non-member, who is 403 on
    everything — it passes identically whether the write routes are gated on
    ``require_case_read`` or ``require_case_contribute``, so it does not
    actually verify the matrix. The team-role model never hands out a bare
    READ today (members get CONTRIBUTE), so the level is forced here to test
    the routes' own gating rather than the role mapping.
    """
    from vestigo.api import deps

    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}"
    block = client.post(
        f"{base}/blocks", json={"kind": "markdown", "content": {"text": "x"}}
    ).json()["block"]
    export = client.post(f"{base}/exports").json()["export"]

    async def _read_only(user, case):
        return deps.AccessLevel.READ

    monkeypatch.setattr(deps, "resolve_case_access", _read_only)

    # Reads stay open.
    assert client.get(f"/api/cases/{case_id}/stories").status_code == 200
    assert client.get(base).status_code == 200
    assert client.get(f"{base}/exports").status_code == 200
    assert client.get(f"{base}/exports/{export['id']}/snapshot").status_code == 200

    # Every mutation is closed.
    assert client.post(f"/api/cases/{case_id}/stories", json={"title": "n"}).status_code == 403
    assert client.patch(base, json={"title": "n"}).status_code == 403
    assert client.delete(base).status_code == 403
    assert (
        client.post(
            f"{base}/blocks", json={"kind": "markdown", "content": {"text": "y"}}
        ).status_code
        == 403
    )
    assert (
        client.patch(
            f"{base}/blocks/{block['id']}", json={"content": {"text": "y"}, "version": 1}
        ).status_code
        == 403
    )
    assert (
        client.post(
            f"{base}/blocks/{block['id']}/move", json={"after_block_id": None, "version": 1}
        ).status_code
        == 403
    )
    assert client.delete(f"{base}/blocks/{block['id']}").status_code == 403
    assert client.post(f"{base}/exports").status_code == 403
    assert (
        client.post(
            f"{base}/exports/{export['id']}/artifact", json={"html": "<p>x</p>"}
        ).status_code
        == 403
    )


def test_patch_story_only_touches_sent_fields(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    resp = client.post(f"/api/cases/{case_id}/stories", json={"title": "T", "description": "blurb"})
    story = resp.json()["story"]
    base = f"/api/cases/{case_id}/stories/{story['id']}"

    # A title-only patch leaves the description alone.
    patched = client.patch(base, json={"title": "T2"}).json()["story"]
    assert patched == {**patched, "title": "T2", "description": "blurb"}
    # An explicit null clears it.
    cleared = client.patch(base, json={"description": None}).json()["story"]
    assert cleared["description"] is None
    assert cleared["title"] == "T2"
    # A blank title is rejected the same way POST rejects one.
    assert client.patch(base, json={"title": "   "}).status_code == 422


def test_block_scope_validated_against_case(client, admin_bootstrap, store):
    """A foreign or mistyped referent is a 422 now, not a broken card later."""
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}/blocks"
    timeline_id = _default_timeline(client, case_id)
    view = _create_view(client, case_id)

    assert (
        client.post(
            base,
            json={
                "kind": "view_ref",
                "content": {"view_id": "ghost", "timeline_id": timeline_id},
            },
        ).status_code
        == 422
    )
    assert (
        client.post(
            base,
            json={"kind": "view_ref", "content": {"view_id": view["id"], "timeline_id": "ghost"}},
        ).status_code
        == 422
    )
    ok = client.post(
        base,
        json={"kind": "view_ref", "content": {"view_id": view["id"], "timeline_id": timeline_id}},
    )
    assert ok.status_code == 200, ok.text


def test_event_block_source_validated_against_case(client, admin_bootstrap, store):
    """An event block's source has to be one of this case's sources."""
    import asyncio

    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}/blocks"

    foreign = client.post("/api/cases/", json={"name": "other-case"}).json()["case"]

    async def _make_sources():
        await store.create_source(case_id, "src-mine", "mine.csv", "h1", 10)
        await store.create_source(foreign["id"], "src-theirs", "theirs.csv", "h2", 10)

    asyncio.run(_make_sources())

    assert (
        client.post(
            base,
            json={"kind": "event_ref", "content": {"event_id": "e1", "source_id": "src-theirs"}},
        ).status_code
        == 422
    )
    ok = client.post(
        base, json={"kind": "event_ref", "content": {"event_id": "e1", "source_id": "src-mine"}}
    )
    assert ok.status_code == 200, ok.text


def test_story_delete_with_exports_is_admin_only(client, admin_bootstrap, store, monkeypatch):
    """The cascade must not be a way around the admin-only export deletion.

    The non-admin here has genuine CONTRIBUTE access — a non-member would be
    403 from the access dependency and prove nothing about this gate.
    """
    from vestigo.api import deps

    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}"
    export = client.post(f"{base}/exports").json()["export"]

    client.post("/api/admin/users", json={"username": "worker", "password": "abcdefgh12"})

    async def _contribute(user, case):
        return deps.AccessLevel.CONTRIBUTE

    # Restored by hand rather than with monkeypatch.undo(), which would also
    # roll back the patches the client/store fixtures installed.
    real_resolve = deps.resolve_case_access
    monkeypatch.setattr(deps, "resolve_case_access", _contribute)
    login(client, "worker", "abcdefgh12")
    resp = client.delete(base)
    assert resp.status_code == 403, resp.text
    assert "administrator" in resp.json()["detail"]
    # And nothing was destroyed on the way to the refusal.
    assert client.get(f"{base}/exports").json()["exports"][0]["id"] == export["id"]

    monkeypatch.setattr(deps, "resolve_case_access", real_resolve)
    login(client, admin_bootstrap["username"], "rotated-pass-456")
    assert client.delete(base).status_code == 200
    rows = client.get("/api/admin/audit", params={"case_id": case_id}).json()["audit"]
    deletes = [r for r in rows if r["action"] == "story.delete"]
    assert deletes, rows
    hashes = [e["snapshot_hash"] for e in deletes[0]["detail"]["exports"]]
    assert hashes == [export["snapshot_hash"]]


def test_markdown_block_size_is_bounded(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}/blocks"
    from vestigo.core.config import get_settings

    oversize = "x" * (get_settings().story_max_markdown_bytes + 1)
    resp = client.post(base, json={"kind": "markdown", "content": {"text": oversize}})
    assert resp.status_code == 422, resp.text


def test_export_flow(client, admin_bootstrap, store):
    from vestigo.stories.schemas import canonical_hash

    as_admin(client, admin_bootstrap)
    case_id = _setup_case(client)
    story = _create_story(client, case_id)
    base = f"/api/cases/{case_id}/stories/{story['id']}"
    client.post(f"{base}/blocks", json={"kind": "markdown", "content": {"text": "# frozen"}})
    # A view ref that goes dangling after the fact (the view is deleted out
    # from under the block) must not fail the export — it freezes as an error.
    view = _create_view(client, case_id)
    timeline_id = _default_timeline(client, case_id)
    added = client.post(
        f"{base}/blocks",
        json={
            "kind": "view_ref",
            "content": {"view_id": view["id"], "timeline_id": timeline_id},
        },
    )
    assert added.status_code == 200, added.text
    assert client.delete(f"/api/cases/{case_id}/views/{view['id']}").status_code == 200

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

    # The artifact has to name the snapshot it claims to render.
    unbound = client.post(f"{base}/exports/{export['id']}/artifact", json={"html": "<p>r</p>"})
    assert unbound.status_code == 422

    # Seal the artifact exactly once.
    artifact = f"<p>r {export['snapshot_hash']}</p>"
    ok = client.post(f"{base}/exports/{export['id']}/artifact", json={"html": artifact})
    assert ok.status_code == 200
    assert ok.json()["export"]["has_artifact"] is True
    again = client.post(
        f"{base}/exports/{export['id']}/artifact",
        json={"html": f"<p>x {export['snapshot_hash']}</p>"},
    )
    assert again.status_code == 409

    # Downloads round-trip.
    snap = client.get(f"{base}/exports/{export['id']}/snapshot")
    assert snap.status_code == 200
    assert snap.json()["v"] == 1
    # The served bytes are the hashed bytes, so a third party can verify the
    # attestation without knowing our canonicalization rules.
    import hashlib as _hashlib

    assert _hashlib.sha256(snap.content).hexdigest() == export["snapshot_hash"]
    assert snap.headers["X-Vestigo-Snapshot-Hash"] == export["snapshot_hash"]
    art = client.get(f"{base}/exports/{export['id']}/artifact")
    assert art.status_code == 200
    assert art.text == artifact

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
