"""Unit tests for the sliding context window (vestigo.agent.window).

Pure-logic tests: elision order, protected regions, turn-dropping, purity and
determinism. No LLM, no router.
"""

from __future__ import annotations

import copy

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from vestigo.agent.window import (
    MIN_KEEP_CHARS,
    TURN_DROP_MARKER,
    WindowStats,
    apply_window,
    budget_for,
    estimate_tokens,
    make_window_processor,
)

BIG = "x" * 4000  # ~1000 estimated tokens per tool result


def _user_turn(text: str, answer: str) -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content=text)]),
        ModelResponse(parts=[TextPart(content=answer)]),
    ]


def _tool_cycle(name: str, content, call_id: str) -> list:
    """One tool call + its return, as they interleave inside a turn."""
    return [
        ModelResponse(parts=[ToolCallPart(tool_name=name, args={}, tool_call_id=call_id)]),
        ModelRequest(parts=[ToolReturnPart(tool_name=name, content=content, tool_call_id=call_id)]),
    ]


def _big_turn(question: str, cycles: int, prefix: str) -> list:
    """A user turn with several bulky tool exchanges and a prose answer."""
    messages: list = [ModelRequest(parts=[UserPromptPart(content=question)])]
    for i in range(cycles):
        messages += _tool_cycle("search_events", {"data": BIG, "i": i}, f"{prefix}{i}")
    messages.append(ModelResponse(parts=[TextPart(content=f"answer to {question}")]))
    return messages


def _elided(part: ToolReturnPart) -> bool:
    return isinstance(part.content, dict) and part.content.get("elided") is True


def _truncated(part: ToolReturnPart) -> bool:
    return isinstance(part.content, dict) and part.content.get("truncated") is True


def _tool_returns(messages) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


# ---------------------------------------------------------------------------
# estimate / budget
# ---------------------------------------------------------------------------


def test_estimate_tokens_scales_with_content():
    small = estimate_tokens(_user_turn("q", "a"))
    large = estimate_tokens(_user_turn("q" * 4000, "a"))
    assert small > 0
    assert large >= small + 900


def test_budget_for_reserves_headroom_and_system_prompt():
    assert budget_for(10_000, "s" * 4000) == 8_000 - 1_000


def test_budget_for_clamps_to_floor_and_warns(caplog):
    """A system prompt that eats the whole margin must not yield a budget <= 0
    (which would silently maximally elide every request)."""
    import logging

    with caplog.at_level(logging.WARNING, logger="vestigo.agent.window"):
        assert budget_for(1024, "s" * 100_000) == 1
    assert "context_window" in caplog.text


# ---------------------------------------------------------------------------
# apply_window: pass 1 (elision)
# ---------------------------------------------------------------------------


def test_under_budget_is_untouched():
    history = _big_turn("q1", cycles=2, prefix="a")
    out, stats = apply_window(history, budget=10**9)
    assert out == history
    assert stats.results_elided == 0
    assert stats.turns_dropped == 0


def test_elides_oldest_tool_results_first():
    history = _big_turn("q1", cycles=4, prefix="a")
    # Room for roughly two big results: the two oldest go, the newest stays.
    out, stats = apply_window(history, budget=3_600)
    returns = _tool_returns(out)
    assert len(returns) == 4
    assert _elided(returns[0]) and _elided(returns[1])
    assert not _elided(returns[3])
    assert stats.results_elided == 2
    assert stats.estimated_after <= stats.budget


def test_tiny_results_are_not_stubbed():
    """A tool return smaller than the elision stub is left alone — replacing
    it would grow the prompt and count a no-op elision."""
    history: list = [ModelRequest(parts=[UserPromptPart(content="q1")])]
    history += _tool_cycle("get_event", "ok", "tiny0")  # smaller than the stub
    history += _tool_cycle("search_events", {"data": BIG}, "big0")
    history += _tool_cycle("search_events", {"data": BIG}, "big1")
    history.append(ModelResponse(parts=[TextPart(content="answer")]))
    out, stats = apply_window(history, budget=1_200)
    returns = _tool_returns(out)
    assert returns[0].content == "ok"
    assert _elided(returns[1])
    assert stats.results_elided == 1


def test_newest_request_cycle_is_protected():
    """Even a budget nothing can satisfy leaves the newest tool return intact —
    it is what the model is about to reason over."""
    history = _big_turn("q1", cycles=3, prefix="a")
    out, _ = apply_window(history, budget=1)
    returns = _tool_returns(out)
    assert _elided(returns[0]) and _elided(returns[1])
    assert not _elided(returns[-1])


def test_elision_stub_names_recovery_path():
    history = _big_turn("q1", cycles=2, prefix="a")
    out, _ = apply_window(history, budget=1_200)
    stub = _tool_returns(out)[0].content
    assert stub["elided"] is True
    assert "get_event" in stub["note"]


def test_assistant_prose_and_structure_survive_elision():
    history = _big_turn("q1", cycles=3, prefix="a")
    out, _ = apply_window(history, budget=1_500)
    assert [type(m).__name__ for m in out] == [type(m).__name__ for m in history]
    prose = [
        p.content
        for m in out
        if isinstance(m, ModelResponse)
        for p in m.parts
        if isinstance(p, TextPart)
    ]
    assert prose == ["answer to q1"]
    # tool_call_id pairing intact after elision.
    for call, ret in zip(
        [p for m in out for p in m.parts if isinstance(p, ToolCallPart)],
        _tool_returns(out),
        strict=True,
    ):
        assert call.tool_call_id == ret.tool_call_id


def test_apply_window_does_not_mutate_input():
    history = _big_turn("q1", cycles=3, prefix="a")
    snapshot = copy.deepcopy(history)
    apply_window(history, budget=1_200)
    assert history == snapshot


def test_apply_window_is_deterministic():
    history = _big_turn("q1", cycles=4, prefix="a") + _big_turn("q2", cycles=2, prefix="b")
    first = apply_window(history, budget=2_000)
    second = apply_window(history, budget=2_000)
    assert first == second


# ---------------------------------------------------------------------------
# apply_window: pass 2 (turn drop)
# ---------------------------------------------------------------------------


def test_drops_oldest_middle_turns_when_elision_is_not_enough():
    history = (
        _user_turn("first question with the case context", "ack")
        + _big_turn("q2", cycles=2, prefix="a")
        + _user_turn("q3" * 600, "a3" * 600)
        + _big_turn("q4", cycles=1, prefix="b")
    )
    out, stats = apply_window(history, budget=1_400)
    assert stats.turns_dropped >= 1
    # The first user request survives verbatim.
    assert out[0].parts[0].content == "first question with the case context"
    # A marker pair stands where the dropped turns were.
    assert isinstance(out[2], ModelRequest)
    assert TURN_DROP_MARKER in out[2].parts[0].content
    assert isinstance(out[3], ModelResponse)
    # The newest turn's question is still present.
    prompts = [
        p.content
        for m in out
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]
    assert prompts[-1] == "q4"


def test_turn_drop_never_orphans_tool_returns():
    history = (
        _user_turn("first", "1")
        + _big_turn("q2", cycles=2, prefix="a")
        + _big_turn("q3", cycles=2, prefix="b")
    )
    out, stats = apply_window(history, budget=1_400)
    assert stats.turns_dropped >= 1
    call_ids = [p.tool_call_id for m in out for p in m.parts if isinstance(p, ToolCallPart)]
    return_ids = [p.tool_call_id for p in _tool_returns(out)]
    assert call_ids == return_ids


def test_last_turn_is_never_dropped():
    history = _user_turn("first", "1") + _big_turn("only real turn", cycles=3, prefix="a")
    out, stats = apply_window(history, budget=1)
    assert stats.turns_dropped == 0
    prompts = [
        p.content
        for m in out
        if isinstance(m, ModelRequest)
        for p in m.parts
        if isinstance(p, UserPromptPart)
    ]
    assert "only real turn" in prompts


# ---------------------------------------------------------------------------
# apply_window: pass 3 (truncate the newest returns)
# ---------------------------------------------------------------------------


def test_truncates_newest_result_when_nothing_else_can_help():
    """One tool result bigger than the whole budget: elision protects it and
    turn dropping cannot reach inside it, so without truncation the turn
    overflows, retries identically, and dies."""
    history: list = [ModelRequest(parts=[UserPromptPart(content="q1")])]
    history += _tool_cycle("search_events", {"data": "y" * 40_000}, "huge0")
    out, stats = apply_window(history, budget=2_000)
    part = _tool_returns(out)[0]
    assert _truncated(part)
    assert not _elided(part)
    assert stats.results_truncated == 1
    assert stats.results_elided == 0
    assert "get_event" in part.content["note"]
    # The head is a real slice of the original, and the pairing survives.
    assert part.content["head"].startswith('{"data": "yyy')
    assert part.tool_call_id == "huge0"
    assert stats.estimated_after < stats.estimated_before


def test_truncation_keeps_a_floor_of_content():
    """Even an unsatisfiable budget leaves the model the shape of its result —
    a bare note reads the same as an empty result."""
    history: list = [ModelRequest(parts=[UserPromptPart(content="q1")])]
    history += _tool_cycle("search_events", {"data": "y" * 40_000}, "huge0")
    out, stats = apply_window(history, budget=1)
    head = _tool_returns(out)[0].content["head"]
    assert len(head) == MIN_KEEP_CHARS
    assert stats.results_truncated == 1


def test_truncation_only_fires_when_elision_is_not_enough():
    """A history elision alone can fit leaves the newest result byte-identical."""
    history = _big_turn("q1", cycles=4, prefix="a")
    out, stats = apply_window(history, budget=3_600)
    assert stats.results_truncated == 0
    assert not _truncated(_tool_returns(out)[-1])


def test_truncation_is_pure_and_deterministic():
    history: list = [ModelRequest(parts=[UserPromptPart(content="q1")])]
    history += _tool_cycle("search_events", {"data": "y" * 40_000}, "huge0")
    snapshot = copy.deepcopy(history)
    first = apply_window(history, budget=2_000)
    second = apply_window(history, budget=2_000)
    assert history == snapshot
    assert first == second


# ---------------------------------------------------------------------------
# processor factory
# ---------------------------------------------------------------------------


def test_processor_applies_window_and_keeps_the_largest_reduction():
    stats = WindowStats(budget=3_600)
    processor = make_window_processor(3_600, stats)
    history = _big_turn("q1", cycles=4, prefix="a")
    out = processor(history)
    assert stats.results_elided == 2
    assert _tool_returns(out)[0].content["elided"] is True
    # A later, smaller request must not replace the recorded reduction.
    processor(_big_turn("q1", cycles=1, prefix="z"))
    assert stats.results_elided == 2


def test_processor_stats_describe_one_real_request():
    """before/after must come from the same request — a field-wise maximum
    would report a reduction that never happened."""
    stats = WindowStats()
    processor = make_window_processor(3_600, stats)
    small = _big_turn("q1", cycles=4, prefix="a")
    large = _big_turn("q1", cycles=9, prefix="b")
    processor(large)
    processor(small)
    _, large_stats = apply_window(large, budget=3_600)
    assert stats.estimated_before == large_stats.estimated_before
    assert stats.estimated_after == large_stats.estimated_after
    assert stats.results_elided == large_stats.results_elided
    assert stats.budget == 3_600
