"""Streamable-HTTP MCP endpoint serving the scoped agent tool server.

External MCP clients (Claude Code, hermes-agent, …) connect with
``Authorization: Bearer vgo_…`` — a scoped token minted per case+timeline
(``api/routers/agent_tokens.py``). The wrapper authenticates, re-checks the
creating user's live case RBAC, builds the exact same tool server the
built-in agent uses (``build_tool_server``), and delegates the request to a
per-request stateless MCP app. Scope comes from the token, never the model —
the scope-safety invariant holds on this transport too.

Tool calls are audited like the built-in loop's (``agent.tool_call``), with
the token id in the detail.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from vestigo.db._dt import ensure_utc

logger = logging.getLogger(__name__)

# Hard cap on the buffered MCP request body (JSON-RPC messages are small;
# 10 MiB is generous headroom for large tool arguments).
_MAX_BODY_BYTES = 10 * 1024 * 1024


def _token_auth_error(row: Any | None) -> str | None:
    """Return the auth-failure reason for a token row, or None when usable."""
    if row is None:
        return "unknown token"
    if row.revoked_at is not None:
        return "token revoked"
    if row.expires_at is not None and ensure_utc(row.expires_at) < datetime.now(UTC):
        return "token expired"
    return None


async def _authenticate(headers: dict[bytes, bytes]) -> tuple[Any, Any] | JSONResponse:
    """Resolve the Bearer token to (token_row, user) or an error response."""
    from vestigo.api.deps import AccessLevel, get_store, has_case_access
    from vestigo.api.routers.agent_tokens import TOKEN_PREFIX, hash_token

    auth = headers.get(b"authorization", b"").decode()
    if not auth.startswith("Bearer ") or not auth[7:].startswith(TOKEN_PREFIX):
        return JSONResponse(status_code=401, content={"detail": "Bearer token required"})
    store = get_store()
    row = await store.get_agent_token_by_hash(hash_token(auth[7:]))
    reason = _token_auth_error(row)
    if reason is not None:
        return JSONResponse(status_code=401, content={"detail": reason})
    user = await store.get_user(row.user_id)
    if user is None or not user.is_active:
        return JSONResponse(status_code=401, content={"detail": "token user inactive"})
    case = await store.get_case(row.case_id)
    if not await has_case_access(user, case, AccessLevel.READ):
        return JSONResponse(status_code=403, content={"detail": "case access revoked"})
    return row, user


async def _audit_tool_call(body: bytes, token_row: Any, user: Any) -> None:
    """Best-effort agent.tool_call audit row(s) for tools/call requests.

    Handles both a single JSON-RPC object and a batch array (one audit row
    per ``tools/call`` member). The MCP SDK's streamable-HTTP transport
    currently rejects batches (removed in the 2025-06-18 spec), so the array
    branch is defense in depth: the custody trail must not depend on a
    transport implementation detail.
    """
    from vestigo.api.deps import get_store

    try:
        parsed = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return
    messages = parsed if isinstance(parsed, list) else [parsed]
    for message in messages:
        if not isinstance(message, dict) or message.get("method") != "tools/call":
            continue
        params = message.get("params") or {}
        try:
            await get_store().record_audit(
                action="agent.tool_call",
                actor=user,
                case_id=token_row.case_id,
                target_type="agent_token",
                target_id=token_row.id,
                detail={
                    "tool": params.get("name"),
                    "args": params.get("arguments"),
                    "transport": "mcp_http",
                },
            )
        except Exception:
            # Audit is best-effort — a logging hiccup must never fail the tool call.
            logger.exception("Failed to write agent.tool_call audit row for MCP request")


class MCPEndpoint:
    """ASGI app mounted at /mcp: Bearer auth + per-request scoped MCP dispatch."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            return
        from vestigo.core.config import get_settings

        # Registered unconditionally so a disabled endpoint answers a clean 404
        # (rather than the SPA catch-all's 405); when off it is invisible.
        if not get_settings().mcp_enabled:
            await JSONResponse(status_code=404, content={"detail": "Not Found"})(
                scope, receive, send
            )
            return
        headers = dict(scope.get("headers") or [])
        auth = await _authenticate(headers)
        if isinstance(auth, JSONResponse):
            await auth(scope, receive, send)
            return
        token_row, user = auth

        # Buffer the request body once: audit sniffs it, then the inner app
        # re-reads it through a replaying receive. Capped — an authenticated
        # client must not be able to balloon server memory with one request.
        body = b""
        more = True
        while more:
            message = await receive()
            body += message.get("body", b"")
            if len(body) > _MAX_BODY_BYTES:
                await JSONResponse(status_code=413, content={"detail": "Request body too large"})(
                    scope, receive, send
                )
                return
            more = message.get("more_body", False)
        await _audit_tool_call(body, token_row, user)

        sent = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal sent
            if not sent:
                sent = True
                return {"type": "http.request", "body": body, "more_body": False}
            return await receive()

        from vestigo.agent.config import resolve_agent_config
        from vestigo.agent.tools import build_scope, build_tool_server

        try:
            # Only the admin hard-deny layer applies here — per-user/per-chat
            # tool preferences are an in-app concept. Resolved per request on
            # purpose: an admin deny must apply to the next /mcp call, not
            # after some cache TTL. One small Postgres read per request is
            # fine at this deployment's scale.
            config = await resolve_agent_config()
            agent_scope = await build_scope(
                token_row.case_id,
                token_row.timeline_id,
                user,
                disabled_tools=frozenset(config.disabled_tools or ()),
            )
        except HTTPException as exc:
            await JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})(
                scope, replay_receive, send
            )
            return

        # The inner Starlette app registers a single route at
        # ``streamable_http_path`` ("/"). Depending on how the outer app routes
        # us (an exact Route passes the unstripped "/mcp"; a Mount strips its
        # prefix to "" or "/"), the incoming path varies — but there is only one
        # MCP endpoint, so pin the inner path to "/" so it always matches.
        inner_scope = dict(scope)
        inner_scope["path"] = "/"

        from mcp.server.transport_security import TransportSecuritySettings

        server = build_tool_server(agent_scope)
        server.settings.stateless_http = True
        server.settings.streamable_http_path = "/"
        # FastMCP enables DNS-rebinding host validation by default (allowing only
        # its configured host). That protection guards browser-based attacks that
        # rely on ambient credentials; this endpoint authenticates with an
        # explicit Bearer token no cross-origin page can read, and Host handling
        # belongs to the deployment's reverse proxy — so disable it here.
        server.settings.transport_security = TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        )
        app = server.streamable_http_app()
        async with server.session_manager.run():
            await app(inner_scope, replay_receive, send)
