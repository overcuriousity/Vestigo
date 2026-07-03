"""Tests for the admin console: user/team/membership CRUD and guardrails."""

from __future__ import annotations

from tests.conftest import as_admin, login


def test_non_admin_cannot_reach_admin_routes(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    client.post("/api/admin/users", json={"username": "plain", "password": "abcdefgh12"})

    plain_client = client.__class__(client.app)
    login(plain_client, "plain", "abcdefgh12")
    assert plain_client.get("/api/admin/users").status_code == 403
    assert plain_client.post("/api/admin/teams", json={"name": "x"}).status_code == 403


def test_duplicate_username_rejected(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.post("/api/admin/users", json={"username": "dupe", "password": "abcdefgh12"})
    assert resp.status_code == 200
    resp = client.post("/api/admin/users", json={"username": "dupe", "password": "abcdefgh12"})
    assert resp.status_code == 409


def test_admin_cannot_delete_own_account(client, admin_bootstrap, store):
    me = as_admin(client, admin_bootstrap)
    resp = client.delete(f"/api/admin/users/{me['id']}")
    assert resp.status_code == 400


def test_admin_cannot_remove_own_admin_flag(client, admin_bootstrap, store):
    me = as_admin(client, admin_bootstrap)
    resp = client.patch(f"/api/admin/users/{me['id']}", json={"is_admin": False})
    assert resp.status_code == 400


def test_delete_user_owning_cases_requires_reassignment(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.post("/api/admin/users", json={"username": "owner1", "password": "abcdefgh12"})
    owner = resp.json()["user"]

    owner_client = client.__class__(client.app)
    login(owner_client, "owner1", "abcdefgh12")
    case = owner_client.post("/api/cases/", json={"name": "owned-case"}).json()["case"]

    resp = client.delete(f"/api/admin/users/{owner['id']}")
    assert resp.status_code == 409

    me = client.get("/api/auth/me").json()["user"]
    resp = client.delete(f"/api/admin/users/{owner['id']}", params={"reassign_to": me["id"]})
    assert resp.status_code == 200

    resp = client.get(f"/api/cases/{case['id']}")
    assert resp.status_code == 200
    assert resp.json()["case"]["owner_id"] == me["id"]


def test_rotate_password_forces_change_and_revokes_sessions(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.post("/api/admin/users", json={"username": "rotateme", "password": "abcdefgh12"})
    user_id = resp.json()["user"]["id"]

    victim_client = client.__class__(client.app)
    login(victim_client, "rotateme", "abcdefgh12")
    assert victim_client.get("/api/auth/me").status_code == 200

    resp = client.post(f"/api/admin/users/{user_id}/password", json={"new_password": "newone1234"})
    assert resp.status_code == 200

    # Old session is dead.
    assert victim_client.get("/api/auth/me").status_code == 401

    fresh = client.__class__(client.app)
    payload = login(fresh, "rotateme", "newone1234")
    assert payload["user"]["must_change_password"] is True


def test_team_and_membership_crud(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = client.post("/api/admin/teams", json={"name": "Ops"}).json()["team"]
    user = client.post(
        "/api/admin/users", json={"username": "opsuser", "password": "abcdefgh12"}
    ).json()["user"]

    resp = client.post(
        f"/api/admin/teams/{team['id']}/members", json={"user_id": user["id"], "role": "member"}
    )
    assert resp.status_code == 200

    # Duplicate membership rejected.
    resp = client.post(
        f"/api/admin/teams/{team['id']}/members", json={"user_id": user["id"], "role": "member"}
    )
    assert resp.status_code == 409

    resp = client.get(f"/api/admin/teams/{team['id']}/members")
    assert len(resp.json()["members"]) == 1
    assert resp.json()["members"][0]["role"] == "member"

    resp = client.patch(
        f"/api/admin/teams/{team['id']}/members/{user['id']}", json={"role": "manager"}
    )
    assert resp.status_code == 200
    resp = client.get(f"/api/admin/teams/{team['id']}/members")
    assert resp.json()["members"][0]["role"] == "manager"

    resp = client.delete(f"/api/admin/teams/{team['id']}/members/{user['id']}")
    assert resp.status_code == 200
    resp = client.get(f"/api/admin/teams/{team['id']}/members")
    assert resp.json()["members"] == []


def test_deleting_team_reverts_its_cases_to_personal(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = client.post("/api/admin/teams", json={"name": "Doomed"}).json()["team"]
    manager = client.post(
        "/api/admin/users", json={"username": "doomedmgr", "password": "abcdefgh12"}
    ).json()["user"]
    client.post(
        f"/api/admin/teams/{team['id']}/members", json={"user_id": manager["id"], "role": "manager"}
    )

    mgr_client = client.__class__(client.app)
    login(mgr_client, "doomedmgr", "abcdefgh12")
    case = mgr_client.post("/api/cases/", json={"name": "team-case", "team_id": team["id"]}).json()[
        "case"
    ]

    resp = client.delete(f"/api/admin/teams/{team['id']}")
    assert resp.status_code == 200

    resp = mgr_client.get(f"/api/cases/{case['id']}")
    assert resp.status_code == 200
    assert resp.json()["case"]["team_id"] is None
    # The former manager remains the owner and keeps manage access.
    assert resp.json()["case"]["owner_id"] == manager["id"]


def test_duplicate_team_name_rejected(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.post("/api/admin/teams", json={"name": "Ops"})
    assert resp.status_code == 200
    resp = client.post("/api/admin/teams", json={"name": "Ops"})
    assert resp.status_code == 409


def test_rotate_password_rejected_for_oidc_account(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    user = client.post(
        "/api/admin/users", json={"username": "localuser", "password": "abcdefgh12"}
    ).json()["user"]

    # Flip the freshly-created local user to an OIDC-provisioned one at the
    # store level — there's no API surface to do this, since OIDC accounts
    # are only ever created via the OIDC callback.
    import asyncio

    from tracesignal.db.postgres import User as UserModel

    async def _make_oidc() -> None:
        async with store.session_factory() as session:
            row = await session.get(UserModel, user["id"])
            row.auth_provider = "oidc"
            row.oidc_subject = "sub-123"
            await session.commit()

    asyncio.run(_make_oidc())

    resp = client.post(
        f"/api/admin/users/{user['id']}/password", json={"new_password": "newone1234"}
    )
    assert resp.status_code == 409


def test_default_pool_listing(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = client.post("/api/admin/teams", json={"name": "HasTeam"}).json()["team"]
    teamed = client.post(
        "/api/admin/users", json={"username": "teamed", "password": "abcdefgh12"}
    ).json()["user"]
    client.post(f"/api/admin/teams/{team['id']}/members", json={"user_id": teamed["id"]})
    client.post("/api/admin/users", json={"username": "unassigned1", "password": "abcdefgh12"})

    resp = client.get("/api/admin/users", params={"unassigned": True})
    usernames = {u["username"] for u in resp.json()["users"]}
    assert "unassigned1" in usernames
    assert "teamed" not in usernames
