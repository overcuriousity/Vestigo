"""Scoped MCP access tokens for the external agent endpoint (docs/AGENT.md).

A token is bound to one case + timeline at creation; presenting it to the
/mcp endpoint yields exactly that scope. Only the SHA-256 is stored — the
plaintext appears once in the creation response. Access is re-validated
against the creating user's live case RBAC on every MCP connect, so these
endpoints only need READ (the token can never do more than read tools).
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from vestigo.api.deps import get_current_user, get_store, require_case_read
from vestigo.db.postgres import Case, User

router = APIRouter(prefix="/api/cases", tags=["agent-tokens"])

TOKEN_PREFIX = "vgo_"


def hash_token(plaintext: str) -> str:
    """SHA-256 hex digest of a presented token — the only stored identity."""
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


class CreateTokenRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=255)
    expires_in_days: int | None = Field(default=None, ge=1, le=3650)


@router.post("/{case_id}/timelines/{timeline_id}/agent-tokens")
async def create_agent_token(
    case_id: str,
    timeline_id: str,
    payload: CreateTokenRequest,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a scoped MCP token; the plaintext is returned exactly once."""
    store = get_store()
    if await store.get_timeline(case_id, timeline_id) is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    plaintext = TOKEN_PREFIX + secrets.token_urlsafe(32)
    expires_at = (
        datetime.now(UTC) + timedelta(days=payload.expires_in_days)
        if payload.expires_in_days
        else None
    )
    row = await store.create_agent_token(
        case_id, timeline_id, user.id, payload.name, hash_token(plaintext), expires_at=expires_at
    )
    await store.record_audit(
        action="agent_token.create",
        actor=user,
        case_id=case_id,
        target_type="agent_token",
        target_id=row.id,
        detail={"name": payload.name, "timeline_id": timeline_id},
    )
    return {**row.to_dict(), "token": plaintext}


@router.get("/{case_id}/timelines/{timeline_id}/agent-tokens")
async def list_agent_tokens(
    case_id: str,
    timeline_id: str,
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """List a timeline's MCP tokens (metadata only, revoked included)."""
    rows = await get_store().list_agent_tokens(case_id, timeline_id)
    return {"tokens": [r.to_dict() for r in rows]}


@router.delete("/{case_id}/timelines/{timeline_id}/agent-tokens/{token_id}")
async def revoke_agent_token(
    case_id: str,
    timeline_id: str,
    token_id: str,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Revoke a token immediately (checked on every MCP connect)."""
    revoked = await get_store().revoke_agent_token(case_id, token_id)
    if not revoked:
        raise HTTPException(status_code=404, detail="Token not found")
    await get_store().record_audit(
        action="agent_token.revoke",
        actor=user,
        case_id=case_id,
        target_type="agent_token",
        target_id=token_id,
    )
    return {"revoked": True}
