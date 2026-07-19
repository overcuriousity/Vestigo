"""API endpoints for the optional AI investigation agent (docs/AGENT.md).

All endpoints 503 unless the agent is configured and its endpoint answered
the availability probe — the frontend never renders agent UI in that state,
so a 503 here means someone is poking the API directly.

The message endpoint streams the agent's turn as SSE over a POST response
(the browser consumes it via fetch + ReadableStream; EventSource is GET-only).
Every step is persisted to ``agent_messages`` as it completes, and each tool
call is additionally recorded in the audit trail — an agent-assisted finding
must be explainable later from the case record alone.
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncGenerator
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from vestigo.agent.availability import agent_available
from vestigo.agent.config import resolve_agent_config
from vestigo.agent.runtime import dump_history, load_history, stream_turn
from vestigo.agent.tools import build_scope
from vestigo.api.deps import (
    get_current_user,
    get_store,
    require_case_contribute,
    require_case_read,
)
from vestigo.db.postgres import (
    ANNOTATION_ORIGIN_AGENT,
    AgentConversation,
    Case,
    User,
    generate_id,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cases", tags=["agent"])

_AGENT_UNAVAILABLE_DETAIL = (
    "The AI agent is not available: configure VESTIGO_AGENT_MODEL and "
    "VESTIGO_AGENT_API_BASE_URL (see docs/AGENT.md) and ensure the endpoint "
    "is reachable."
)

_TITLE_MAX = 80


async def _require_agent() -> None:
    if not await agent_available():
        raise HTTPException(status_code=503, detail=_AGENT_UNAVAILABLE_DETAIL)


async def _require_conversation(
    case_id: str, conversation_id: str, user: User
) -> AgentConversation:
    conversation = await get_store().get_agent_conversation(case_id, conversation_id)
    if conversation is None:
        raise HTTPException(status_code=404, detail="Conversation not found")
    if conversation.user_id != user.id:
        # Conversations are personal working notes; other analysts read the
        # audit trail, not each other's chats.
        raise HTTPException(status_code=404, detail="Conversation not found")
    return conversation


class CreateConversationRequest(BaseModel):
    timeline_id: str = Field(..., min_length=1)


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=32768)
    # Snapshot of the analyst's current Explorer filters (frontend
    # EventFilters shape) — injected as context so the agent is aware of what
    # the analyst is looking at.
    view_filters: dict[str, Any] | None = None


@router.post("/{case_id}/agent/conversations")
async def create_conversation(
    case_id: str,
    payload: CreateConversationRequest,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Create a new agent conversation scoped to a timeline."""
    await _require_agent()
    store = get_store()
    timeline = await store.get_timeline(case_id, payload.timeline_id)
    if timeline is None:
        raise HTTPException(status_code=404, detail="Timeline not found")
    config = await resolve_agent_config()
    conversation = await store.create_agent_conversation(
        case_id,
        payload.timeline_id,
        user.id,
        model_id=f"{config.provider}:{config.model}",
    )
    return conversation.to_dict()


@router.get("/{case_id}/agent/conversations")
async def list_conversations(
    case_id: str,
    timeline_id: str | None = None,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """List the current user's agent conversations for a case."""
    conversations = await get_store().list_agent_conversations(
        case_id, timeline_id=timeline_id, user_id=user.id
    )
    return {"conversations": [c.to_dict() for c in conversations]}


@router.get("/{case_id}/agent/conversations/{conversation_id}")
async def get_conversation(
    case_id: str,
    conversation_id: str,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Return one conversation with its full message history."""
    conversation = await _require_conversation(case_id, conversation_id, user)
    messages = await get_store().list_agent_messages(conversation_id)
    payload = conversation.to_dict()
    payload["messages"] = [m.to_dict() for m in messages]
    return payload


@router.delete("/{case_id}/agent/conversations/{conversation_id}")
async def delete_conversation(
    case_id: str,
    conversation_id: str,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Delete a conversation and its messages."""
    await _require_conversation(case_id, conversation_id, user)
    deleted = await get_store().delete_agent_conversation(case_id, conversation_id)
    return {"deleted": deleted}


def _sse(event: dict[str, Any]) -> str:
    return f"data: {json.dumps(event, default=str)}\n\n"


async def _message_stream(
    case_id: str,
    conversation: AgentConversation,
    payload: SendMessageRequest,
    user: User,
) -> AsyncGenerator[str]:
    store = get_store()
    conversation_id = conversation.id
    await store.add_agent_message(conversation_id, "user", payload.content)
    if not conversation.title:
        await store.update_agent_conversation(conversation_id, title=payload.content[:_TITLE_MAX])

    scope = await build_scope(
        case_id, conversation.timeline_id, user, conversation_id=conversation.id
    )
    history = load_history(conversation.history)
    text_parts: list[str] = []
    try:
        async for event in stream_turn(
            scope,
            user_text=payload.content,
            history=history,
            view_filters=payload.view_filters,
        ):
            if event["type"] == "result":
                turn = event["turn"]
                await store.add_agent_message(
                    conversation_id,
                    "assistant",
                    turn.output_text,
                    prompt_tokens=turn.prompt_tokens,
                    completion_tokens=turn.completion_tokens,
                )
                await store.update_agent_conversation(
                    conversation_id, history=dump_history(history + turn.new_messages)
                )
                yield _sse(
                    {
                        "type": "done",
                        "content": turn.output_text,
                        "prompt_tokens": turn.prompt_tokens,
                        "completion_tokens": turn.completion_tokens,
                    }
                )
                continue
            if event["type"] == "text_delta":
                text_parts.append(event["text"])
            elif event["type"] == "tool_call":
                await store.add_agent_message(
                    conversation_id,
                    "tool",
                    tool_name=event["tool"],
                    tool_args=event["args"],
                )
                # GET-style reads leave no middleware audit rows, so agent
                # tool calls get explicit ones — the custody trail must show
                # what the agent queried on whose behalf.
                await store.record_audit(
                    action="agent.tool_call",
                    actor=user,
                    case_id=case_id,
                    target_type="agent_conversation",
                    target_id=conversation_id,
                    detail={"tool": event["tool"], "args": event["args"]},
                )
            elif event["type"] == "tool_result":
                await store.add_agent_message(
                    conversation_id,
                    "tool",
                    tool_name=event["tool"],
                    tool_result=event["result"],
                )
            yield _sse(event)
    except Exception:
        logger.exception("Agent turn failed (conversation %s)", conversation_id)
        # Persist whatever streamed before the failure so the record stays
        # truthful, then tell the client.
        if text_parts:
            await store.add_agent_message(
                conversation_id, "assistant", "".join(text_parts) + " [interrupted]"
            )
        yield _sse({"type": "error", "detail": "Agent turn failed — see server logs."})


@router.post("/{case_id}/agent/conversations/{conversation_id}/messages")
async def send_message(
    case_id: str,
    conversation_id: str,
    payload: SendMessageRequest,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Send a user message and stream the agent's turn as SSE."""
    await _require_agent()
    conversation = await _require_conversation(case_id, conversation_id, user)
    return StreamingResponse(
        _message_stream(case_id, conversation, payload, user),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ---------------------------------------------------------------------------
# Proposals: propose->confirm annotation writes (A1)
# ---------------------------------------------------------------------------


def _proposal_resolver():
    """Confirm-time event re-resolution, patchable in tests.

    A thin indirection over :func:`vestigo.agent.tools._resolve_event_sources`
    so tests can substitute a fake resolver without needing a real
    ClickHouse-backed scope (the same seam used at propose time in
    ``propose_annotation``).
    """
    from vestigo.agent.tools import _resolve_event_sources

    return _resolve_event_sources


@router.get("/{case_id}/agent/conversations/{conversation_id}/proposals")
async def list_proposals(
    case_id: str,
    conversation_id: str,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """List a conversation's agent-proposed annotations, oldest first."""
    await _require_conversation(case_id, conversation_id, user)
    rows = await get_store().list_agent_proposals(conversation_id)
    return {"proposals": [p.to_dict() for p in rows]}


@router.post("/{case_id}/agent/conversations/{conversation_id}/proposals/{proposal_id}/confirm")
async def confirm_proposal(
    case_id: str,
    conversation_id: str,
    proposal_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Confirm an agent proposal, writing its annotations with origin agentic-analysis.

    Events are re-resolved against the current scope rather than trusted from
    propose time — a source may have left the timeline since the agent
    proposed the annotation. Whatever still resolves is written; the rest is
    reported back as ``skipped_event_ids`` so the record stays truthful.
    """
    conversation = await _require_conversation(case_id, conversation_id, user)
    store = get_store()
    proposal = await store.get_agent_proposal(conversation_id, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    decided = await store.decide_agent_proposal(
        proposal_id, status="confirmed", decided_by=user.username
    )
    if decided is None:
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal.status}")

    scope = await build_scope(case_id, conversation.timeline_id, user)
    found, unknown = await _proposal_resolver()(scope, [e["event_id"] for e in decided.events])
    rows = []
    for event in decided.events:
        if event["event_id"] not in found:
            continue
        for ann_type, content in (("tag", decided.tag), ("comment", decided.comment)):
            if content:
                rows.append(
                    {
                        "annotation_id": generate_id("ann"),
                        "case_id": case_id,
                        "source_id": event["source_id"],
                        "event_id": event["event_id"],
                        "annotation_type": ann_type,
                        "content": content,
                        "created_by": user.username,
                        "origin": ANNOTATION_ORIGIN_AGENT,
                    }
                )
    written = await store.bulk_create_annotations(rows)
    await store.record_audit(
        action="agent.annotation_confirm",
        actor=user,
        case_id=case_id,
        target_type="agent_proposal",
        target_id=proposal_id,
        detail={
            "conversation_id": conversation_id,
            "written": written,
            "skipped_event_ids": unknown,
            "tag": decided.tag,
            "comment_present": bool(decided.comment),
        },
    )
    return {"proposal": decided.to_dict(), "written": written, "skipped_event_ids": unknown}


@router.post("/{case_id}/agent/conversations/{conversation_id}/proposals/{proposal_id}/reject")
async def reject_proposal(
    case_id: str,
    conversation_id: str,
    proposal_id: str,
    case: Case = Depends(require_case_contribute),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Reject an agent proposal — no annotations are written."""
    await _require_conversation(case_id, conversation_id, user)
    store = get_store()
    proposal = await store.get_agent_proposal(conversation_id, proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail="Proposal not found")
    decided = await store.decide_agent_proposal(
        proposal_id, status="rejected", decided_by=user.username
    )
    if decided is None:
        raise HTTPException(status_code=409, detail=f"Proposal already {proposal.status}")
    await store.record_audit(
        action="agent.annotation_reject",
        actor=user,
        case_id=case_id,
        target_type="agent_proposal",
        target_id=proposal_id,
        detail={"conversation_id": conversation_id},
    )
    return {"proposal": decided.to_dict()}
