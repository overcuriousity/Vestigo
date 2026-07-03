"""Tests for the case-access matrix: admin/manager/member/personal-owner/default-pool."""

from __future__ import annotations

from tests.conftest import as_admin, login


def _create_user(client, username: str, password: str = "abcdefgh12") -> dict:
    resp = client.post("/api/admin/users", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return resp.json()["user"]


def _create_team(client, name: str) -> dict:
    resp = client.post("/api/admin/teams", json={"name": name})
    assert resp.status_code == 200, resp.text
    return resp.json()["team"]


def _add_member(client, team_id: str, user_id: str, role: str = "member") -> None:
    resp = client.post(
        f"/api/admin/teams/{team_id}/members", json={"user_id": user_id, "role": role}
    )
    assert resp.status_code == 200, resp.text


def test_personal_case_is_owner_only(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    alice = _create_user(client, "alice")
    _create_user(client, "bob")

    alice_client = client.__class__(client.app)
    login(alice_client, "alice", "abcdefgh12")
    case = alice_client.post("/api/cases/", json={"name": "alice-personal"}).json()["case"]
    assert case["owner_id"] == alice["id"]
    assert case["team_id"] is None

    bob_client = client.__class__(client.app)
    login(bob_client, "bob", "abcdefgh12")
    resp = bob_client.get(f"/api/cases/{case['id']}")
    assert resp.status_code == 403

    # Admin can still see everything.
    resp = client.get(f"/api/cases/{case['id']}")
    assert resp.status_code == 200


def test_team_member_can_read_and_contribute_but_not_manage(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = _create_team(client, "Blue Team")
    manager = _create_user(client, "manager1")
    member = _create_user(client, "member1")
    _add_member(client, team["id"], manager["id"], role="manager")
    _add_member(client, team["id"], member["id"], role="member")

    mgr_client = client.__class__(client.app)
    login(mgr_client, "manager1", "abcdefgh12")
    case = mgr_client.post("/api/cases/", json={"name": "team-case", "team_id": team["id"]}).json()[
        "case"
    ]
    assert case["team_id"] == team["id"]

    mem_client = client.__class__(client.app)
    login(mem_client, "member1", "abcdefgh12")

    # Member can read the team case.
    assert mem_client.get(f"/api/cases/{case['id']}").status_code == 200

    # Member can contribute: add a timeline.
    resp = mem_client.post(f"/api/cases/{case['id']}/timelines", json={"name": "tl1"})
    assert resp.status_code == 200

    # Member cannot delete the case (manage-only).
    resp = mem_client.delete(f"/api/cases/{case['id']}")
    assert resp.status_code == 403

    # Manager can delete it.
    resp = mgr_client.delete(f"/api/cases/{case['id']}")
    assert resp.status_code == 200


def test_plain_member_cannot_create_team_case(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = _create_team(client, "Red Team")
    member = _create_user(client, "member2")
    _add_member(client, team["id"], member["id"], role="member")

    mem_client = client.__class__(client.app)
    login(mem_client, "member2", "abcdefgh12")
    resp = mem_client.post("/api/cases/", json={"name": "nope", "team_id": team["id"]})
    assert resp.status_code == 403


def test_cross_team_member_has_no_access(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team_a = _create_team(client, "Team A")
    team_b = _create_team(client, "Team B")
    manager_a = _create_user(client, "mgr_a")
    outsider = _create_user(client, "outsider")
    _add_member(client, team_a["id"], manager_a["id"], role="manager")
    _add_member(client, team_b["id"], outsider["id"], role="member")

    mgr_a_client = client.__class__(client.app)
    login(mgr_a_client, "mgr_a", "abcdefgh12")
    case = mgr_a_client.post(
        "/api/cases/", json={"name": "team-a-case", "team_id": team_a["id"]}
    ).json()["case"]

    outsider_client = client.__class__(client.app)
    login(outsider_client, "outsider", "abcdefgh12")
    resp = outsider_client.get(f"/api/cases/{case['id']}")
    assert resp.status_code == 403


def test_default_pool_user_sees_only_own_cases(client, admin_bootstrap, store):
    """A user with no team membership (e.g. a freshly OIDC-provisioned
    account) only sees cases they personally created."""
    as_admin(client, admin_bootstrap)
    _create_user(client, "pooled")

    pooled_client = client.__class__(client.app)
    login(pooled_client, "pooled", "abcdefgh12")
    resp = pooled_client.get("/api/cases/")
    assert resp.status_code == 200
    assert resp.json()["cases"] == []

    pooled_client.post("/api/cases/", json={"name": "my-own-case"})
    resp = pooled_client.get("/api/cases/")
    assert len(resp.json()["cases"]) == 1


def test_owner_can_release_personal_case_to_own_managed_team(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = _create_team(client, "Green Team")
    alice = _create_user(client, "alice3")
    _add_member(client, team["id"], alice["id"], role="manager")

    alice_client = client.__class__(client.app)
    login(alice_client, "alice3", "abcdefgh12")
    case = alice_client.post("/api/cases/", json={"name": "alice-solo"}).json()["case"]
    assert case["team_id"] is None

    resp = alice_client.patch(f"/api/cases/{case['id']}/scope", json={"team_id": team["id"]})
    assert resp.status_code == 200, resp.text
    assert resp.json()["case"]["team_id"] == team["id"]

    # Now a team case: another team manager (not this team) can't reach it,
    # but a fellow member of Green Team should have read access.
    member = _create_user(client, "green-member")
    _add_member(client, team["id"], member["id"], role="member")
    member_client = client.__class__(client.app)
    login(member_client, "green-member", "abcdefgh12")
    assert member_client.get(f"/api/cases/{case['id']}").status_code == 200


def test_cannot_assign_case_to_team_you_do_not_manage(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team_a = _create_team(client, "Owner Team")
    team_b = _create_team(client, "Other Team")
    alice = _create_user(client, "alice4")
    _add_member(client, team_a["id"], alice["id"], role="manager")

    alice_client = client.__class__(client.app)
    login(alice_client, "alice4", "abcdefgh12")
    case = alice_client.post("/api/cases/", json={"name": "alice-solo2"}).json()["case"]

    resp = alice_client.patch(f"/api/cases/{case['id']}/scope", json={"team_id": team_b["id"]})
    assert resp.status_code == 403


def test_team_member_cannot_change_case_scope_only_manager_can(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = _create_team(client, "Yellow Team")
    manager = _create_user(client, "manager2")
    member = _create_user(client, "member3")
    _add_member(client, team["id"], manager["id"], role="manager")
    _add_member(client, team["id"], member["id"], role="member")

    mgr_client = client.__class__(client.app)
    login(mgr_client, "manager2", "abcdefgh12")
    case = mgr_client.post(
        "/api/cases/", json={"name": "yellow-case", "team_id": team["id"]}
    ).json()["case"]

    mem_client = client.__class__(client.app)
    login(mem_client, "member3", "abcdefgh12")
    resp = mem_client.patch(f"/api/cases/{case['id']}/scope", json={"team_id": None})
    assert resp.status_code == 403

    # Manager can release it back to personal (owner still the original creator).
    resp = mgr_client.patch(f"/api/cases/{case['id']}/scope", json={"team_id": None})
    assert resp.status_code == 200
    assert resp.json()["case"]["team_id"] is None


def test_list_cases_scoped_per_user_not_global(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    client.post("/api/cases/", json={"name": "admin-case"})
    _create_user(client, "alice2")

    alice_client = client.__class__(client.app)
    login(alice_client, "alice2", "abcdefgh12")
    resp = alice_client.get("/api/cases/")
    assert resp.json()["cases"] == []

    # Admin sees everything, including alice's own case once she makes one.
    alice_client.post("/api/cases/", json={"name": "alice-case"})
    resp = client.get("/api/cases/")
    names = {c["name"] for c in resp.json()["cases"]}
    assert {"admin-case", "alice-case"} <= names
