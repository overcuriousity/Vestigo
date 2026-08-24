"""Repairing a mid-turn history snapshot into something replayable.

`AgentRun.new_messages()` mid-run can end with tool calls that have no
returns (pydantic-ai assembles the returns `ModelRequest` only after a whole
batch finishes) and, when a model stream dies, with a partial `ModelResponse`
whose trailing `ToolCallPart` carries half-written JSON arguments. Replaying an
unpaired call breaks providers on the Anthropic protocol and violates the
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

from vestigo.agent.resume import INTERRUPTED_RESULT, RESUME_MARKER, repair_partial


def _call(tool_call_id: str, name: str = "search_events") -> ToolCallPart:
    return ToolCallPart(tool_name=name, args={"q": "x"}, tool_call_id=tool_call_id)


def _result(tool_call_id: str, content: object, name: str = "search_events") -> ToolReturnPart:
    """A tool answer as `stream_turn` records it — the part, not its content."""
    return ToolReturnPart(tool_name=name, content=content, tool_call_id=tool_call_id)


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
    assert repair_partial([], results={}) == []


def test_complete_snapshot_is_returned_unchanged():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
        ModelRequest(parts=[_result("a", {"n": 1})]),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    repaired = repair_partial(messages, results={"a": _result("a", {"n": 1})})
    assert repaired == messages


def test_unpaired_call_gets_its_streamed_result_back():
    """The tool ran and its output reached the analyst — keep it, don't
    discard work the model already paid for."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a"), _call("b")]),
    ]
    repaired = repair_partial(
        messages, results={"a": _result("a", {"hits": 3}), "b": _result("b", {"hits": 4})}
    )
    assert _unpaired(repaired) == set()
    assert _returns(repaired) == {"a": {"hits": 3}, "b": {"hits": 4}}


def test_call_that_never_returned_gets_the_interrupted_marker():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a"), _call("b")]),
    ]
    repaired = repair_partial(messages, results={"a": _result("a", {"hits": 3})})
    assert _returns(repaired) == {"a": {"hits": 3}, "b": INTERRUPTED_RESULT}
    # The prose note is for the model; `outcome` is for a machine reading an
    # export, which must be able to tell a synthesized answer from a real one.
    synthesized = {
        part.tool_call_id: part.outcome
        for message in repaired
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    }
    assert synthesized == {"a": "success", "b": "interrupted"}


def test_a_synthesized_request_is_marked_interrupted():
    """pydantic-ai reads `state` on a trailing request to decide that the
    preceding response's open calls will never be executed."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
    ]
    repaired = repair_partial(messages, results={})
    assert [m.state for m in repaired if isinstance(m, ModelRequest)] == [
        "complete",
        "interrupted",
    ]


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
    repaired = repair_partial(messages, results={})
    # No second answer synthesized; only the trailing marker response closing
    # the request/response pair (see test_a_repaired_snapshot_ends_in_a_response).
    assert repaired[:3] == messages
    assert _returns(repaired) == {}


def test_a_rejected_call_is_replayed_as_a_retry_not_a_return():
    """`FunctionToolResultEvent.part` is a RetryPromptPart when the call was
    rejected. Rebuilding it as a ToolReturnPart would replay a rejection as a
    successful result and put the retry's error text where a tool's output
    belongs."""
    retry = RetryPromptPart(content="bad args", tool_name="search_events", tool_call_id="a")
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
    ]
    repaired = repair_partial(messages, results={"a": retry})
    assert _unpaired(repaired) == set()
    assert _returns(repaired) == {}, "a rejection is not a return"
    assert any(
        part is retry
        for message in repaired
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


def test_a_truncated_tool_call_is_kept_and_answered():
    """A model stream that dies mid-response leaves a ToolCallPart whose JSON
    arguments may be truncated. Dropping it rewrites the shape of a response
    whose thinking signature was computed over the turn that included the call —
    and this blob is the only place those signatures live. Keep it and answer
    it, as pydantic-ai does for the same reason."""
    ghost = ToolCallPart(tool_name="search_events", args='{"q": "unf', tool_call_id="ghost")
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[TextPart(content="let me look"), ghost]),
    ]
    repaired = repair_partial(messages, results={})
    assert _unpaired(repaired) == set()
    assert repaired[1].parts == [TextPart(content="let me look"), ghost]
    assert _returns(repaired) == {"ghost": INTERRUPTED_RESULT}


def test_an_answer_sits_in_the_request_after_its_own_call():
    """Anthropic wants a tool_result in the request immediately following its
    tool_use, and no later normalization reorders across a response. Batching
    every synthesized answer onto the end of the snapshot would separate the
    first response's answers from its calls."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("early")]),
        ModelResponse(parts=[_call("late")]),
    ]
    repaired = repair_partial(messages, results={})
    shape = [
        (type(m).__name__, [getattr(p, "tool_call_id", None) for p in m.parts]) for m in repaired
    ]
    assert shape == [
        ("ModelRequest", [None]),
        ("ModelResponse", ["early"]),
        ("ModelRequest", ["early"]),
        ("ModelResponse", ["late"]),
        ("ModelRequest", ["late"]),
        ("ModelResponse", [None]),
    ]


def test_an_answer_joins_the_existing_returns_of_the_following_request():
    """Half a batch has already been assembled into a request. The missing
    answer belongs in that same request, ahead of its user-facing parts, not in
    a second one."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a"), _call("b")]),
        ModelRequest(parts=[_result("a", {"n": 1}), UserPromptPart(content="also this")]),
    ]
    repaired = repair_partial(messages, results={"b": _result("b", {"n": 2})})
    # Only the trailing marker response is added — no second request.
    assert [type(m).__name__ for m in repaired] == [
        "ModelRequest",
        "ModelResponse",
        "ModelRequest",
        "ModelResponse",
    ]
    assert [type(p).__name__ for p in repaired[2].parts] == [
        "ToolReturnPart",
        "ToolReturnPart",
        "UserPromptPart",
    ]
    assert _returns(repaired) == {"a": {"n": 1}, "b": {"n": 2}}


def test_repair_is_idempotent():
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
    ]
    results = {"a": _result("a", {"hits": 3})}
    once = repair_partial(messages, results=results)
    twice = repair_partial(once, results=results)
    assert once == twice


@pytest.mark.parametrize(
    "trailing",
    [
        pytest.param(None, id="synthesized-tool-returns"),
        pytest.param(
            ModelRequest(
                parts=[RetryPromptPart(content="bad", tool_name="search_events", tool_call_id="a")]
            ),
            id="retry-prompt",
        ),
    ],
)
def test_a_repaired_snapshot_ends_in_a_response(trailing):
    """A completed turn's history ends in a `ModelResponse`, and the next turn's
    prompt arrives as its own `ModelRequest`. pydantic-ai merges the two, so
    ending on a request is not itself a protocol error — but that merge is
    private API under a `>=` pin, and a checkpoint blob shaped exactly like a
    completed turn's is one less thing downstream has to special-case."""
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
    ]
    if trailing is not None:
        messages.append(trailing)
    results = {"a": _result("a", {"hits": 3})}
    repaired = repair_partial(messages, results=results)
    assert isinstance(repaired[-1], ModelResponse)
    assert isinstance(repaired[-1].parts[0], TextPart)
    # Idempotent: a second repair must not stack a second marker.
    twice = repair_partial(repaired, results=results)
    assert twice == repaired


def test_the_library_still_merges_adjacent_requests():
    """Guards the assumption behind RESUME_MARKER being belt-and-braces rather
    than load-bearing (see `agent/resume.py`). If a pydantic-ai bump stops
    merging a trailing tool-return request into the next turn's prompt request,
    this fails here instead of as a role-alternation 400 against a live
    endpoint."""
    from pydantic_ai._agent_graph import _clean_message_history  # noqa: PLC2701

    cleaned = _clean_message_history(
        [
            ModelResponse(parts=[_call("a")]),
            ModelRequest(parts=[_result("a", {"n": 1})]),
            ModelRequest(parts=[UserPromptPart(content="next turn")]),
        ]
    )
    assert [type(m).__name__ for m in cleaned] == ["ModelResponse", "ModelRequest"]
    # And the tool result still leads the merged request, where providers want it.
    assert [type(p).__name__ for p in cleaned[-1].parts] == ["ToolReturnPart", "UserPromptPart"]


def test_repairing_a_prompt_only_snapshot_would_fabricate_a_reply():
    """Why `stream_turn` refuses to checkpoint before the model has committed a
    response: closing the pair on a snapshot that is only the analyst's prompt
    puts an answer to it in the model's mouth. The repair can't detect this —
    a trailing request is a trailing request — so the caller must not ask."""
    repaired = repair_partial([ModelRequest(parts=[UserPromptPart(content="find it")])], results={})
    assert repaired[-1].parts[0].content == RESUME_MARKER


def test_input_is_not_mutated():
    response = ModelResponse(parts=[_call("ghost")])
    request = ModelRequest(parts=[UserPromptPart(content="find it")])
    messages = [request, response]
    repair_partial(messages, results={})
    assert messages == [request, response]
    assert len(response.parts) == 1
    assert request.state == "complete"


def test_a_repaired_snapshot_survives_the_sliding_window():
    """The window picks turn boundaries assuming tool_call/tool_result pairing
    holds. A resumed history that broke it would take the window down with it."""
    from vestigo.agent.window import apply_window

    messages = [
        ModelRequest(parts=[UserPromptPart(content="find it")]),
        ModelResponse(parts=[_call("a")]),
        ModelRequest(parts=[_result("a", {"n": "x" * 5000})]),
        ModelResponse(parts=[_call("b"), _call("c")]),
    ]
    repaired = repair_partial(messages, results={"b": _result("b", {"hits": "y" * 5000})})
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
            # And it always holds something the model actually produced, never
            # just the prompt closed off with a fabricated reply.
            assert any(
                isinstance(m, ModelResponse)
                and not (
                    len(m.parts) == 1 and getattr(m.parts[0], "content", None) == RESUME_MARKER
                )
                for m in recorder.messages
            )

    types = [e["type"] for e in seen]
    assert "checkpoint" in types
    assert types.index("checkpoint") > types.index("tool_result")
    assert recorder.revision >= 2
    assert recorder.messages, "the recorder must hold the turn's messages"


@pytest.mark.asyncio
async def test_no_second_checkpoint_at_a_node_that_ended_on_a_tool_result(monkeypatch):
    """Write amplification: the node-boundary checkpoint after a tool batch
    captures the state the per-result checkpoint already did. A real turn's ~125
    tool calls would otherwise pay 250 full history serializations and 250
    whole-column JSON UPDATEs on the event loop."""
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: _ping_stub())

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        last = messages[-1]
        if any(getattr(p, "part_kind", "") == "tool-return" for p in last.parts):
            yield "done"
        else:
            yield {
                0: DeltaToolCall(name="ping", json_args='{"word": "a"}'),
                1: DeltaToolCall(name="ping", json_args='{"word": "b"}'),
            }

    types: list[str] = []
    async for event in runtime.stream_turn(
        _scope(),
        user_text="ping please",
        history=[],
        recorder=runtime.TurnRecorder(),
        model=FunctionModel(stream_function=model_stream),
    ):
        types.append(event["type"])

    # Two tool results -> two checkpoints, plus one for the node that streamed
    # the final text. No checkpoint immediately follows the last tool_result's.
    assert types.count("checkpoint") == 3
    assert types.count("tool_result") == 2
    for i, kind in enumerate(types):
        if kind == "tool_result":
            assert types[i + 1] == "checkpoint"
            assert types[i + 2] != "checkpoint"


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
async def test_a_synthesized_return_keeps_the_raw_tool_content(monkeypatch):
    """The SSE payload coerces a non-dict/list result with `str(...)` for the
    wire. Writing that coerced form into the replayed history would make the
    resumed conversation disagree with the run about what the tool returned —
    the forensic-reproducibility rule forbids it."""
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    stub = FastMCP("stub")

    @stub.tool()
    async def count(word: str) -> int:
        """Count."""
        return 7

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: stub)

    async def dying(messages: list[ModelMessage], info: AgentInfo):
        last = messages[-1]
        if any(getattr(p, "part_kind", "") == "tool-return" for p in last.parts):
            raise RuntimeError("endpoint died")
        yield {0: DeltaToolCall(name="count", json_args='{"word": "hi"}')}

    recorder = runtime.TurnRecorder()
    mapped: list[object] = []
    with pytest.raises(RuntimeError):
        async for event in runtime.stream_turn(
            _scope(),
            user_text="count please",
            history=[],
            recorder=recorder,
            model=FunctionModel(stream_function=dying),
        ):
            if event["type"] == "tool_result":
                mapped.append(event["result"])

    assert mapped == ["7"], "the SSE payload keeps its coerced shape"
    assert list(_returns(recorder.messages).values()) == [7]


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


@pytest.mark.asyncio
async def test_the_resume_note_reaches_the_model_but_not_the_stored_prompt(monkeypatch):
    """The note has to be *sent* (that is the whole feature) without becoming
    part of the analyst's persisted message — instructions ride with the
    request, the prompt is kept in the history forever."""
    from pydantic_ai.messages import ModelMessage, ModelMessagesTypeAdapter
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from vestigo.agent import runtime
    from vestigo.agent.resume import RESUME_NOTE

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: _ping_stub())

    sent: list[str | None] = []

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        sent.extend(
            m.instructions for m in messages if isinstance(m, ModelRequest) and m.instructions
        )
        yield "carrying on"

    seen = []
    async for event in runtime.stream_turn(
        _scope(),
        user_text="carry on",
        history=[],
        model=FunctionModel(stream_function=model_stream),
        resume_note=RESUME_NOTE,
    ):
        seen.append(event)

    assert sent and RESUME_NOTE in sent[0], "the model must actually receive the note"

    turn = seen[-1]["turn"]
    prompts = [
        part.content
        for message in turn.new_messages
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert prompts and all(RESUME_NOTE not in str(p) for p in prompts)
    # And it is not smuggled in anywhere else the analyst's prompt is stored.
    blob = ModelMessagesTypeAdapter.dump_json(turn.new_messages).decode()
    assert blob.count(RESUME_NOTE[:40]) <= 1, "at most the request's own instructions"


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


def _assert_is_the_turn_blob(history) -> None:
    """The persisted blob is exactly the turn's messages on the (empty) base.

    Asserting only that it is truthy would miss the regression that matters:
    persisting the wrong base — the history duplicated, or the failed attempt
    concatenated ahead of the re-run — still yields a non-empty blob.
    """
    from vestigo.agent.runtime import load_history

    assert len(history) == 2, history
    loaded = load_history(history)
    assert [type(m) for m in loaded] == [ModelRequest, ModelResponse]
    assert loaded[-1].parts[-1].content == "partial answer"


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
    assert fresh.history_partial_at is not None
    _assert_is_the_turn_blob(fresh.history)


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
    assert fresh.history_partial_at is not None
    _assert_is_the_turn_blob(fresh.history)


@pytest.mark.asyncio
async def test_a_checkpoint_that_did_not_advance_is_not_rewritten(store, monkeypatch):
    """Each write is a full `dump_history` plus a whole-column JSON UPDATE of a
    blob that grows into hundreds of KB, on the event loop. Repeating it for a
    recorder that has not moved (the error exit right after a checkpoint) buys
    nothing."""
    from vestigo.api.routers import agent as agent_router

    user, case, conversation = await _conversation(store)
    writes: list[object] = []
    original = store.update_agent_conversation

    async def counting(conversation_id, **kwargs):
        if "history" in kwargs:
            writes.append(kwargs["history"])
        return await original(conversation_id, **kwargs)

    monkeypatch.setattr(store, "update_agent_conversation", counting)

    async def fake_stream_turn(scope, *, user_text, history, recorder=None, **kwargs):
        recorder.messages = _fake_turn_messages()
        recorder.revision += 1
        yield {"type": "checkpoint"}
        # Same state, checkpointed again, then an error exit that persists once
        # more — one write in total is the honest cost.
        yield {"type": "checkpoint"}
        raise RuntimeError("endpoint died")

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)
    payload = agent_router.SendMessageRequest(content="look into this")
    async for _chunk in agent_router._message_stream(case.id, conversation, payload, user):
        pass

    assert len(writes) == 1, "only advanced snapshots are written"
    fresh = await store.get_agent_conversation(case.id, conversation.id)
    _assert_is_the_turn_blob(fresh.history)


def _count_partial_writes(store, monkeypatch) -> list[object]:
    """Record the blobs written by `_persist_partial`, ignoring completed-turn
    writes (which clear the stamp instead of setting it)."""
    writes: list[object] = []
    original = store.update_agent_conversation

    async def counting(conversation_id, **kwargs):
        if kwargs.get("history_partial_at") is not None:
            writes.append(kwargs["history"])
        return await original(conversation_id, **kwargs)

    monkeypatch.setattr(store, "update_agent_conversation", counting)
    return writes


@pytest.mark.asyncio
async def test_checkpoints_in_quick_succession_are_written_once(store, monkeypatch):
    """Each write is a full `dump_history` plus a whole-column JSON UPDATE of a
    monotonically growing blob, on the event loop. A 125-tool-call turn writing
    every time costs bytes quadratic in the turn's length, to buy durability at
    a granularity nobody asked for."""
    from vestigo.agent.runtime import TurnResult
    from vestigo.api.routers import agent as agent_router

    user, case, conversation = await _conversation(store)
    writes = _count_partial_writes(store, monkeypatch)

    async def fake_stream_turn(scope, *, user_text, history, recorder=None, **kwargs):
        for revision in range(1, 4):
            # Each snapshot differs, so the revision guard would let all three
            # through — only the interval floor stops them.
            recorder.messages = _fake_turn_messages() * revision
            recorder.revision = revision
            yield {"type": "checkpoint"}
        yield {
            "type": "result",
            "turn": TurnResult(output_text="done", new_messages=_fake_turn_messages()),
        }

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)
    payload = agent_router.SendMessageRequest(content="look into this")
    async for _chunk in agent_router._message_stream(case.id, conversation, payload, user):
        pass

    assert len(writes) == 1, "the interval floor collapses a burst into one write"


@pytest.mark.asyncio
async def test_a_stop_right_after_a_throttled_checkpoint_still_persists(store, monkeypatch):
    """The floor must never cost an analyst their turn. A stop is the one moment
    the write is worth paying for unconditionally — otherwise throttling would
    reintroduce exactly the loss this feature exists to prevent."""
    import asyncio
    from time import monotonic

    from vestigo.api.routers import agent as agent_router

    user, case, conversation = await _conversation(store)
    turn = agent_router._ActiveTurn(cancel=asyncio.Event(), started=monotonic())
    agent_router._active_turns[conversation.id] = turn
    writes = _count_partial_writes(store, monkeypatch)

    async def fake_stream_turn(scope, *, user_text, history, recorder=None, **kwargs):
        recorder.messages = _fake_turn_messages()
        recorder.revision = 1
        yield {"type": "checkpoint"}
        # Throttled: too soon after the first, and the turn is about to end.
        recorder.messages = [
            *_fake_turn_messages(),
            ModelResponse(parts=[TextPart(content="more")]),
        ]
        recorder.revision = 2
        yield {"type": "checkpoint"}
        turn.cancel.set()
        yield {"type": "text_delta", "text": "never streamed"}

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)
    payload = agent_router.SendMessageRequest(content="look into this")
    async for _chunk in agent_router._message_stream(case.id, conversation, payload, user):
        pass

    assert len(writes) == 2, "the throttled snapshot is written by the stop, not lost"
    fresh = await store.get_agent_conversation(case.id, conversation.id)
    assert fresh.history_partial_at is not None
    assert len(fresh.history) == 3, "the newest snapshot won, not the one that got in first"


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
