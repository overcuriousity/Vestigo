"""Repairing a mid-turn history snapshot into something replayable.

`AgentRun.new_messages()` mid-run can end with tool calls that have no
returns (pydantic-ai assembles the returns `ModelRequest` only after a whole
batch finishes) and, when a model stream dies, with a partial `ModelResponse`
whose trailing `ToolCallPart` carries half-written JSON arguments. Replaying
either shape breaks providers on the Anthropic protocol and violates the
pairing invariant `agent/window.py` relies on.
"""

from __future__ import annotations

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from vestigo.agent.resume import INTERRUPTED_RESULT, repair_partial


def _call(tool_call_id: str, name: str = "search_events") -> ToolCallPart:
    return ToolCallPart(tool_name=name, args={"q": "x"}, tool_call_id=tool_call_id)


def _returns(messages) -> dict[str, object]:
    return {
        part.tool_call_id: part.content
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }


def _unpaired(messages) -> set[str]:
    calls = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    }
    answered = {
        part.tool_call_id
        for message in messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, (ToolReturnPart, RetryPromptPart))
    }
    return calls - answered


def test_empty_snapshot_is_returned_unchanged():
    assert repair_partial([], called_ids=set(), results={}) == []


def test_complete_snapshot_is_returned_unchanged():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="search_events", content={"n": 1}, tool_call_id="a")]
        ),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    repaired = repair_partial(messages, called_ids={"a"}, results={"a": {"n": 1}})
    assert repaired == messages


def test_unpaired_call_gets_its_streamed_result_back():
    """The tool ran and its output reached the analyst — keep it, don't
    discard work the model already paid for."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a"), _call("b")]),
    ]
    repaired = repair_partial(
        messages, called_ids={"a", "b"}, results={"a": {"hits": 3}, "b": {"hits": 4}}
    )
    assert _unpaired(repaired) == set()
    assert _returns(repaired) == {"a": {"hits": 3}, "b": {"hits": 4}}


def test_call_that_never_returned_gets_the_interrupted_marker():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a"), _call("b")]),
    ]
    repaired = repair_partial(messages, called_ids={"a", "b"}, results={"a": {"hits": 3}})
    assert _returns(repaired) == {"a": {"hits": 3}, "b": INTERRUPTED_RESULT}


def test_a_retry_prompt_counts_as_answering_a_call():
    """A rejected tool call is answered by a RetryPromptPart, not a return —
    synthesizing a second answer for it would duplicate the pairing."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
        ModelRequest(
            parts=[RetryPromptPart(content="bad args", tool_name="search_events", tool_call_id="a")]
        ),
    ]
    repaired = repair_partial(messages, called_ids={"a"}, results={})
    assert repaired == messages


def test_tool_call_that_never_executed_is_dropped():
    """A model stream that dies mid-response leaves a ToolCallPart whose JSON
    arguments may be truncated. It never reached the tool executor, so no
    FunctionToolCallEvent was seen for it — drop it rather than replay it."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[TextPart(content="let me look"), _call("ghost")]),
    ]
    repaired = repair_partial(messages, called_ids=set(), results={})
    assert _unpaired(repaired) == set()
    assert repaired[-1].parts == [TextPart(content="let me look")]


def test_response_left_with_no_parts_is_dropped_entirely():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("ghost")]),
    ]
    repaired = repair_partial(messages, called_ids=set(), results={})
    assert len(repaired) == 1
    assert isinstance(repaired[0], ModelRequest)


def test_only_the_trailing_response_is_pruned():
    """An earlier response's calls did execute — a missing call id there means
    our bookkeeping lost it, not that the call is a phantom. Pruning it would
    silently delete real investigation steps."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("old")]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="search_events", content={"n": 1}, tool_call_id="old")]
        ),
        ModelResponse(parts=[_call("ghost")]),
    ]
    repaired = repair_partial(messages, called_ids=set(), results={})
    assert any(
        isinstance(p, ToolCallPart) and p.tool_call_id == "old"
        for m in repaired
        if isinstance(m, ModelResponse)
        for p in m.parts
    )


def test_repair_is_idempotent():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
    ]
    once = repair_partial(messages, called_ids={"a"}, results={"a": {"hits": 3}})
    twice = repair_partial(once, called_ids={"a"}, results={"a": {"hits": 3}})
    assert once == twice


def test_input_is_not_mutated():
    response = ModelResponse(parts=[_call("ghost")])
    messages = [ModelRequest(parts=[UserPromptPart(content="find it")]), response]
    repair_partial(messages, called_ids=set(), results={})
    assert len(messages) == 2
    assert len(response.parts) == 1


def test_a_repaired_snapshot_survives_the_sliding_window():
    """The window picks turn boundaries assuming tool_call/tool_result pairing
    holds. A resumed history that broke it would take the window down with it."""
    from vestigo.agent.window import apply_window

    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="search_events", content={"n": "x" * 5000}, tool_call_id="a"
                )
            ]
        ),
        ModelResponse(parts=[_call("b"), _call("c")]),
    ]
    repaired = repair_partial(
        messages, called_ids={"a", "b", "c"}, results={"b": {"hits": "y" * 5000}}
    )
    windowed, _stats = apply_window(repaired, budget=200)
    assert _unpaired(windowed) == set()
