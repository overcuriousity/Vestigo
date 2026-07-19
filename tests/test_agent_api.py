"""Tests for the optional AI investigation agent (docs/AGENT.md).

Covers the availability gate (configured + probe), the conversation CRUD
endpoints (503 when unconfigured, per-user visibility), the runtime's SSE
event mapping over a stubbed tool server and FunctionModel (no real LLM),
and the Kimi coding-plan replay shim.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import as_admin, login
from vestigo.agent import availability
from vestigo.agent.tools import AgentScope
from vestigo.core.config import get_settings


@pytest.fixture(autouse=True)
def _reset_probe_cache():
    availability.reset_probe_cache()
    yield
    availability.reset_probe_cache()


def _configure_agent(monkeypatch, provider: str = "openai"):
    monkeypatch.setenv("VESTIGO_AGENT_MODEL", "test-model")
    monkeypatch.setenv("VESTIGO_AGENT_PROVIDER", provider)
    monkeypatch.setenv("VESTIGO_AGENT_API_BASE_URL", "http://localhost:9/v1")
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def test_agent_unconfigured_is_not_configured():
    get_settings.cache_clear()
    assert availability.agent_configured() is False


@pytest.mark.asyncio
async def test_agent_available_false_without_config():
    get_settings.cache_clear()
    assert await availability.agent_available() is False


@pytest.mark.asyncio
async def test_agent_available_requires_probe_success(monkeypatch):
    _configure_agent(monkeypatch)
    assert availability.agent_configured() is True

    async def probe_ok(settings):
        return True

    monkeypatch.setattr(availability, "_probe", probe_ok)
    assert await availability.agent_available(force=True) is True

    async def probe_fail(settings):
        return False

    monkeypatch.setattr(availability, "_probe", probe_fail)
    assert await availability.agent_available(force=True) is False


@pytest.mark.asyncio
async def test_probe_result_is_cached(monkeypatch):
    _configure_agent(monkeypatch)
    calls = {"n": 0}

    async def probe(settings):
        calls["n"] += 1
        return True

    monkeypatch.setattr(availability, "_probe", probe)
    availability.reset_probe_cache()
    assert await availability.agent_available() is True
    assert await availability.agent_available() is True
    assert calls["n"] == 1


def test_health_reports_agent_available(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["agent_available"] is False


def test_kimi_probe_url_and_headers(monkeypatch):
    monkeypatch.setenv("VESTIGO_AGENT_MODEL", "kimi-k2.5")
    monkeypatch.setenv("VESTIGO_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("VESTIGO_AGENT_API_BASE_URL", "https://api.kimi.com/coding")
    monkeypatch.setenv("VESTIGO_AGENT_USER_AGENT", "claude-code/0.1.0")
    get_settings.cache_clear()
    try:
        settings = get_settings()
        assert availability._models_probe_url(settings) == "https://api.kimi.com/coding/v1/models"
        assert availability.probe_headers(settings)["User-Agent"] == "claude-code/0.1.0"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Router: gating + conversation CRUD
# ---------------------------------------------------------------------------


def _make_case_and_timeline(client) -> tuple[str, str]:
    case = client.post("/api/cases/", json={"name": "agent-case"}).json()["case"]
    timeline = client.post(f"/api/cases/{case['id']}/timelines", json={"name": "tl"}).json()[
        "timeline"
    ]
    return case["id"], timeline["id"]


def test_agent_endpoints_503_when_unconfigured(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    )
    assert resp.status_code == 503


@pytest.fixture()
def agent_on(monkeypatch):
    """Force the availability gate open without touching the network."""

    async def always_available(*, force: bool = False):
        return True

    from vestigo.api.routers import agent as agent_router

    monkeypatch.setattr(agent_router, "agent_available", always_available)
    _configure_agent(monkeypatch)
    yield
    get_settings.cache_clear()


def test_conversation_crud(client, admin_bootstrap, agent_on):
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)

    created = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    )
    assert created.status_code == 200, created.text
    conversation = created.json()
    assert conversation["timeline_id"] == timeline_id
    assert conversation["model_id"] == "openai:test-model"

    listed = client.get(f"/api/cases/{case_id}/agent/conversations").json()
    assert [c["id"] for c in listed["conversations"]] == [conversation["id"]]

    fetched = client.get(f"/api/cases/{case_id}/agent/conversations/{conversation['id']}").json()
    assert fetched["messages"] == []

    deleted = client.delete(f"/api/cases/{case_id}/agent/conversations/{conversation['id']}").json()
    assert deleted["deleted"] is True
    assert client.get(f"/api/cases/{case_id}/agent/conversations").json()["conversations"] == []


def test_conversation_404_for_unknown_timeline(client, admin_bootstrap, agent_on):
    as_admin(client, admin_bootstrap)
    case_id, _ = _make_case_and_timeline(client)
    resp = client.post(f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": "nope"})
    assert resp.status_code == 404


def test_conversations_are_private_to_their_creator(client, admin_bootstrap, agent_on, store):
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()

    # A second analyst with case access sees neither the listing entry nor
    # the conversation itself.
    client.post("/api/admin/users", json={"username": "analyst2", "password": "abcdefgh12"})
    client.post(
        f"/api/cases/{case_id}/members",
        json={"user_id": None, "username": "analyst2", "level": "contribute"},
    )
    other = client.__class__(client.app)
    login(other, "analyst2", "abcdefgh12")
    listed = other.get(f"/api/cases/{case_id}/agent/conversations")
    if listed.status_code == 200:
        assert conversation["id"] not in [c["id"] for c in listed.json()["conversations"]]
        resp = other.get(f"/api/cases/{case_id}/agent/conversations/{conversation['id']}")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Runtime: SSE event mapping over a stubbed tool server + FunctionModel
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stream_turn_maps_events(monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    stub = FastMCP("stub")

    @stub.tool()
    async def ping(word: str) -> dict:
        """Echo."""
        return {"echo": word}

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: stub)

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        last = messages[-1]
        if any(getattr(p, "part_kind", "") == "tool-return" for p in last.parts):
            yield "the echo "
            yield "came back"
        else:
            yield {0: DeltaToolCall(name="ping", json_args='{"word": "hi"}')}

    scope = AgentScope(
        case_id="c1",
        timeline_id="t1",
        user=None,  # unused by the stubbed server
        source_ids=["s1"],
        field_mappings=None,
        source_offsets=None,
    )
    events = []
    async for event in runtime.stream_turn(
        scope,
        user_text="ping please",
        history=[],
        view_filters={"q": "ssh"},
        model=FunctionModel(stream_function=model_stream),
    ):
        events.append(event)

    types = [e["type"] for e in events]
    assert "tool_call" in types
    assert "tool_result" in types
    assert types[-1] == "result"
    call = next(e for e in events if e["type"] == "tool_call")
    assert call["tool"] == "ping"
    assert call["args"] == {"word": "hi"}
    result_event = next(e for e in events if e["type"] == "tool_result")
    assert "echo" in str(result_event["result"])
    turn = events[-1]["turn"]
    assert turn.output_text == "the echo came back"
    assert len(turn.new_messages) >= 2
    # History round-trips through JSON for Postgres persistence.
    dumped = runtime.dump_history(turn.new_messages)
    assert runtime.load_history(dumped)


@pytest.mark.asyncio
async def test_stream_turn_result_carries_measured_token_usage(monkeypatch):
    """FunctionModel reports non-zero fake usage; TurnResult must surface it."""
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from vestigo.agent import runtime

    stub = FastMCP("stub")
    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: stub)

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        yield "a plain answer, no tools needed"

    scope = AgentScope(
        case_id="c1",
        timeline_id="t1",
        user=None,
        source_ids=["s1"],
        field_mappings=None,
        source_offsets=None,
    )
    events = []
    async for event in runtime.stream_turn(
        scope,
        user_text="what happened",
        history=[],
        model=FunctionModel(stream_function=model_stream),
    ):
        events.append(event)

    turn = events[-1]["turn"]
    assert turn.prompt_tokens is not None and turn.prompt_tokens > 0
    assert turn.completion_tokens is not None and turn.completion_tokens > 0


def test_send_message_persists_and_streams_token_usage(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """A streamed turn stamps the persisted assistant row and the done SSE event."""
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from vestigo.agent import runtime

    stub = FastMCP("stub")
    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: stub)

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        yield "the answer is 42"

    monkeypatch.setattr(
        runtime, "build_model", lambda settings=None: FunctionModel(stream_function=model_stream)
    )

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "hello agent"},
    )
    assert resp.status_code == 200
    done_events = [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]
    done = next(e for e in done_events if e["type"] == "done")
    assert done["prompt_tokens"] and done["prompt_tokens"] > 0
    assert done["completion_tokens"] and done["completion_tokens"] > 0

    async def _fetch_messages():
        return await store.list_agent_messages(conversation["id"])

    messages = asyncio.run(_fetch_messages())
    assistant = [m for m in messages if m.role == "assistant"][-1]
    assert assistant.prompt_tokens and assistant.prompt_tokens > 0
    assert assistant.completion_tokens and assistant.completion_tokens > 0


# ---------------------------------------------------------------------------
# Kimi coding-plan shim
# ---------------------------------------------------------------------------


def test_is_kimi_coding_endpoint():
    from vestigo.agent.runtime import _is_kimi_coding_endpoint

    assert _is_kimi_coding_endpoint("https://api.kimi.com/coding")
    assert _is_kimi_coding_endpoint("https://api.kimi.com/coding/v1")
    assert not _is_kimi_coding_endpoint("https://api.moonshot.ai/v1")
    assert not _is_kimi_coding_endpoint("https://evil.example/coding")
    assert not _is_kimi_coding_endpoint(None)


@pytest.mark.asyncio
async def test_kimi_shim_injects_unsigned_thinking_on_tool_call_replay():
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        TextPart,
        ToolCallPart,
        ToolReturnPart,
        UserPromptPart,
    )
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.providers.anthropic import AnthropicProvider

    from vestigo.agent.runtime import KimiAnthropicModel

    model = KimiAnthropicModel(
        "kimi-k2.5",
        provider=AnthropicProvider(api_key="test", base_url="https://api.kimi.com/coding"),
    )
    messages = [
        ModelRequest(parts=[UserPromptPart(content="find anomalies")]),
        ModelResponse(parts=[ToolCallPart(tool_name="ping", args={}, tool_call_id="tc1")]),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="ping", content={"ok": True}, tool_call_id="tc1")]
        ),
        ModelResponse(parts=[TextPart("done")]),
        ModelRequest(parts=[UserPromptPart(content="thanks, continue")]),
    ]
    _, anthropic_messages = await model._map_message(messages, ModelRequestParameters(), {})
    assistant_tool_msgs = [
        m
        for m in anthropic_messages
        if m.get("role") == "assistant"
        and isinstance(m.get("content"), list)
        and any(b.get("type") == "tool_use" for b in m["content"] if isinstance(b, dict))
    ]
    assert assistant_tool_msgs, "expected a replayed assistant tool-call message"
    for m in assistant_tool_msgs:
        first = m["content"][0]
        assert first["type"] == "thinking"
        assert first["signature"] == ""


async def test_agent_message_token_columns(store):
    await store.init_schema()
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="openai:m")
    msg = await store.add_agent_message(
        conv.id, "assistant", "hi", prompt_tokens=1200, completion_tokens=80
    )
    assert msg.to_dict()["prompt_tokens"] == 1200
    assert msg.to_dict()["completion_tokens"] == 80
    bare = await store.add_agent_message(conv.id, "user", "q")
    assert bare.to_dict()["prompt_tokens"] is None


async def test_agent_proposal_lifecycle(store):
    await store.init_schema()
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    p = await store.create_agent_proposal(
        case_id="c1",
        timeline_id="t1",
        conversation_id=conv.id,
        tag="lateral-movement",
        comment=None,
        rationale="pattern X",
        events=[{"source_id": "s1", "event_id": "e1"}],
    )
    assert p.status == "proposed"
    decided = await store.decide_agent_proposal(p.id, status="confirmed", decided_by="alice")
    assert decided is not None and decided.status == "confirmed"
    # second decision must not go through
    assert await store.decide_agent_proposal(p.id, status="rejected", decided_by="bob") is None
    assert (await store.get_agent_proposal(conv.id, p.id)).status == "confirmed"
