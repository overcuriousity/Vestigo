"""Repairing a mid-turn history snapshot into something replayable.

`AgentRun.new_messages()` mid-run can end with tool calls that have no
returns (pydantic-ai assembles the returns `ModelRequest` only after a whole
batch finishes) and, when a model stream dies, with a partial `ModelResponse`
whose trailing `ToolCallPart` carries half-written JSON arguments. Replaying
either shape breaks providers on the Anthropic protocol and violates the
pairing invariant `agent/window.py` relies on.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
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


# ---------------------------------------------------------------------------
# stream_turn checkpointing
# ---------------------------------------------------------------------------

from vestigo.agent.tools import AgentScope  # noqa: E402


def _scope() -> AgentScope:
    return AgentScope(
        case_id="c1",
        timeline_id="t1",
        user=None,  # unused by the stubbed tool server
        source_ids=["s1"],
        field_mappings=None,
        source_offsets=None,
    )


def _ping_stub():
    """A minimal FastMCP server with one echo tool, shared by the tests below."""
    from mcp.server.fastmcp import FastMCP

    stub = FastMCP("stub")

    @stub.tool()
    async def ping(word: str) -> dict:
        """Echo."""
        return {"echo": word}

    return stub


@pytest.mark.asyncio
async def test_stream_turn_checkpoints_after_each_tool_result(monkeypatch):
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: _ping_stub())

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        last = messages[-1]
        if any(getattr(p, "part_kind", "") == "tool-return" for p in last.parts):
            yield "done"
        else:
            yield {0: DeltaToolCall(name="ping", json_args='{"word": "hi"}')}

    recorder = runtime.TurnRecorder()
    seen: list[dict] = []
    async for event in runtime.stream_turn(
        _scope(),
        user_text="ping please",
        history=[],
        recorder=recorder,
        model=FunctionModel(stream_function=model_stream),
    ):
        seen.append(event)
        # The snapshot is replay-safe at every single checkpoint, not just the
        # last one — a hard kill lands on an arbitrary checkpoint.
        if event["type"] == "checkpoint":
            assert _unpaired(recorder.messages) == set()

    types = [e["type"] for e in seen]
    assert "checkpoint" in types
    assert types.index("checkpoint") > types.index("tool_result")
    assert recorder.revision >= 2
    assert recorder.messages, "the recorder must hold the turn's messages"


@pytest.mark.asyncio
async def test_stream_turn_still_maps_events_and_returns_a_result(monkeypatch):
    """The rewrite onto agent.iter must not change the event contract the
    router and the frontend consume."""
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: _ping_stub())

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        last = messages[-1]
        if any(getattr(p, "part_kind", "") == "tool-return" for p in last.parts):
            yield "the echo "
            yield "came back"
        else:
            yield {0: DeltaToolCall(name="ping", json_args='{"word": "hi"}')}

    seen = []
    async for event in runtime.stream_turn(
        _scope(),
        user_text="ping please",
        history=[],
        view_filters={"q": "ssh"},
        model=FunctionModel(stream_function=model_stream),
    ):
        seen.append(event)

    types = [e["type"] for e in seen]
    assert types[-1] == "result"
    assert "tool_call" in types and "tool_result" in types
    streamed = "".join(e["text"] for e in seen if e["type"] == "text_delta")
    assert streamed == "the echo came back"
    turn = seen[-1]["turn"]
    assert turn.output_text == "the echo came back"
    assert turn.new_messages


@pytest.mark.asyncio
async def test_recorder_survives_a_turn_that_dies_mid_stream(monkeypatch):
    """The reported failure: the model endpoint dies partway through. The
    recorder must still hold a replayable snapshot of what ran."""
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: _ping_stub())

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        last = messages[-1]
        if any(getattr(p, "part_kind", "") == "tool-return" for p in last.parts):
            raise RuntimeError("endpoint died")
        yield {0: DeltaToolCall(name="ping", json_args='{"word": "hi"}')}

    recorder = runtime.TurnRecorder()
    with pytest.raises(RuntimeError):
        async for _event in runtime.stream_turn(
            _scope(),
            user_text="ping please",
            history=[],
            recorder=recorder,
            model=FunctionModel(stream_function=model_stream),
        ):
            pass

    assert recorder.messages, "the tool call and its result must survive"
    assert _unpaired(recorder.messages) == set()
    assert _returns(recorder.messages)  # the echo result is on the record


@pytest.mark.asyncio
async def test_a_checkpointed_snapshot_replays_as_history(monkeypatch):
    """End to end: dump the interrupted snapshot the way the router does, load
    it back, and run a second turn on top of it."""
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: _ping_stub())

    async def dying(messages: list[ModelMessage], info: AgentInfo):
        last = messages[-1]
        if any(getattr(p, "part_kind", "") == "tool-return" for p in last.parts):
            raise RuntimeError("endpoint died")
        yield {0: DeltaToolCall(name="ping", json_args='{"word": "hi"}')}

    recorder = runtime.TurnRecorder()
    with pytest.raises(RuntimeError):
        async for _event in runtime.stream_turn(
            _scope(),
            user_text="ping please",
            history=[],
            recorder=recorder,
            model=FunctionModel(stream_function=dying),
        ):
            pass

    stored = runtime.dump_history(recorder.messages)
    resumed = runtime.load_history(stored)
    assert resumed, "the blob round-trips"

    seen_history: list[int] = []

    async def second(messages: list[ModelMessage], info: AgentInfo):
        seen_history.append(len(messages))
        yield "continuing"

    events = []
    async for event in runtime.stream_turn(
        _scope(),
        user_text="carry on",
        history=resumed,
        model=FunctionModel(stream_function=second),
    ):
        events.append(event)

    assert events[-1]["type"] == "result"
    # The second turn saw the first turn's work, not an empty slate.
    assert seen_history[0] > 1


# ---------------------------------------------------------------------------
# Persistence of the partial-history stamp
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_history_partial_at_is_set_and_cleared_independently(store):
    """`None` already means "no change" for every other field on this method,
    so clearing the stamp needs its own sentinel — otherwise a turn could
    never mark itself complete."""
    from datetime import UTC, datetime

    await store.init_schema()
    user = await store.create_user("u1", "analyst", is_admin=True)
    case = await store.create_case("c1", "Case 1", owner_id=user.id)
    timeline = await store.create_timeline(case.id, "tl1", "Timeline 1", source_ids=[])
    conversation = await store.create_agent_conversation(
        case.id, timeline.id, user.id, model_id="stub:stub"
    )

    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history_partial_at is None

    stamp = datetime.now(UTC)
    await store.update_agent_conversation(
        conversation.id, history=[{"x": 1}], history_partial_at=stamp
    )
    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history_partial_at is not None
    assert fresh.history == [{"x": 1}]

    # A title-only update must not disturb the stamp.
    await store.update_agent_conversation(conversation.id, title="renamed")
    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history_partial_at is not None

    await store.update_agent_conversation(conversation.id, history_partial_at=None)
    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history_partial_at is None


# ---------------------------------------------------------------------------
# Router: every exit path keeps the turn's history
# ---------------------------------------------------------------------------


async def _conversation(store):
    await store.init_schema()
    user = await store.create_user("u1", "analyst", is_admin=True)
    case = await store.create_case("c1", "Case 1", owner_id=user.id)
    timeline = await store.create_timeline(case.id, "tl1", "Timeline 1", source_ids=[])
    conversation = await store.create_agent_conversation(
        case.id, timeline.id, user.id, model_id="stub:stub"
    )
    return user, case, conversation


def _fake_turn_messages():
    return [
        ModelRequest(parts=[UserPromptPart(content="look into this")]),
        ModelResponse(parts=[TextPart(content="partial answer")]),
    ]


@pytest.mark.asyncio
async def test_a_stopped_turn_keeps_its_history_and_is_marked_partial(store, monkeypatch):
    """The reported bug: an interrupted turn used to persist message rows and
    nothing replayable, so the next turn started from zero."""
    import asyncio
    import json
    from time import monotonic

    from vestigo.api.routers import agent as agent_router

    user, case, conversation = await _conversation(store)
    # Fake the reservation `send_message` makes, as tests/test_agent_api.py does.
    turn = agent_router._ActiveTurn(cancel=asyncio.Event(), started=monotonic())
    agent_router._active_turns[conversation.id] = turn

    async def fake_stream_turn(scope, *, user_text, history, recorder=None, **kwargs):
        yield {"type": "text_delta", "text": "partial answer"}
        recorder.messages = _fake_turn_messages()
        recorder.revision += 1
        yield {"type": "checkpoint"}
        turn.cancel.set()
        yield {"type": "text_delta", "text": "never streamed"}

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)
    payload = agent_router.SendMessageRequest(content="look into this")
    chunks = [
        chunk async for chunk in agent_router._message_stream(case.id, conversation, payload, user)
    ]
    events = [json.loads(c.removeprefix("data: ").strip()) for c in chunks]

    assert events[-1] == {"type": "cancelled"}
    # No checkpoint leaks to the client — it is a router-internal signal.
    assert all(e["type"] != "checkpoint" for e in events)

    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history, "the stopped turn's history must survive"
    assert fresh.history_partial_at is not None


@pytest.mark.asyncio
async def test_a_failed_turn_keeps_its_history_and_is_marked_partial(store, monkeypatch):
    from vestigo.api.routers import agent as agent_router

    user, case, conversation = await _conversation(store)

    async def fake_stream_turn(scope, *, user_text, history, recorder=None, **kwargs):
        yield {"type": "text_delta", "text": "partial answer"}
        recorder.messages = _fake_turn_messages()
        recorder.revision += 1
        yield {"type": "checkpoint"}
        raise RuntimeError("endpoint died")

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)
    payload = agent_router.SendMessageRequest(content="look into this")
    async for _chunk in agent_router._message_stream(case.id, conversation, payload, user):
        pass

    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history
    assert fresh.history_partial_at is not None


@pytest.mark.asyncio
async def test_a_completed_turn_clears_the_partial_mark(store, monkeypatch):
    from vestigo.agent.runtime import TurnResult
    from vestigo.api.routers import agent as agent_router

    user, case, conversation = await _conversation(store)
    await store.update_agent_conversation(conversation.id, history_partial_at=datetime.now(UTC))

    async def fake_stream_turn(scope, *, user_text, history, recorder=None, **kwargs):
        yield {"type": "text_delta", "text": "done"}
        yield {
            "type": "result",
            "turn": TurnResult(output_text="done", new_messages=_fake_turn_messages()),
        }

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)
    payload = agent_router.SendMessageRequest(content="look into this")
    async for _chunk in agent_router._message_stream(case.id, conversation, payload, user):
        pass

    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history_partial_at is None


@pytest.mark.asyncio
async def test_a_partial_conversation_resumes_with_the_note(store, monkeypatch):
    from vestigo.agent.resume import RESUME_NOTE
    from vestigo.agent.runtime import TurnResult
    from vestigo.api.routers import agent as agent_router

    user, case, conversation = await _conversation(store)
    await store.update_agent_conversation(conversation.id, history_partial_at=datetime.now(UTC))
    conversation = await store.get_agent_conversation(case.id, conversation.id)

    seen: dict[str, object] = {}

    async def fake_stream_turn(scope, *, user_text, history, recorder=None, **kwargs):
        seen["resume_note"] = kwargs.get("resume_note")
        yield {
            "type": "result",
            "turn": TurnResult(output_text="ok", new_messages=_fake_turn_messages()),
        }

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)
    payload = agent_router.SendMessageRequest(content="carry on")
    async for _chunk in agent_router._message_stream(case.id, conversation, payload, user):
        pass

    assert seen["resume_note"] == RESUME_NOTE


@pytest.mark.asyncio
async def test_the_overflow_rerun_does_not_concatenate_the_failed_attempt(store, monkeypatch):
    """Attempt 1 replays from the same `history` base, so the recorder must be
    reset per attempt — otherwise the persisted blob carries attempt 0's
    messages ahead of the re-run's and the record stops being faithful."""
    from pydantic_ai.exceptions import ModelHTTPError

    from vestigo.agent.runtime import TurnResult
    from vestigo.api.routers import agent as agent_router

    user, case, conversation = await _conversation(store)
    attempts: list[int] = []

    async def fake_stream_turn(scope, *, user_text, history, recorder=None, **kwargs):
        attempts.append(scope.attempt)
        if scope.attempt == 0:
            recorder.messages = _fake_turn_messages()
            recorder.revision += 1
            yield {"type": "checkpoint"}
            raise ModelHTTPError(
                status_code=400,
                model_name="stub",
                body={"error": {"message": "maximum context length is 8192 tokens"}},
            )
        # The re-run must start from an empty recorder.
        assert recorder.messages == []
        assert recorder.revision == 0
        yield {
            "type": "result",
            "turn": TurnResult(output_text="ok", new_messages=_fake_turn_messages()),
        }

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)
    payload = agent_router.SendMessageRequest(content="look into this")
    async for _chunk in agent_router._message_stream(case.id, conversation, payload, user):
        pass

    assert attempts == [0, 1]
    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history_partial_at is None
    assert len(fresh.history) == 2
