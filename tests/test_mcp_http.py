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


def test_mcp_body_cap_413(mcp_client, admin_bootstrap, monkeypatch):
    """The buffered request body is capped — oversized requests get a 413."""
    from vestigo.agent import mcp_http

    monkeypatch.setattr(mcp_http, "_MAX_BODY_BYTES", 64)
    as_admin(mcp_client, admin_bootstrap)
    case_id, tl_id, token = _setup_token(mcp_client)
    resp = mcp_client.post(
        "/mcp",
        content=b"x" * 1024,
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 413


def test_mcp_batch_tools_call_still_audited(mcp_client, admin_bootstrap, store):
    """A JSON-RPC batch array writes one agent.tool_call audit row per member.

    The SDK transport rejects batches (2025-06-18 spec removed them) — the
    audit sniffing must not depend on that, so the rows exist regardless of
    the transport's response.
    """
    import asyncio

    as_admin(mcp_client, admin_bootstrap)
    case_id, tl_id, token = _setup_token(mcp_client)
    mcp_client.post(
        "/mcp",
        json=[
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_baselines", "arguments": {}},
            },
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "list_saved_views", "arguments": {}},
            },
        ],
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {token}",
        },
    )

    async def _rows():
        return await store.query_audit(case_id=case_id, action="agent.tool_call")

    rows = asyncio.run(_rows())
    audited_tools = {r.detail["tool"] for r in rows}
    assert {"list_baselines", "list_saved_views"} <= audited_tools


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


def test_mcp_tool_results_honour_tool_fidelity(mcp_client, admin_bootstrap, monkeypatch):
    """`tool_fidelity` describes what the deployment can afford to hand a
    model, and an external client is driving one just the same — so the
    external transport spends the configured tier rather than always `full`."""
    import vestigo.api.routers.events as events_router

    class _Page:
        total = 1
        events = [
            {
                "event_id": "e1",
                "source_id": "s1",
                "message": "login attempt [svc-a/rock] succeeded",
                "attributes": {"user": "svc-a"},
            }
        ]

    class _Service:
        def query(self, query):
            return _Page()

    monkeypatch.setattr(events_router, "_get_query_service", lambda: _Service())
    monkeypatch.setenv("VESTIGO_AGENT_TOOL_FIDELITY", "minimal")
    get_settings.cache_clear()
    try:
        as_admin(mcp_client, admin_bootstrap)
        case_id, tl_id, token = _setup_token(mcp_client)
        assert _rpc_initialize(mcp_client, token).status_code == 200
        called = mcp_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "search_events", "arguments": {}},
            },
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
        )
        assert called.status_code == 200, called.text
        assert "minimal" in called.text
        # The reduction is real, not just declared.
        assert "svc-a" not in called.text
    finally:
        monkeypatch.delenv("VESTIGO_AGENT_TOOL_FIDELITY", raising=False)
        get_settings.cache_clear()


def test_mcp_tools_list_respects_admin_disabled(mcp_client, admin_bootstrap, monkeypatch):
    """The admin hard-deny list applies to the external transport too."""
    monkeypatch.setenv("VESTIGO_AGENT_DISABLED_TOOLS", '["list_baselines"]')
    get_settings.cache_clear()
    try:
        as_admin(mcp_client, admin_bootstrap)
        case_id, tl_id, token = _setup_token(mcp_client)
        assert _rpc_initialize(mcp_client, token).status_code == 200
        listed = mcp_client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json, text/event-stream",
                "Authorization": f"Bearer {token}",
            },
        )
        assert listed.status_code == 200, listed.text
        # Parse tool *names* from the SSE data frame — "list_baselines" also
        # appears inside another tool's description text.
        import json as _json

        data_line = next(line for line in listed.text.splitlines() if line.startswith("data: "))
        names = {t["name"] for t in _json.loads(data_line[len("data: ") :])["result"]["tools"]}
        assert "list_baselines" not in names
        assert "list_saved_views" in names
    finally:
        get_settings.cache_clear()
