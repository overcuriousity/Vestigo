"""History auto-compaction: keep long conversations inside the context window.

The runtime replays the full pydantic-ai history every turn, so a long
investigation eventually overflows the model's context window and the
provider answers 400. When the operator configures ``context_window`` (and
optionally ``compact_threshold``), the router calls :func:`should_compact`
before each turn — and retries after a detected overflow, escalating from
keeping 2 recent turns down to 1 before giving up — replacing the older
turns with an LLM-written summary via :func:`compact_history`.

Design constraints:

- **Forensic trail**: compaction never destroys the record. The router
  persists a ``role="compaction"`` message row carrying both the summary and
  the exact pre-compaction history blob, so the original context remains
  reconstructible (and exportable) even though follow-up turns run on the
  compacted history.
- **Provider portability**: the compacted history is an ordinary
  user/assistant message pair followed by the kept tail — no provider-native
  compaction parts — so it replays unchanged against OpenAI-protocol,
  Anthropic-protocol, and Kimi endpoints. The stub is a *pair* (not a lone
  user message) so strict user/assistant alternation survives, and the split
  only happens at user-turn boundaries so tool_use/tool_result pairs are
  never orphaned.
- **Token math is an estimate**: the only measured number is last turn's
  usage; tool output sizes are unpredictable, so the threshold check lags a
  turn behind. The overflow-retry path in the router is the designed
  backstop, not an edge case.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass

import httpx
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model

from vestigo.agent.config import DEFAULT_COMPACT_THRESHOLD, AgentConfig

logger = logging.getLogger(__name__)

# How many trailing user turns survive compaction verbatim. Two keeps the
# immediate working context (the current line of questioning plus one back)
# while everything older is summarized.
KEEP_RECENT_TURNS = 2

# Prefix of the stub user message that carries the summary into the replayed
# history — also what an analyst sees when inspecting raw_history in exports.
COMPACTION_MARKER = "[Conversation compacted]"

_RENDER_TRUNCATE = 500
_LLM_TIMEOUT = 300.0

SUMMARY_SYSTEM_PROMPT = """\
You compress a forensic log-investigation conversation to free context space.
Preserve, densely and factually: the analyst's goals and instructions; every
finding reached so far with its exact event_ids, counts, field values and
filter specs; hypotheses still open; approaches already tried that failed
(so they are not repeated). Never invent data; omit pleasantries. Output
plain prose/bullets, no preamble."""


@dataclass
class CompactionOutcome:
    """Result of one successful compaction."""

    new_history: list[ModelMessage]
    summary: str
    messages_summarized: int


def estimate_next_prompt_tokens(
    last_prompt_tokens: int | None,
    last_completion_tokens: int | None,
    history: list[ModelMessage],
    user_text: str,
) -> int:
    """Estimate the prompt size of the next turn, in tokens.

    Measured path: the previous turn's prompt plus its completion is a floor
    for the next prompt (the completion becomes history). Fallback when the
    endpoint never reported usage: chars/4 over the serialized history.
    """
    from vestigo.agent.runtime import dump_history

    user_estimate = len(user_text) // 4 + 1
    if last_prompt_tokens:
        return last_prompt_tokens + (last_completion_tokens or 0) + user_estimate
    if not history:
        return user_estimate
    return len(json.dumps(dump_history(history), default=str)) // 4 + user_estimate


def should_compact(config: AgentConfig, estimated_tokens: int) -> bool:
    """Whether the estimated next prompt crosses the compaction threshold."""
    if not config.context_window:
        return False
    threshold = config.compact_threshold or DEFAULT_COMPACT_THRESHOLD
    return estimated_tokens >= threshold * config.context_window


def _user_turn_boundaries(history: list[ModelMessage]) -> list[int]:
    """Indices of requests that start a user turn (contain a UserPromptPart).

    Requests that only carry tool returns are *not* boundaries — splitting
    there would orphan a tool_use from its tool_result on replay.
    """
    return [
        i
        for i, message in enumerate(history)
        if isinstance(message, ModelRequest)
        and any(isinstance(part, UserPromptPart) for part in message.parts)
    ]


def split_history(
    history: list[ModelMessage], keep_turns: int = KEEP_RECENT_TURNS
) -> tuple[list[ModelMessage], list[ModelMessage]] | None:
    """Split history into (head to summarize, tail to keep verbatim).

    Returns None when there are not more than ``keep_turns`` user turns —
    nothing old enough to fold away, so compaction cannot help.
    """
    boundaries = _user_turn_boundaries(history)
    if len(boundaries) <= keep_turns:
        return None
    cut = boundaries[-keep_turns]
    return list(history[:cut]), list(history[cut:])


def _truncate(text: str, limit: int = _RENDER_TRUNCATE) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def render_for_summary(head: list[ModelMessage]) -> str:
    """Flatten the head into readable lines for the summarizer model.

    Thinking parts are skipped (reasoning scratch, not investigation state);
    tool args/results are truncated — the summary needs the shape of what was
    tried, not full payloads.
    """
    lines: list[str] = []
    for message in head:
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    content = part.content if isinstance(part.content, str) else str(part.content)
                    lines.append(f"ANALYST: {_truncate(content, 2000)}")
                elif isinstance(part, ToolReturnPart):
                    lines.append(
                        f"RESULT {part.tool_name}: "
                        f"{_truncate(json.dumps(part.content, default=str))}"
                    )
        elif isinstance(message, ModelResponse):
            for part in message.parts:
                if isinstance(part, TextPart):
                    lines.append(f"AGENT: {_truncate(part.content, 2000)}")
                elif isinstance(part, ToolCallPart):
                    lines.append(
                        f"TOOL {part.tool_name}({_truncate(json.dumps(part.args, default=str))})"
                    )
    return "\n".join(lines)


async def summarize(model: Model, head: list[ModelMessage]) -> str:
    """Run the toolset-less summarizer agent over the rendered head."""
    agent = Agent(model, system_prompt=SUMMARY_SYSTEM_PROMPT)
    result = await agent.run(
        "Summarize this investigation conversation so far:\n\n" + render_for_summary(head)
    )
    return str(result.output)


async def compact_history(
    config: AgentConfig,
    history: list[ModelMessage],
    *,
    keep_turns: int = KEEP_RECENT_TURNS,
    model: Model | None = None,
) -> CompactionOutcome | None:
    """Summarize older turns and return the compacted history, or None.

    None means there is nothing old enough to fold (see
    :func:`split_history`) — the caller degrades to a friendly error if it
    got here via a context overflow. ``keep_turns`` lets the router escalate
    (fold down to a single verbatim turn) when a first compaction still
    overflows. When ``model`` is not injected (tests inject one), the call
    builds and owns its own HTTP client, mirroring ``stream_turn``.
    """
    split = split_history(history, keep_turns=keep_turns)
    if split is None:
        return None
    head, tail = split

    http_client: httpx.AsyncClient | None = None
    if model is None:
        from vestigo.agent.availability import probe_headers
        from vestigo.agent.runtime import build_model

        http_client = httpx.AsyncClient(headers=probe_headers(config), timeout=_LLM_TIMEOUT)
        model = build_model(config, http_client)
    try:
        summary = await summarize(model, head)
    finally:
        if http_client is not None:
            await http_client.aclose()

    stub: list[ModelMessage] = [
        ModelRequest(
            parts=[
                UserPromptPart(
                    content=(
                        f"{COMPACTION_MARKER} Older turns were summarized to fit the "
                        f"context window. Summary of the earlier conversation:\n\n{summary}"
                    )
                )
            ]
        ),
        ModelResponse(
            parts=[
                TextPart(
                    content=(
                        "Understood. Continuing the investigation with that summary as context."
                    )
                )
            ]
        ),
    ]
    logger.info("Compacted agent history: %d messages summarized, %d kept", len(head), len(tail))
    return CompactionOutcome(
        new_history=stub + tail,
        summary=summary,
        messages_summarized=len(head),
    )
