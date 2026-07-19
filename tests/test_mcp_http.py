"""End-to-end tests for the /mcp streamable-HTTP endpoint with token auth."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from tests.conftest import as_admin
from vestigo.api.main import create_app
from vestigo.api.routers.agent_tokens import hash_token
from vestigo.core.config import get_settings


@pytest.fixture()
def mcp_client(store, admin_bootstrap, monkeypatch):
    monkeypatch.setenv("VESTIGO_MCP_ENABLED", "1")
    get_settings.cache_clear()
    app = create_app()
    with TestClient(app) as c:
        yield c
    get_settings.cache_clear()


def _setup_token(client) -> tuple[str, str, str]:
    """Create case + timeline + token via the API. Caller must be logged in (as_admin)."""
    case = client.post("/api/cases/", json={"name": "mcp-case"}).json()["case"]
    tl = client.post(f"/api/cases/{case['id']}/timelines", json={"name": "tl"}).json()["timeline"]
    token = client.post(
        f"/api/cases/{case['id']}/timelines/{tl['id']}/agent-tokens", json={"name": "e2e"}
    ).json()["token"]
    return case["id"], tl["id"], token


def _rpc_initialize(client, token: str | None, path: str = "/mcp"):
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        path,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "test", "version": "0"},
            },
        },
        headers=headers,
    )


def test_mcp_absent_when_disabled(client, admin_bootstrap):
    resp = _rpc_initialize(client, token=None)
    assert resp.status_code == 404
    assert client.get("/api/health").json()["mcp_enabled"] is False


def test_mcp_requires_valid_token(mcp_client, admin_bootstrap):
    assert mcp_client.get("/api/health").json()["mcp_enabled"] is True
    assert _rpc_initialize(mcp_client, token=None).status_code == 401
    assert _rpc_initialize(mcp_client, token="vgo_wrong").status_code == 401


def test_mcp_accepts_valid_token(mcp_client, admin_bootstrap):
    as_admin(mcp_client, admin_bootstrap)
    case_id, tl_id, token = _setup_token(mcp_client)
    ok = _rpc_initialize(mcp_client, token)
    assert ok.status_code == 200, ok.text


async def test_mcp_rejects_revoked_and_expired_rows(store):
    """Auth-decision unit test at the store level (revoked/expired distinguishable)."""
    from vestigo.agent.mcp_http import _token_auth_error

    await store.init_schema()
    valid = await store.create_agent_token("c1", "t1", "u1", "ok", hash_token("vgo_a"))
    assert _token_auth_error(valid) is None

    revoked = await store.create_agent_token("c1", "t1", "u1", "rev", hash_token("vgo_b"))
    await store.revoke_agent_token("c1", revoked.id)
    revoked = await store.get_agent_token_by_hash(hash_token("vgo_b"))
    assert _token_auth_error(revoked) == "token revoked"

    expired = await store.create_agent_token(
        "c1",
        "t1",
        "u1",
        "exp",
        hash_token("vgo_c"),
        expires_at=datetime.now(UTC) - timedelta(days=1),
    )
    assert _token_auth_error(expired) == "token expired"


def test_mcp_end_to_end_tool_call(mcp_client, admin_bootstrap):
    """Full streamable-HTTP round trip: initialize, list tools, call one."""
    as_admin(mcp_client, admin_bootstrap)
    case_id, tl_id, token = _setup_token(mcp_client)

    init = _rpc_initialize(mcp_client, token)
    assert init.status_code == 200, init.text

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": f"Bearer {token}",
    }
    listed = mcp_client.post(
        "/mcp",
        json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        headers=headers,
    )
    assert listed.status_code == 200, listed.text
    assert "list_baselines" in listed.text
    assert "propose_annotation" not in listed.text

    called = mcp_client.post(
        "/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "list_baselines", "arguments": {}},
        },
        headers=headers,
    )
    assert called.status_code == 200, called.text
    assert '"total"' in called.text
