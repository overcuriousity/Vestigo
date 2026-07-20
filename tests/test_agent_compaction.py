"""Unit tests for agent history auto-compaction (vestigo.agent.compaction).

Pure-logic tests: turn-boundary splitting (never orphans a tool return),
threshold math, and the compacted-history shape produced with an injected
FunctionModel — no real LLM, no router.
"""

from __future__ import annotations

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from vestigo.agent.compaction import (
    COMPACTION_MARKER,
    CompactionOutcome,
    compact_history,
    estimate_next_prompt_tokens,
    render_for_summary,
    should_compact,
    split_history,
)
from vestigo.agent.config import AgentConfig


def _config(context_window: int | None = None, compact_threshold: float | None = None):
    return AgentConfig(
        model="m",
        provider="openai",
        api_base_url=None,
        api_key=None,
        user_agent=None,
        extra_headers=None,
        max_turns=15,
        reasoning_effort="off",
        context_window=context_window,
        compact_threshold=compact_threshold,
    )


def _user_turn(text: str, answer: str) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content=text)]),
        ModelResponse(parts=[TextPart(content=answer)]),
    ]


def _tool_turn(text: str) -> list:
    """A user turn whose answer involves a tool call + tool return."""
    return [
        ModelRequest(parts=[UserPromptPart(content=text)]),
        ModelResponse(parts=[ToolCallPart(tool_name="search_events", args={"limit": 5})]),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="search_events", content={"total": 3}, tool_call_id="x")
            ]
        ),
        ModelResponse(parts=[TextPart(content="found 3 events")]),
    ]


# ---------------------------------------------------------------------------
# should_compact / estimate
# ---------------------------------------------------------------------------


def test_should_compact_off_without_context_window():
    assert should_compact(_config(context_window=None), estimated_tokens=10**9) is False


def test_should_compact_threshold_math():
    config = _config(context_window=10_000, compact_threshold=0.8)
    assert should_compact(config, 7_999) is False
    assert should_compact(config, 8_000) is True


def test_should_compact_default_threshold():
    config = _config(context_window=10_000)
    assert should_compact(config, 8_499) is False
    assert should_compact(config, 8_500) is True


def test_estimate_prefers_measured_usage():
    history = _user_turn("q", "a")
    est = estimate_next_prompt_tokens(1000, 200, history, "next question")
    assert est == 1000 + 200 + len("next question") // 4 + 1


def test_estimate_falls_back_to_serialized_size():
    history = _user_turn("q" * 400, "a" * 400)
    est = estimate_next_prompt_tokens(None, None, history, "hi")
    # chars/4 over the serialized blob: at least the raw content size / 4.
    assert est > 800 // 4


# ---------------------------------------------------------------------------
# split_history
# ---------------------------------------------------------------------------


def test_split_returns_none_when_too_short():
    history = _user_turn("one", "1") + _user_turn("two", "2")
    assert split_history(history, keep_turns=2) is None


def test_split_keeps_recent_turns_verbatim():
    history = _user_turn("one", "1") + _user_turn("two", "2") + _user_turn("three", "3")
    head, tail = split_history(history, keep_turns=2)
    assert len(head) == 2 and len(tail) == 4
    assert head[0].parts[0].content == "one"
    assert tail[0].parts[0].content == "two"


def test_split_keep_one_turn_folds_everything_older():
    """The escalated retry (keep_turns=1) folds all but the last user turn —
    including a previous compaction's stub pair."""
    history = _user_turn("one", "1") + _user_turn("two", "2")
    head, tail = split_history(history, keep_turns=1)
    assert len(head) == 2 and len(tail) == 2
    assert tail[0].parts[0].content == "two"


def test_split_never_orphans_tool_returns():
    """A tool-return-only request is not a boundary — the cut lands on the
    user turn before it, keeping tool_use/tool_result adjacent in the tail."""
    history = _user_turn("one", "1") + _tool_turn("two") + _user_turn("three", "3")
    head, tail = split_history(history, keep_turns=2)
    assert head == history[:2]
    # Tail starts at the "two" user turn and carries the whole tool exchange.
    assert tail[0].parts[0].content == "two"
    kinds = [type(p).__name__ for m in tail for p in m.parts]
    assert "ToolCallPart" in kinds and "ToolReturnPart" in kinds


# ---------------------------------------------------------------------------
# render_for_summary / compact_history
# ---------------------------------------------------------------------------


def test_render_covers_all_part_kinds():
    rendered = render_for_summary(_tool_turn("who logged in?"))
    assert "ANALYST: who logged in?" in rendered
    assert "TOOL search_events" in rendered
    assert "RESULT search_events" in rendered
    assert "AGENT: found 3 events" in rendered


@pytest.mark.asyncio
async def test_compact_history_builds_alternating_stub_plus_tail():
    history = _user_turn("one", "1") + _user_turn("two", "2") + _user_turn("three", "3")

    async def summarizer(messages, info):
        return ModelResponse(parts=[TextPart(content="SUMMARY: the analyst asked one thing.")])

    outcome = await compact_history(
        _config(context_window=1024), history, model=FunctionModel(summarizer)
    )
    assert isinstance(outcome, CompactionOutcome)
    assert outcome.summary == "SUMMARY: the analyst asked one thing."
    assert outcome.messages_summarized == 2

    new = outcome.new_history
    # Stub pair: user message carrying the marker + summary, assistant ack —
    # strict user/assistant alternation for Anthropic-protocol replay.
    assert isinstance(new[0], ModelRequest)
    stub_text = new[0].parts[0].content
    assert stub_text.startswith(COMPACTION_MARKER)
    assert "SUMMARY" in stub_text
    assert isinstance(new[1], ModelResponse)
    # Tail preserved verbatim after the stub.
    assert new[2:] == history[2:]


@pytest.mark.asyncio
async def test_compact_history_none_when_nothing_to_fold():
    history = _user_turn("only", "turn")

    async def summarizer(messages, info):  # pragma: no cover - never called
        raise AssertionError("summarizer must not run")

    outcome = await compact_history(
        _config(context_window=1024), history, model=FunctionModel(summarizer)
    )
    assert outcome is None
