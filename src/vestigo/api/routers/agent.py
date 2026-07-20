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

import asyncio
import json
import logging
import re
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, field_validator
from pydantic_ai.exceptions import ModelHTTPError

from vestigo import __version__
from vestigo.agent.availability import agent_available
from vestigo.agent.compaction import (
    compact_history,
    estimate_next_prompt_tokens,
    should_compact,
)
from vestigo.agent.config import DEFAULT_MAX_TURNS, resolve_agent_config
from vestigo.agent.runtime import LLM_TIMEOUT, dump_history, load_history, stream_turn
from vestigo.agent.tools import TOOL_NAMES, TOOL_REGISTRY, build_scope
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

# Non-case-scoped agent endpoints: the config disclosure behind the
# new-conversation OPSEC notice, and per-user tool preferences.
info_router = APIRouter(prefix="/api/agent", tags=["agent"])

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


def _validate_tool_names(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    unknown = sorted(set(value) - TOOL_NAMES)
    if unknown:
        raise ValueError(f"unknown tool name(s): {', '.join(unknown)}")
    return sorted(set(value))


class CreateConversationRequest(BaseModel):
    timeline_id: str = Field(..., min_length=1)
    # Per-chat tool restriction chosen in the new-conversation dialog
    # (user defaults + modal edits, already resolved client-side). Frozen on
    # the conversation; the admin hard-deny list is unioned in per turn.
    disabled_tools: list[str] | None = None

    _check_tools = field_validator("disabled_tools")(_validate_tool_names)


class UpdateConversationRequest(BaseModel):
    """Mutable fields on an existing conversation. Tool set only, for now.

    Omitted means "leave alone", not "clear" — `[]` is a meaningful value here
    (re-enable every tool), so a PATCH that doesn't mention `disabled_tools`
    must not silently widen the agent's reach.
    """

    disabled_tools: list[str] | None = None

    _check_tools = field_validator("disabled_tools")(_validate_tool_names)


class SendMessageRequest(BaseModel):
    content: str = Field(..., min_length=1, max_length=32768)
    # Snapshot of the analyst's current Explorer filters (frontend
    # EventFilters shape) — injected as context so the agent is aware of what
    # the analyst is looking at.
    view_filters: dict[str, Any] | None = None


def _conversation_payload(conversation: AgentConversation) -> dict[str, Any]:
    """Conversation dict plus the live `active` flag.

    `active` is process state, not a column — it says whether a turn is
    streaming *right now*, which is what lets a panel that was closed and
    reopened (or a second tab) show a working Stop instead of a dead input.
    """
    payload = conversation.to_dict()
    payload["active"] = turn_is_active(conversation.id)
    return payload


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
        disabled_tools=payload.disabled_tools,
    )
    return _conversation_payload(conversation)


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
    return {"conversations": [_conversation_payload(c) for c in conversations]}


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
    payload = _conversation_payload(conversation)
    payload["messages"] = [m.to_dict() for m in messages]
    return payload


@router.get("/{case_id}/agent/conversations/{conversation_id}/export")
async def export_conversation(
    case_id: str,
    conversation_id: str,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> Response:
    """Export the full conversation as a JSON attachment.

    Contains every message row (user/assistant/tool/thinking/compaction,
    with tool args/results and measured token usage), the proposals, and
    ``raw_history`` — the provider-wire pydantic-ai blob, the only place
    thinking signatures and provider quirks live. Deliberately not gated on
    ``_require_agent``: the record must stay exportable while the LLM
    endpoint is down or unconfigured.
    """
    conversation = await _require_conversation(case_id, conversation_id, user)
    store = get_store()
    messages = await store.list_agent_messages(conversation_id)
    proposals = await store.list_agent_proposals(conversation_id)
    payload = {
        "export_version": 1,
        "exported_at": datetime.now(UTC).isoformat(),
        "exported_by": user.username,
        "vestigo_version": __version__,
        "conversation": conversation.to_dict(),
        "messages": [m.to_dict() for m in messages],
        "proposals": [p.to_dict() for p in proposals],
        "raw_history": conversation.history or [],
    }
    await store.record_audit(
        action="agent.conversation_export",
        actor=user,
        case_id=case_id,
        target_type="agent_conversation",
        target_id=conversation_id,
        detail={"message_count": len(messages)},
    )
    return Response(
        content=json.dumps(payload, indent=2, default=str),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="agent-conversation-{conversation_id}.json"'
        },
    )


@router.patch("/{case_id}/agent/conversations/{conversation_id}")
async def update_conversation(
    case_id: str,
    conversation_id: str,
    payload: UpdateConversationRequest,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Narrow or widen an existing conversation's tool set.

    The turn reads ``conversation.disabled_tools`` fresh on every send, so a
    change here takes effect from the next turn onward — it never rewrites
    what earlier turns were allowed to do. Audited for exactly that reason:
    the conversation row only carries the *current* restriction, so who
    changed the agent's reach, and when, has to live in the audit trail for
    the record to stay readable after the fact.
    """
    await _require_agent()
    conversation = await _require_conversation(case_id, conversation_id, user)
    store = get_store()
    if payload.disabled_tools is None:
        return _conversation_payload(conversation)
    before = sorted(conversation.disabled_tools or ())
    after = sorted(payload.disabled_tools)
    if before != after:
        await store.update_agent_conversation(conversation_id, disabled_tools=after)
        await store.record_audit(
            action="agent.conversation_tools_changed",
            actor=user,
            case_id=case_id,
            target_type="agent_conversation",
            target_id=conversation_id,
            detail={"disabled_tools_before": before, "disabled_tools_after": after},
        )
    updated = await store.get_agent_conversation(case_id, conversation_id)
    return _conversation_payload(updated or conversation)


@router.post("/{case_id}/agent/conversations/{conversation_id}/cancel")
async def cancel_turn(
    case_id: str,
    conversation_id: str,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> dict[str, Any]:
    """Stop the turn currently streaming for this conversation, if any.

    Idempotent: cancelling an idle conversation is a no-op, not an error —
    the client may well be racing the turn's own completion. Signals the
    generator rather than killing the task so the partial turn still lands in
    the record (see ``_message_stream_inner``).

    Audited: a stop truncates the record, so who did it has to be recoverable
    afterwards — the messages alone only show that the turn ended early.
    """
    await _require_conversation(case_id, conversation_id, user)
    if not turn_is_active(conversation_id):
        return {"cancelled": False}
    _active_turns[conversation_id].cancel.set()
    await get_store().record_audit(
        action="agent.turn_cancelled",
        actor=user,
        case_id=case_id,
        target_type="agent_conversation",
        target_id=conversation_id,
    )
    return {"cancelled": True}


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


# Known overflow phrasings: OpenAI-protocol "maximum context length is N
# tokens" / code "context_length_exceeded"; Anthropic "prompt is too long" /
# "input is too long"; generic "context window" / "token limit" variants.
# Deliberately NOT bare "token"/"maximum"/"length": those also appear in
# unrelated 400s ("invalid token", "max_tokens must be ...") and a false
# positive here burns a summarizer LLM call and surfaces a misleading
# "start a new conversation" error.
_CONTEXT_OVERFLOW_RE = re.compile(
    r"context[ _-]?(?:length|window|limit)"
    r"|maximum context"
    r"|prompt is too long"
    r"|input is too long"
    r"|too many tokens"
    r"|token (?:count )?limit",
    re.IGNORECASE,
)


def _is_context_overflow(exc: ModelHTTPError) -> bool:
    """Best-effort detection of a context-window overflow across providers.

    There is no standard error code — this is a heuristic: a 400/413 whose
    body matches a known overflow phrasing. False negatives just surface the
    generic model_error message.
    """
    return exc.status_code in (400, 413) and bool(_CONTEXT_OVERFLOW_RE.search(str(exc.body or "")))


@dataclass(frozen=True)
class _ActiveTurn:
    """A reserved in-flight turn: its cancel signal and when it started."""

    cancel: asyncio.Event
    started: float


# Conversations with a turn currently streaming, mapped to that turn's cancel
# signal. Two concurrent turns on one conversation would race on `history`
# (last writer wins, the other turn's messages vanish from the replayable
# record), so send_message 409s instead.
#
# The cancel event is what makes Stop honest. A client aborting its SSE fetch
# only drops its own connection: with no output flowing (a long tool call, a
# slow model), Starlette may not notice the disconnect for a while, and the
# turn keeps running and spending tokens. `cancel_turn` sets this event and
# `_message_stream_inner` checks it as it streams — so Stop works from any
# client, including one that navigated away and came back.
#
# In-memory on purpose — same single-process deployment premise as JobStore.
_active_turns: dict[str, _ActiveTurn] = {}

# Ceiling on how long a reservation is believed. `send_message` reserves before
# returning the StreamingResponse, so if the ASGI task is cancelled before the
# generator's first step the entry's release (the generator's `finally`) never
# runs — leaving the conversation permanently "active": a Stop button that does
# nothing and a 409 on every send, unrecoverable without a restart. Past this
# age the entry is dropped and the conversation is treated as idle.
#
# The tradeoff is deliberate: a turn that genuinely runs longer than this is
# reported idle, and a concurrent turn then becomes possible (with the `history`
# race that the reservation exists to prevent). The bound is the worst case a
# turn can legitimately take — every model request timing out at `LLM_TIMEOUT`,
# `max_turns` times over — so exceeding it means something is already wrong.
_TURN_STALE_AFTER = LLM_TIMEOUT * DEFAULT_MAX_TURNS


def turn_is_active(conversation_id: str) -> bool:
    """Whether a turn is currently streaming for this conversation.

    Prunes a stranded reservation as a side effect, so this is the single
    gate every caller (the 409 check, `cancel_turn`, the `active` payload
    flag) goes through — otherwise they could disagree about the same entry.
    """
    turn = _active_turns.get(conversation_id)
    if turn is None:
        return False
    if monotonic() - turn.started > _TURN_STALE_AFTER:
        logger.warning(
            "Dropping stranded turn reservation for conversation %s (age > %.0fs)",
            conversation_id,
            _TURN_STALE_AFTER,
        )
        _active_turns.pop(conversation_id, None)
        return False
    return True


async def _message_stream(
    case_id: str,
    conversation: AgentConversation,
    payload: SendMessageRequest,
    user: User,
) -> AsyncGenerator[str]:
    """Release the turn reservation once the turn ends, however it ends.

    The cancel check lives in `_message_stream_inner`, not here: breaking out
    of the loop from outside would close the inner generator with a
    `GeneratorExit`, which — deriving from `BaseException` — no `except
    Exception` catches, silently dropping the streamed text instead of
    persisting it.
    """
    try:
        async for chunk in _message_stream_inner(case_id, conversation, payload, user):
            yield chunk
    finally:
        _active_turns.pop(conversation.id, None)


async def _message_stream_inner(
    case_id: str,
    conversation: AgentConversation,
    payload: SendMessageRequest,
    user: User,
) -> AsyncGenerator[str]:
    store = get_store()
    conversation_id = conversation.id
    # The reservation `send_message` made for this turn. Checked as the turn
    # streams so a stop can persist what ran and return normally — see the
    # `_cancelled` helper below.
    reservation = _active_turns.get(conversation_id)
    await store.add_agent_message(conversation_id, "user", payload.content)
    if not conversation.title:
        await store.update_agent_conversation(conversation_id, title=payload.content[:_TITLE_MAX])

    config = await resolve_agent_config()
    # Admin hard-deny ∪ the restriction frozen on this conversation.
    disabled_tools = frozenset(config.disabled_tools or ()) | frozenset(
        conversation.disabled_tools or ()
    )
    scope = await build_scope(
        case_id,
        conversation.timeline_id,
        user,
        conversation_id=conversation.id,
        disabled_tools=disabled_tools,
    )
    history = load_history(conversation.history)
    last_prompt, last_completion = await store.get_last_agent_usage(conversation_id)

    async def _run_compaction(
        current: list[Any], reason: str, keep_turns: int
    ) -> tuple[list[Any], dict[str, Any]] | None:
        """Compact, persist the forensic record, return (new_history, sse_event).

        None means compaction wasn't possible (nothing old enough to fold,
        or the summarizer call itself failed) — the caller falls through to
        its error path.
        """
        try:
            outcome = await compact_history(config, current, keep_turns=keep_turns)
        except Exception:
            logger.exception("History compaction failed (conversation %s)", conversation_id)
            return None
        if outcome is None:
            return None
        estimated = estimate_next_prompt_tokens(
            last_prompt, last_completion, current, payload.content
        )
        # The append-only row keeps the summary AND the exact pre-compaction
        # wire blob: the original full context stays reconstructible (and
        # exportable) even though future turns replay the compacted history.
        await store.add_agent_message(
            conversation_id,
            "compaction",
            outcome.summary,
            tool_result={
                "reason": reason,
                "keep_turns": keep_turns,
                "messages_summarized": outcome.messages_summarized,
                "estimated_tokens_before": estimated,
                "pre_compaction_history": dump_history(current),
            },
        )
        await store.update_agent_conversation(
            conversation_id, history=dump_history(outcome.new_history)
        )
        await store.record_audit(
            action="agent.compaction",
            actor=user,
            case_id=case_id,
            target_type="agent_conversation",
            target_id=conversation_id,
            detail={"reason": reason, "messages_summarized": outcome.messages_summarized},
        )
        return outcome.new_history, {
            "type": "compaction",
            "summary": outcome.summary,
            "reason": reason,
        }

    # Escalation schedule: the first compaction keeps 2 recent turns
    # verbatim; if the model still overflows, a second folds down to 1; a
    # third overflow gives up with the friendly context_overflow error.
    keep_schedule = (2, 1)
    compactions = 0
    estimated = estimate_next_prompt_tokens(last_prompt, last_completion, history, payload.content)
    if should_compact(config, estimated):
        compaction = await _run_compaction(history, "threshold", keep_schedule[0])
        if compaction is not None:
            history, compaction_event = compaction
            compactions = 1
            yield _sse(compaction_event)

    def _cancelled() -> bool:
        return reservation is not None and reservation.cancel.is_set()

    text_parts: list[str] = []
    for attempt in range(len(keep_schedule) + 1):
        text_parts = []
        if _cancelled():
            yield _sse({"type": "cancelled"})
            return
        try:
            async for event in stream_turn(
                scope,
                user_text=payload.content,
                history=history,
                view_filters=payload.view_filters,
            ):
                # A stop lands here, between streamed events — so the partial
                # turn is persisted the same way the interrupt paths below do
                # it, and the generator returns normally. Breaking out of this
                # from the *caller* would close this generator with a
                # `GeneratorExit`, which no `except Exception` catches, and the
                # streamed text would be lost.
                #
                # The bound: a stop takes effect at the next streamed event,
                # and always before the next model request. A tool call already
                # in flight still runs to completion first.
                if _cancelled():
                    if text_parts:
                        await store.add_agent_message(
                            conversation_id, "assistant", "".join(text_parts) + " [stopped]"
                        )
                    yield _sse({"type": "cancelled"})
                    return
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
                elif event["type"] == "thinking":
                    # One completed reasoning segment (thinking_delta events
                    # streamed it live; this is the durable record). Model
                    # prose, not a data access — no audit row.
                    await store.add_agent_message(conversation_id, "thinking", event["text"])
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
                    # Retried attempts re-execute tool calls; the attempt tag
                    # lets the custody trail distinguish the re-runs from the
                    # first pass instead of looking like duplicates.
                    audit_detail = {"tool": event["tool"], "args": event["args"]}
                    if attempt > 0:
                        audit_detail["attempt"] = attempt
                    await store.record_audit(
                        action="agent.tool_call",
                        actor=user,
                        case_id=case_id,
                        target_type="agent_conversation",
                        target_id=conversation_id,
                        detail=audit_detail,
                    )
                elif event["type"] == "tool_result":
                    await store.add_agent_message(
                        conversation_id,
                        "tool",
                        tool_name=event["tool"],
                        tool_result=event["result"],
                    )
                yield _sse(event)
            return
        except ModelHTTPError as exc:
            overflow = _is_context_overflow(exc)
            if overflow and compactions < len(keep_schedule):
                # The threshold estimate lagged behind (tool-heavy turn) —
                # compact and retry, escalating down the keep schedule on a
                # repeat overflow. Tool rows already persisted by this
                # attempt stay: the record shows what actually ran.
                compaction = await _run_compaction(history, "overflow", keep_schedule[compactions])
                if compaction is not None:
                    history, compaction_event = compaction
                    compactions += 1
                    yield _sse(compaction_event)
                    continue
            logger.exception("Agent turn failed (conversation %s)", conversation_id)
            if text_parts:
                await store.add_agent_message(
                    conversation_id, "assistant", "".join(text_parts) + " [interrupted]"
                )
            if overflow:
                detail = (
                    "The conversation no longer fits the model's context window "
                    "and could not be compacted further — start a new conversation."
                )
            else:
                detail = f"The model endpoint rejected the request (HTTP {exc.status_code}) — see server logs."
            yield _sse(
                {
                    "type": "error",
                    "code": "context_overflow" if overflow else "model_error",
                    "detail": detail,
                }
            )
            return
        except Exception:
            logger.exception("Agent turn failed (conversation %s)", conversation_id)
            # Persist whatever streamed before the failure so the record stays
            # truthful, then tell the client.
            if text_parts:
                await store.add_agent_message(
                    conversation_id, "assistant", "".join(text_parts) + " [interrupted]"
                )
            yield _sse({"type": "error", "detail": "Agent turn failed — see server logs."})
            return


@router.post("/{case_id}/agent/conversations/{conversation_id}/messages")
async def send_message(
    case_id: str,
    conversation_id: str,
    payload: SendMessageRequest,
    case: Case = Depends(require_case_read),
    user: User = Depends(get_current_user),
) -> StreamingResponse:
    """Send a user message and stream the agent's turn as SSE.

    One turn at a time per conversation: a second POST while a turn is
    streaming gets a 409 (see ``_active_turns``).
    """
    await _require_agent()
    conversation = await _require_conversation(case_id, conversation_id, user)
    if turn_is_active(conversation_id):
        raise HTTPException(
            status_code=409, detail="A turn is already running for this conversation"
        )
    # Reserve before returning the response — the generator's finally releases.
    _active_turns[conversation_id] = _ActiveTurn(cancel=asyncio.Event(), started=monotonic())
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
                        "created_by": user.id,
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


# ─────────────────────────────────────────────────────────────────────────────
# Agent info + per-user tool preferences (info_router, /api/agent)
# ─────────────────────────────────────────────────────────────────────────────


class PreferencesUpdate(BaseModel):
    disabled_tools: list[str] = Field(default_factory=list)

    _check_tools = field_validator("disabled_tools")(_validate_tool_names)


@info_router.get("/info")
async def agent_info(user: User = Depends(get_current_user)) -> dict[str, Any]:
    """Return the agent config any authenticated user may see, plus the tool catalog.

    This deliberately discloses the configured model and API base URL to
    non-admins: it powers the OPSEC notice shown before every conversation
    ("evidence is sent to X, processed by Y"). The API key is never included.
    """
    await _require_agent()
    config = await resolve_agent_config()
    admin_disabled = set(config.disabled_tools or ())
    prefs = user.preferences or {}
    user_disabled = [
        n for n in prefs.get("agent_disabled_tools", []) if isinstance(n, str) and n in TOOL_NAMES
    ]
    return {
        "model": config.model,
        "provider": config.provider,
        "api_base_url": config.api_base_url,
        "context_window": config.context_window,
        "compact_threshold": config.compact_threshold,
        "tools": [
            {
                "name": t.name,
                "description": t.description,
                "embeddings_gated": t.embeddings_gated,
                "requires_conversation": t.requires_conversation,
                "admin_disabled": t.name in admin_disabled,
                # Drives the tool-selector's "Core" preset (A13).
                "tier": t.tier,
            }
            for t in TOOL_REGISTRY
        ],
        "user_disabled_tools": user_disabled,
    }


@info_router.put("/preferences")
async def update_agent_preferences(
    payload: PreferencesUpdate, user: User = Depends(get_current_user)
) -> dict[str, Any]:
    """Persist the user's default tool selection for new conversations."""
    updated = await get_store().update_user_preferences(
        user.id, {"agent_disabled_tools": payload.disabled_tools}
    )
    if updated is None:
        raise HTTPException(status_code=404, detail="User not found")
    prefs = updated.preferences or {}
    return {"disabled_tools": prefs.get("agent_disabled_tools", [])}
