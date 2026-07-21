"""Tests for the admin console: user/team/membership CRUD and guardrails."""

from __future__ import annotations

import httpx
import pytest

from tests.conftest import as_admin, login
from vestigo.agent import availability
from vestigo.core.config import get_settings


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

    from vestigo.db.postgres import User as UserModel

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


def test_agent_settings_upsert_and_mask(client, admin_bootstrap, store):
    """update_agent_settings upserts the single 'global' row; only keys present
    in `values` change; a key present with value None clears that column; and
    to_dict(mask_key=True) never exposes the plaintext api_key."""
    import asyncio

    async def _exercise() -> None:
        assert await store.get_agent_settings() is None

        row = await store.update_agent_settings({"model": "qwen3:32b"}, "root")
        assert row.id == "global"
        assert row.model == "qwen3:32b"
        assert row.api_key is None
        assert row.updated_by == "root"

        row = await store.update_agent_settings({"api_key": "sk-x"}, "root")
        assert row.model == "qwen3:32b"  # preserved
        assert row.api_key == "sk-x"

        d = row.to_dict()
        assert d["api_key_set"] is True
        assert "api_key" not in d

        d_unmasked = row.to_dict(mask_key=False)
        assert d_unmasked["api_key"] == "sk-x"

        row = await store.update_agent_settings({"api_key": None}, "root")
        assert row.api_key is None
        assert row.model == "qwen3:32b"  # still preserved

        d = row.to_dict()
        assert d["api_key_set"] is False
        assert "api_key" not in d

        fetched = await store.get_agent_settings()
        assert fetched is not None
        assert fetched.id == "global"

    asyncio.run(_exercise())


# ═════════════════════════════════════════════════════════════════════════════
# Agent settings API (A7)
# ═════════════════════════════════════════════════════════════════════════════


@pytest.fixture(autouse=True)
def _reset_agent_probe_cache():
    availability.reset_probe_cache()
    yield
    availability.reset_probe_cache()


def test_agent_settings_requires_admin(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    client.post("/api/admin/users", json={"username": "plainagent", "password": "abcdefgh12"})

    plain_client = client.__class__(client.app)
    login(plain_client, "plainagent", "abcdefgh12")
    assert plain_client.get("/api/admin/agent-settings").status_code == 403
    assert plain_client.put("/api/admin/agent-settings", json={"model": "x"}).status_code == 403


def _unpinned_agent_config(monkeypatch):
    """Pin the resolved config to a known, env-free baseline.

    A developer `.env` can pin agent fields, which would otherwise make the
    override assertions here depend on the machine running the tests.
    """
    from vestigo.agent.config import AgentConfig
    from vestigo.api.routers import admin as admin_router

    config = AgentConfig(
        model=None,
        provider="openai",
        api_base_url=None,
        api_key=None,
        user_agent=None,
        extra_headers=None,
        max_turns=15,
        reasoning_effort="off",
        sources={},
    )

    async def _resolved(*args, **kwargs):
        return config

    monkeypatch.setattr(admin_router, "resolve_agent_config", _resolved)
    return config


def test_agent_models_lists_ids_from_the_endpoint(client, admin_bootstrap, store, monkeypatch):
    """The model picker's source: ids parsed out of the endpoint's listing."""
    seen: dict[str, object] = {}

    async def fake_get_models(config):
        seen["base_url"] = config.api_base_url
        seen["api_key"] = config.api_key
        return httpx.Response(
            200,
            json={"data": [{"id": "gpt-4o-mini"}, {"id": "gpt-4o"}, {"id": "gpt-4o"}]},
        )

    monkeypatch.setattr(availability, "_get_models", fake_get_models)
    _unpinned_agent_config(monkeypatch)
    as_admin(client, admin_bootstrap)

    resp = client.post(
        "/api/admin/agent-settings/models",
        json={"api_base_url": "http://llm.example/v1", "api_key": "sk-typed"},
    )
    assert resp.status_code == 200
    # Deduped and sorted.
    assert resp.json()["models"] == ["gpt-4o", "gpt-4o-mini"]
    # Unsaved form values are what got probed — the whole point is seeing an
    # endpoint's models before committing it.
    assert seen["base_url"] == "http://llm.example/v1"
    assert seen["api_key"] == "sk-typed"


def test_agent_models_empty_when_endpoint_gives_nothing(
    client, admin_bootstrap, store, monkeypatch
):
    """Unreachable, unparseable, or listing-free endpoints all degrade to the
    free-text fallback rather than surfacing an error."""
    as_admin(client, admin_bootstrap)

    async def unreachable(config):
        return None

    monkeypatch.setattr(availability, "_get_models", unreachable)
    resp = client.post("/api/admin/agent-settings/models", json={})
    assert resp.status_code == 200
    assert resp.json()["models"] == []

    async def not_a_listing(config):
        return httpx.Response(200, json={"object": "list"})

    monkeypatch.setattr(availability, "_get_models", not_a_listing)
    assert client.post("/api/admin/agent-settings/models", json={}).json()["models"] == []

    async def not_json(config):
        return httpx.Response(200, content=b"<html>nope</html>")

    monkeypatch.setattr(availability, "_get_models", not_json)
    assert client.post("/api/admin/agent-settings/models", json={}).json()["models"] == []


def test_agent_models_ignores_overrides_for_env_pinned_fields(
    client, admin_bootstrap, store, monkeypatch
):
    """An env-pinned field is not overridable per-request.

    Otherwise redirecting `api_base_url` while the key stays env-pinned would
    ship the operator's key — which this API never discloses — to a host the
    caller picked.
    """
    import dataclasses

    seen: dict[str, object] = {}

    async def fake_get_models(config):
        seen["base_url"] = config.api_base_url
        seen["api_key"] = config.api_key
        return httpx.Response(200, json={"data": []})

    monkeypatch.setattr(availability, "_get_models", fake_get_models)
    base = _unpinned_agent_config(monkeypatch)
    pinned = dataclasses.replace(
        base,
        api_base_url="http://pinned.example/v1",
        api_key="sk-env-secret",
        sources={"api_base_url": "env", "api_key": "env"},
    )

    async def _resolved(*args, **kwargs):
        return pinned

    from vestigo.api.routers import admin as admin_router

    monkeypatch.setattr(admin_router, "resolve_agent_config", _resolved)
    as_admin(client, admin_bootstrap)

    client.post(
        "/api/admin/agent-settings/models",
        json={"api_base_url": "http://attacker.example/v1"},
    )
    assert seen["base_url"] == "http://pinned.example/v1"
    assert seen["api_key"] == "sk-env-secret"


def test_agent_models_requires_admin(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    client.post("/api/admin/users", json={"username": "plainmodels", "password": "abcdefgh12"})

    plain_client = client.__class__(client.app)
    login(plain_client, "plainmodels", "abcdefgh12")
    assert plain_client.post("/api/admin/agent-settings/models", json={}).status_code == 403


def test_agent_settings_get_masks_key(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    resp = client.get("/api/admin/agent-settings")
    assert resp.status_code == 200
    body = resp.json()
    effective = body["effective"]
    assert "api_key" not in effective
    assert effective["api_key_set"] is False
    assert "sources" in body
    assert "env_vars" in body

    resp = client.put("/api/admin/agent-settings", json={"api_key": "sk-secret"})
    assert resp.status_code == 200
    effective = resp.json()["effective"]
    assert "api_key" not in effective
    assert effective["api_key_set"] is True


def test_agent_settings_put_persists_and_audits_names_only(client, admin_bootstrap, store):
    admin = as_admin(client, admin_bootstrap)
    resp = client.put(
        "/api/admin/agent-settings",
        json={"model": "qwen3:32b", "max_turns": 10, "api_key": "sk-secret-value"},
    )
    assert resp.status_code == 200
    effective = resp.json()["effective"]
    assert effective["model"] == "qwen3:32b"
    assert effective["max_turns"] == 10
    assert effective["api_key_set"] is True

    resp = client.get("/api/admin/agent-settings")
    assert resp.json()["effective"]["model"] == "qwen3:32b"

    resp = client.get("/api/admin/audit", params={"action": "admin.agent_settings_update"})
    rows = resp.json()["audit"]
    assert len(rows) == 1
    row = rows[0]
    assert row["user_id"] == admin["id"]
    assert set(row["detail"]["fields"]) == {"model", "max_turns", "api_key"}
    # Values, including the API key, must never land in the audit trail.
    assert "sk-secret-value" not in str(row["detail"])
    assert "qwen3:32b" not in str(row["detail"])


def test_agent_settings_put_null_clears_field(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    client.put("/api/admin/agent-settings", json={"model": "qwen3:32b"})
    resp = client.put("/api/admin/agent-settings", json={"model": None})
    assert resp.status_code == 200
    assert resp.json()["effective"]["model"] is None


def test_agent_settings_put_rejects_invalid_values(client, admin_bootstrap, store):
    as_admin(client, admin_bootstrap)
    assert client.put("/api/admin/agent-settings", json={"provider": "bogus"}).status_code == 422
    assert (
        client.put("/api/admin/agent-settings", json={"reasoning_effort": "extreme"}).status_code
        == 422
    )
    assert client.put("/api/admin/agent-settings", json={"max_turns": 0}).status_code == 422
    assert client.put("/api/admin/agent-settings", json={"max_turns": 101}).status_code == 422
    assert (
        client.put("/api/admin/agent-settings", json={"tool_fidelity": "lots"}).status_code == 422
    )


def test_agent_settings_tool_fidelity_round_trips(client, admin_bootstrap, store):
    """Unset resolves to `full`: a deployment that declared no context
    constraint is assumed to have room (agent/fidelity.py)."""
    as_admin(client, admin_bootstrap)
    assert client.get("/api/admin/agent-settings").json()["effective"]["tool_fidelity"] == "full"

    resp = client.put("/api/admin/agent-settings", json={"tool_fidelity": "auto"})
    assert resp.status_code == 200
    assert resp.json()["effective"]["tool_fidelity"] == "auto"
    assert resp.json()["sources"]["tool_fidelity"] == "db"

    cleared = client.put("/api/admin/agent-settings", json={"tool_fidelity": None})
    assert cleared.json()["effective"]["tool_fidelity"] == "full"


def test_agent_settings_put_triggers_reprobe(client, admin_bootstrap, store, monkeypatch):
    """A PUT resets the probe cache so the next health check re-probes immediately,
    reusing the counting-monkeypatch pattern from
    tests/test_agent_api.py::test_probe_cache_invalidates_on_config_change."""
    monkeypatch.setenv("VESTIGO_AGENT_MODEL", "test-model")
    monkeypatch.setenv("VESTIGO_AGENT_PROVIDER", "openai")
    monkeypatch.setenv("VESTIGO_AGENT_API_BASE_URL", "http://localhost:9/v1")
    get_settings.cache_clear()

    calls = {"n": 0}

    async def probe(config):
        calls["n"] += 1
        return True

    monkeypatch.setattr(availability, "_probe", probe)
    availability.reset_probe_cache()

    import asyncio

    assert asyncio.run(availability.agent_available()) is True
    assert asyncio.run(availability.agent_available()) is True
    assert calls["n"] == 1

    as_admin(client, admin_bootstrap)
    resp = client.put("/api/admin/agent-settings", json={"max_turns": 42})
    assert resp.status_code == 200

    assert asyncio.run(availability.agent_available()) is True
    assert calls["n"] == 2


def test_agent_settings_put_strips_whitespace(client, admin_bootstrap, store):
    """Pasted values carry stray whitespace (trailing spaces on a URL, a newline
    after an API key) — the PUT must normalize them, and a whitespace-only
    string must clear the field like an explicit null."""
    as_admin(client, admin_bootstrap)
    resp = client.put(
        "/api/admin/agent-settings",
        json={
            "model": " k3 ",
            "api_base_url": "https://api.kimi.com/coding     ",
            "api_key": "sk-kimi-x\n",
            "user_agent": "  claude-code/0.1.0",
        },
    )
    assert resp.status_code == 200
    effective = resp.json()["effective"]
    assert effective["model"] == "k3"
    assert effective["api_base_url"] == "https://api.kimi.com/coding"
    assert effective["user_agent"] == "claude-code/0.1.0"
    assert effective["api_key_set"] is True

    resp = client.put("/api/admin/agent-settings", json={"model": "   ", "api_key": " \n"})
    assert resp.status_code == 200
    effective = resp.json()["effective"]
    # Whitespace-only degrades to an explicit clear.
    assert effective["model"] is None
    assert effective["api_key_set"] is False


def test_agent_settings_env_only_mode_rejects_db_key_storage(
    client, admin_bootstrap, store, monkeypatch
):
    """VESTIGO_AGENT_SECRET_MODE=env-only (A10): the PUT refuses to store the
    api_key in Postgres (other fields still editable), the response advertises
    the mode, and a key already stored before the mode was enabled is ignored
    by the resolver rather than silently used."""
    import asyncio

    from vestigo.agent.config import resolve_agent_config

    as_admin(client, admin_bootstrap)

    # Key stored while in default "db" mode.
    resp = client.put("/api/admin/agent-settings", json={"api_key": "sk-stored"})
    assert resp.status_code == 200
    assert resp.json()["secret_mode"] == "db"

    monkeypatch.setenv("VESTIGO_AGENT_SECRET_MODE", "env-only")
    get_settings.cache_clear()
    try:
        resp = client.get("/api/admin/agent-settings")
        assert resp.json()["secret_mode"] == "env-only"
        # The pre-existing DB key is ignored by the resolver...
        assert resp.json()["effective"]["api_key_set"] is False
        config = asyncio.run(resolve_agent_config())
        assert config.api_key is None
        assert config.sources["api_key"] == "default"

        # ...and new writes are refused outright.
        resp = client.put("/api/admin/agent-settings", json={"api_key": "sk-new"})
        assert resp.status_code == 400
        assert "VESTIGO_AGENT_API_KEY" in resp.json()["detail"]
        # Clearing the stored key is still allowed (it's how you clean up).
        resp = client.put("/api/admin/agent-settings", json={"api_key": None})
        assert resp.status_code == 200
        # Non-secret fields stay editable.
        resp = client.put("/api/admin/agent-settings", json={"model": "qwen3:32b"})
        assert resp.status_code == 200
        assert resp.json()["effective"]["model"] == "qwen3:32b"
    finally:
        monkeypatch.delenv("VESTIGO_AGENT_SECRET_MODE", raising=False)
        get_settings.cache_clear()
