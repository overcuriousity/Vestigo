"""AgentToken store + API + MCP auth tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import as_admin


@pytest.mark.asyncio
async def test_agent_token_store_roundtrip(store):
    await store.init_schema()

    row = await store.create_agent_token("c1", "t1", "u1", "claude-code", "a" * 64)
    assert row.id and row.revoked_at is None

    listed = await store.list_agent_tokens("c1", "t1")
    assert [t.id for t in listed] == [row.id]
    assert "token_hash" not in row.to_dict()

    by_hash = await store.get_agent_token_by_hash("a" * 64)
    assert by_hash is not None and by_hash.id == row.id
    assert await store.get_agent_token_by_hash("b" * 64) is None

    assert await store.revoke_agent_token("c1", row.id) is True
    revoked = await store.get_agent_token_by_hash("a" * 64)
    assert revoked is not None and revoked.revoked_at is not None
    assert await store.revoke_agent_token("c1", "missing") is False


@pytest.mark.asyncio
async def test_agent_token_expiry_field(store):
    await store.init_schema()

    exp = datetime.now(UTC) + timedelta(days=30)
    row = await store.create_agent_token("c1", "t1", "u1", "temp", "c" * 64, expires_at=exp)
    assert row.expires_at is not None


def _case_and_timeline(client) -> tuple[str, str]:
    case = client.post("/api/cases/", json={"name": "token-case"}).json()["case"]
    tl = client.post(f"/api/cases/{case['id']}/timelines", json={"name": "tl"}).json()["timeline"]
    return case["id"], tl["id"]


def test_token_api_lifecycle(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _case_and_timeline(client)
    base = f"/api/cases/{case_id}/timelines/{tl_id}/agent-tokens"

    created = client.post(base, json={"name": "claude-code"})
    assert created.status_code == 200, created.text
    body = created.json()
    assert body["token"].startswith("vgo_")
    assert body["name"] == "claude-code"

    listed = client.get(base).json()["tokens"]
    assert len(listed) == 1
    assert "token" not in listed[0] and "token_hash" not in listed[0]

    revoked = client.delete(f"{base}/{body['id']}")
    assert revoked.status_code == 200
    assert client.get(base).json()["tokens"][0]["revoked_at"] is not None

    assert client.delete(f"{base}/missing").status_code == 404


def test_token_create_rejects_unknown_timeline(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, _ = _case_and_timeline(client)
    resp = client.post(f"/api/cases/{case_id}/timelines/nope/agent-tokens", json={"name": "x"})
    assert resp.status_code == 404


def test_token_create_with_expiry(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, tl_id = _case_and_timeline(client)
    resp = client.post(
        f"/api/cases/{case_id}/timelines/{tl_id}/agent-tokens",
        json={"name": "temp", "expires_in_days": 7},
    )
    assert resp.status_code == 200
    assert resp.json()["expires_at"] is not None
