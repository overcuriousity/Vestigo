"""Job-status endpoint authorization: owner/admin plus case RBAC (M17)."""

from __future__ import annotations

from tests.conftest import as_admin, login
from tracesignal.core.jobs import get_job_store


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


def _login_as(client, username: str):
    other = client.__class__(client.app)
    login(other, username, "abcdefgh12")
    return other


def test_case_member_can_poll_teammates_job(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = _create_team(client, "Job Team")
    manager = _create_user(client, "jobmgr")
    member = _create_user(client, "jobmember")
    _add_member(client, team["id"], manager["id"], role="manager")
    _add_member(client, team["id"], member["id"], role="member")

    mgr_client = _login_as(client, "jobmgr")
    case = mgr_client.post("/api/cases/", json={"name": "jobs-case", "team_id": team["id"]}).json()[
        "case"
    ]

    job = get_job_store().create(kind="ingest", created_by=manager["id"], case_id=case["id"])

    member_client = _login_as(client, "jobmember")
    resp = member_client.get(f"/api/jobs/{job.id}")
    assert resp.status_code == 200
    assert resp.json()["job"]["id"] == job.id
    assert resp.json()["job"]["case_id"] == case["id"]


def test_non_member_cannot_poll_case_job(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = _create_team(client, "Job Team 2")
    manager = _create_user(client, "jobmgr2")
    _create_user(client, "jobstranger")
    _add_member(client, team["id"], manager["id"], role="manager")

    mgr_client = _login_as(client, "jobmgr2")
    case = mgr_client.post(
        "/api/cases/", json={"name": "jobs-case-2", "team_id": team["id"]}
    ).json()["case"]

    job = get_job_store().create(kind="embed", created_by=manager["id"], case_id=case["id"])

    stranger_client = _login_as(client, "jobstranger")
    # 404, not 403 — job IDs must not be probeable for existence.
    assert stranger_client.get(f"/api/jobs/{job.id}").status_code == 404


def test_system_job_visible_to_case_members(client, admin_bootstrap, store):
    """Auto-enrichment jobs have created_by=None; case RBAC still grants access."""
    as_admin(client, admin_bootstrap)
    team = _create_team(client, "Job Team 3")
    member = _create_user(client, "jobmember3")
    _add_member(client, team["id"], member["id"], role="member")

    case = client.post("/api/cases/", json={"name": "jobs-case-3", "team_id": team["id"]}).json()[
        "case"
    ]
    job = get_job_store().create(kind="enrich", created_by=None, case_id=case["id"])

    member_client = _login_as(client, "jobmember3")
    assert member_client.get(f"/api/jobs/{job.id}").status_code == 200


def test_case_jobs_list_scoped_to_case_and_requires_read_access(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    team = _create_team(client, "Job Team 4")
    manager = _create_user(client, "jobmgr4")
    member = _create_user(client, "jobmember4")
    _create_user(client, "jobstranger4")
    _add_member(client, team["id"], manager["id"], role="manager")
    _add_member(client, team["id"], member["id"], role="member")

    mgr_client = _login_as(client, "jobmgr4")
    case = mgr_client.post(
        "/api/cases/", json={"name": "jobs-case-4", "team_id": team["id"]}
    ).json()["case"]
    other_case = mgr_client.post(
        "/api/cases/", json={"name": "jobs-case-4-other", "team_id": team["id"]}
    ).json()["case"]

    job = get_job_store().create(kind="ingest", created_by=manager["id"], case_id=case["id"])
    get_job_store().create(kind="embed", created_by=manager["id"], case_id=other_case["id"])

    member_client = _login_as(client, "jobmember4")
    resp = member_client.get(f"/api/cases/{case['id']}/jobs")
    assert resp.status_code == 200
    job_ids = [j["id"] for j in resp.json()["jobs"]]
    assert job_ids == [job.id]

    stranger_client = _login_as(client, "jobstranger4")
    # require_case_read denies with 403 (unlike the single-job route's 404-for-
    # unguessability trick — case IDs aren't secrets the way job IDs are).
    assert stranger_client.get(f"/api/cases/{case['id']}/jobs").status_code == 403


def test_caseless_job_keeps_owner_or_admin_semantics(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    owner = _create_user(client, "jobowner")
    _create_user(client, "jobother")

    job = get_job_store().create(kind="embed", created_by=owner["id"], case_id=None)

    owner_client = _login_as(client, "jobowner")
    assert owner_client.get(f"/api/jobs/{job.id}").status_code == 200

    other_client = _login_as(client, "jobother")
    assert other_client.get(f"/api/jobs/{job.id}").status_code == 404

    # Admin sees everything.
    assert client.get(f"/api/jobs/{job.id}").status_code == 200
