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
from vestigo.agent.config import config_fingerprint, resolve_agent_config
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


@pytest.mark.asyncio
async def test_agent_unconfigured_is_not_configured(store):
    get_settings.cache_clear()
    config = await resolve_agent_config()
    assert availability.agent_configured(config) is False


@pytest.mark.asyncio
async def test_agent_available_false_without_config(store):
    get_settings.cache_clear()
    assert await availability.agent_available() is False


@pytest.mark.asyncio
async def test_agent_available_requires_probe_success(store, monkeypatch):
    _configure_agent(monkeypatch)
    config = await resolve_agent_config()
    assert availability.agent_configured(config) is True

    async def probe_ok(config):
        return True

    monkeypatch.setattr(availability, "_probe", probe_ok)
    assert await availability.agent_available(force=True) is True

    async def probe_fail(config):
        return False

    monkeypatch.setattr(availability, "_probe", probe_fail)
    assert await availability.agent_available(force=True) is False


@pytest.mark.asyncio
async def test_probe_result_is_cached(store, monkeypatch):
    _configure_agent(monkeypatch)
    calls = {"n": 0}

    async def probe(config):
        calls["n"] += 1
        return True

    monkeypatch.setattr(availability, "_probe", probe)
    availability.reset_probe_cache()
    assert await availability.agent_available() is True
    assert await availability.agent_available() is True
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_probe_is_stale_while_revalidate(store, monkeypatch):
    """An expired same-fingerprint cache entry serves stale and refreshes in background."""
    import time as _time

    _configure_agent(monkeypatch)
    results = {"value": True}
    calls = {"n": 0}

    async def probe(config):
        calls["n"] += 1
        return results["value"]

    monkeypatch.setattr(availability, "_probe", probe)
    availability.reset_probe_cache()
    assert await availability.agent_available() is True
    assert calls["n"] == 1

    # Age the cache past the TTL and flip the endpoint to down: the next call
    # must still answer immediately with the stale True (never block /api/health
    # on a hung endpoint), while a background refresh picks up the new state.
    results["value"] = False
    cached = availability._cache
    availability._cache = (cached[0], _time.monotonic() - 10_000, cached[2])
    assert await availability.agent_available() is True
    assert availability._refresh_task is not None
    await availability._refresh_task
    assert calls["n"] == 2
    assert await availability.agent_available() is False


@pytest.mark.asyncio
async def test_probe_bearer_header_only_for_kimi(store, monkeypatch):
    """The anthropic-protocol probe never duplicates the key into a Bearer header
    for non-Kimi endpoints."""
    captured: dict[str, dict] = {}

    class SpyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, headers=None):
            captured["headers"] = headers or {}

            class _Resp:
                status_code = 200

            return _Resp()

    monkeypatch.setattr(availability.httpx, "AsyncClient", SpyClient)

    monkeypatch.setenv("VESTIGO_AGENT_MODEL", "m")
    monkeypatch.setenv("VESTIGO_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("VESTIGO_AGENT_API_KEY", "sk-secret")
    monkeypatch.setenv("VESTIGO_AGENT_API_BASE_URL", "https://api.anthropic.com")
    get_settings.cache_clear()
    assert await availability._probe(await resolve_agent_config()) is True
    assert captured["headers"]["x-api-key"] == "sk-secret"
    assert "Authorization" not in captured["headers"]

    monkeypatch.setenv("VESTIGO_AGENT_API_BASE_URL", "https://api.kimi.com/coding")
    get_settings.cache_clear()
    assert await availability._probe(await resolve_agent_config()) is True
    assert captured["headers"]["Authorization"] == "Bearer sk-secret"


def test_health_reports_agent_available(client):
    resp = client.get("/api/health")
    assert resp.status_code == 200
    assert resp.json()["agent_available"] is False


@pytest.mark.asyncio
async def test_kimi_probe_url_and_headers(store, monkeypatch):
    monkeypatch.setenv("VESTIGO_AGENT_MODEL", "kimi-k2.5")
    monkeypatch.setenv("VESTIGO_AGENT_PROVIDER", "anthropic")
    monkeypatch.setenv("VESTIGO_AGENT_API_BASE_URL", "https://api.kimi.com/coding")
    monkeypatch.setenv("VESTIGO_AGENT_USER_AGENT", "claude-code/0.1.0")
    get_settings.cache_clear()
    try:
        config = await resolve_agent_config()
        assert availability._models_probe_url(config) == "https://api.kimi.com/coding/v1/models"
        assert availability.probe_headers(config)["User-Agent"] == "claude-code/0.1.0"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# AgentConfig resolver (A7): env wins per field, DB fills gaps, defaults last.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_env_wins_per_field(store, monkeypatch):
    """Env overrides only the fields it sets; DB fills the rest; unset fields default."""
    await store.init_schema()
    await store.update_agent_settings({"model": "db-model", "api_base_url": "http://db"}, "root")
    monkeypatch.setenv("VESTIGO_AGENT_MODEL", "env-model")
    get_settings.cache_clear()
    try:
        config = await resolve_agent_config()
        assert config.model == "env-model"
        assert config.sources["model"] == "env"
        assert config.api_base_url == "http://db"
        assert config.sources["api_base_url"] == "db"
        assert config.provider == "openai"
        assert config.sources["provider"] == "default"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_resolver_picks_up_reasoning_effort_env(store, monkeypatch):
    """VESTIGO_AGENT_REASONING_EFFORT is a real Settings field, not silently dropped."""
    monkeypatch.setenv("VESTIGO_AGENT_REASONING_EFFORT", "high")
    get_settings.cache_clear()
    try:
        config = await resolve_agent_config()
        assert config.reasoning_effort == "high"
        assert config.sources["reasoning_effort"] == "env"
    finally:
        get_settings.cache_clear()


def test_admin_agent_settings_shows_reasoning_effort_env_pinned(
    client, admin_bootstrap, store, monkeypatch
):
    from tests.conftest import as_admin

    as_admin(client, admin_bootstrap)
    monkeypatch.setenv("VESTIGO_AGENT_REASONING_EFFORT", "high")
    get_settings.cache_clear()
    try:
        resp = client.get("/api/admin/agent-settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["effective"]["reasoning_effort"] == "high"
        assert body["sources"]["reasoning_effort"] == "env"
        assert body["env_vars"]["reasoning_effort"] == "VESTIGO_AGENT_REASONING_EFFORT"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_probe_cache_invalidates_on_config_change(store, monkeypatch):
    """A DB-side settings edit changes the fingerprint and bypasses the TTL."""
    await store.init_schema()
    _configure_agent(monkeypatch)
    calls = {"n": 0}

    async def probe(config):
        calls["n"] += 1
        return True

    monkeypatch.setattr(availability, "_probe", probe)
    availability.reset_probe_cache()
    assert await availability.agent_available() is True
    assert await availability.agent_available() is True
    assert calls["n"] == 1

    await store.update_agent_settings({"max_turns": 42}, "root")
    assert await availability.agent_available() is True
    assert calls["n"] == 2


def test_config_fingerprint_ignores_sources():
    from vestigo.agent.config import AgentConfig

    a = AgentConfig(
        model="m",
        provider="openai",
        api_base_url=None,
        api_key=None,
        user_agent=None,
        extra_headers=None,
        max_turns=15,
        reasoning_effort="off",
        sources={"model": "env"},
    )
    b = AgentConfig(
        model="m",
        provider="openai",
        api_base_url=None,
        api_key=None,
        user_agent=None,
        extra_headers=None,
        max_turns=15,
        reasoning_effort="off",
        sources={"model": "db"},
    )
    assert config_fingerprint(a) == config_fingerprint(b)


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
# Proposals: confirm/reject (A1)
# ---------------------------------------------------------------------------


async def _seed_proposal(store, case_id, timeline_id, user_id, *, tag="suspicious", comment=None):
    conv = await store.create_agent_conversation(case_id, timeline_id, user_id, model_id="m")
    proposal = await store.create_agent_proposal(
        case_id=case_id,
        timeline_id=timeline_id,
        conversation_id=conv.id,
        tag=tag,
        comment=comment,
        rationale="clustered failed logins",
        events=[
            {"source_id": "s1", "event_id": "e1"},
            {"source_id": "s1", "event_id": "e2"},
        ],
    )
    return conv, proposal


def _patch_proposal_resolver(monkeypatch, found: dict[str, str], unknown: list[str] | None = None):
    from vestigo.api.routers import agent as agent_router

    async def fake_resolve(scope, event_ids):
        return found, unknown or []

    monkeypatch.setattr(agent_router, "_proposal_resolver", lambda: fake_resolve)


def test_confirm_proposal_writes_annotations(client, admin_bootstrap, agent_on, store, monkeypatch):
    owner = as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)

    async def _seed():
        return await _seed_proposal(store, case_id, timeline_id, owner["id"])

    conv, proposal = asyncio.run(_seed())
    _patch_proposal_resolver(monkeypatch, {"e1": "s1", "e2": "s1"})

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conv.id}/proposals/{proposal.id}/confirm"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["written"] == 2
    assert body["skipped_event_ids"] == []
    assert body["proposal"]["status"] == "confirmed"

    async def _check():
        rows = await store.list_annotations(case_id, "s1", "e1")
        return rows

    rows = asyncio.run(_check())
    assert any(
        r.origin == "agentic-analysis"
        and r.annotation_type == "tag"
        and r.content == "suspicious"
        and r.created_by == owner["id"]
        for r in rows
    )

    async def _audit():
        return await store.query_audit(case_id=case_id)

    audit_rows = asyncio.run(_audit())
    assert any(a.action == "agent.annotation_confirm" for a in audit_rows)


def test_confirm_reports_skipped_events(client, admin_bootstrap, agent_on, store, monkeypatch):
    owner = as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)

    async def _seed():
        return await _seed_proposal(store, case_id, timeline_id, owner["id"])

    conv, proposal = asyncio.run(_seed())
    # Only e1 still resolves against the current scope; e2's source left the
    # timeline (or the event was otherwise removed) since propose time.
    _patch_proposal_resolver(monkeypatch, {"e1": "s1"}, unknown=["e2"])

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conv.id}/proposals/{proposal.id}/confirm"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["written"] == 1
    assert body["skipped_event_ids"] == ["e2"]
    assert body["proposal"]["status"] == "confirmed"

    async def _check(event_id):
        return await store.list_annotations(case_id, "s1", event_id)

    e1_rows = asyncio.run(_check("e1"))
    assert any(
        r.origin == "agentic-analysis"
        and r.annotation_type == "tag"
        and r.content == "suspicious"
        and r.created_by == owner["id"]
        for r in e1_rows
    )
    e2_rows = asyncio.run(_check("e2"))
    assert e2_rows == []

    async def _audit():
        return await store.query_audit(case_id=case_id)

    audit_rows = asyncio.run(_audit())
    confirm_row = next(a for a in audit_rows if a.action == "agent.annotation_confirm")
    assert confirm_row.detail["skipped_event_ids"] == ["e2"]
    assert confirm_row.detail["written"] == 1


def test_confirm_is_idempotent(client, admin_bootstrap, agent_on, store, monkeypatch):
    owner = as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)

    async def _seed():
        return await _seed_proposal(store, case_id, timeline_id, owner["id"])

    conv, proposal = asyncio.run(_seed())
    _patch_proposal_resolver(monkeypatch, {"e1": "s1", "e2": "s1"})

    url = f"/api/cases/{case_id}/agent/conversations/{conv.id}/proposals/{proposal.id}/confirm"
    first = client.post(url)
    assert first.status_code == 200, first.text
    second = client.post(url)
    assert second.status_code == 409


def test_reject_proposal(client, admin_bootstrap, agent_on, store, monkeypatch):
    owner = as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)

    async def _seed():
        return await _seed_proposal(store, case_id, timeline_id, owner["id"])

    conv, proposal = asyncio.run(_seed())
    _patch_proposal_resolver(monkeypatch, {"e1": "s1", "e2": "s1"})

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conv.id}/proposals/{proposal.id}/reject"
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["proposal"]["status"] == "rejected"

    async def _check():
        return await store.list_annotations(case_id, "s1", "e1")

    assert asyncio.run(_check()) == []

    async def _audit():
        return await store.query_audit(case_id=case_id)

    audit_rows = asyncio.run(_audit())
    assert any(a.action == "agent.annotation_reject" for a in audit_rows)


def test_list_proposals(client, admin_bootstrap, agent_on, store):
    owner = as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)

    async def _seed():
        return await _seed_proposal(store, case_id, timeline_id, owner["id"])

    conv, proposal = asyncio.run(_seed())
    resp = client.get(f"/api/cases/{case_id}/agent/conversations/{conv.id}/proposals")
    assert resp.status_code == 200, resp.text
    ids = [p["id"] for p in resp.json()["proposals"]]
    assert ids == [proposal.id]


def test_only_owner_can_decide(client, admin_bootstrap, agent_on, store, monkeypatch):
    owner = as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)

    async def _seed():
        return await _seed_proposal(store, case_id, timeline_id, owner["id"])

    conv, proposal = asyncio.run(_seed())
    _patch_proposal_resolver(monkeypatch, {"e1": "s1", "e2": "s1"})

    # There is no per-case member grant for personal cases (only team roles);
    # give the second analyst admin-level case access (MANAGE on every case)
    # so the only thing standing between them and the proposal is
    # conversation ownership, which is what this test exercises.
    client.post(
        "/api/admin/users",
        json={"username": "analyst2", "password": "abcdefgh12", "is_admin": True},
    )
    other = client.__class__(client.app)
    login(other, "analyst2", "abcdefgh12")

    resp = other.post(
        f"/api/cases/{case_id}/agent/conversations/{conv.id}/proposals/{proposal.id}/confirm"
    )
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
    # The first chunk of a text part arrives as PartStartEvent, not a delta —
    # the streamed text_delta events must still reassemble the full text.
    streamed = "".join(e["text"] for e in events if e["type"] == "text_delta")
    assert streamed == "the echo came back"
    turn = events[-1]["turn"]
    assert turn.output_text == "the echo came back"
    assert len(turn.new_messages) >= 2
    # History round-trips through JSON for Postgres persistence.
    dumped = runtime.dump_history(turn.new_messages)
    assert runtime.load_history(dumped)


@pytest.mark.asyncio
async def test_stream_turn_lets_the_model_correct_a_rejected_tool_call(monkeypatch):
    """Tool legality errors name the legal alternative and are meant to be
    acted on. pydantic-ai's default budget of one retry meant a second wrong
    guess killed the whole turn (propose_chart heatmap/pivot, 2026-07-20)."""
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import AgentInfo, DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    stub = FastMCP("stub")
    attempts = {"n": 0}

    @stub.tool()
    async def picky(word: str) -> dict:
        """Accepts only the third guess."""
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise ValueError('word="x" is illegal; use word="right".')
        return {"ok": True}

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: stub)

    async def model_stream(messages: list[ModelMessage], info: AgentInfo):
        if any(getattr(p, "part_kind", "") == "tool-return" for p in messages[-1].parts):
            yield "done"
        else:
            yield {0: DeltaToolCall(name="picky", json_args='{"word": "x"}')}

    scope = AgentScope(
        case_id="c1",
        timeline_id="t1",
        user=None,
        source_ids=["s1"],
        field_mappings=None,
        source_offsets=None,
    )
    events = [
        event
        async for event in runtime.stream_turn(
            scope,
            user_text="try it",
            history=[],
            model=FunctionModel(stream_function=model_stream),
        )
    ]

    assert attempts["n"] == 3  # two rejections survived, the third call landed
    assert events[-1]["turn"].output_text == "done"


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
        runtime,
        "build_model",
        lambda config=None, http_client=None: FunctionModel(stream_function=model_stream),
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


def _reserve_turn(agent_router, conversation_id: str, *, age: float = 0.0):
    """Fake the reservation `send_message` makes, optionally already aged."""
    from time import monotonic

    turn = agent_router._ActiveTurn(cancel=asyncio.Event(), started=monotonic() - age)
    agent_router._active_turns[conversation_id] = turn
    return turn


def test_active_flag_reflects_a_running_turn(client, admin_bootstrap, agent_on):
    """`active` is what lets a reopened panel show a working Stop instead of a
    dead input — it must track the in-flight reservation, not a column."""
    from vestigo.api.routers import agent as agent_router

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    assert conversation["active"] is False

    listed = client.get(
        f"/api/cases/{case_id}/agent/conversations", params={"timeline_id": timeline_id}
    ).json()["conversations"]
    assert listed[0]["active"] is False

    _reserve_turn(agent_router, conversation["id"])
    try:
        one = client.get(f"/api/cases/{case_id}/agent/conversations/{conversation['id']}").json()
        assert one["active"] is True
        listed = client.get(
            f"/api/cases/{case_id}/agent/conversations", params={"timeline_id": timeline_id}
        ).json()["conversations"]
        assert listed[0]["active"] is True
    finally:
        agent_router._active_turns.pop(conversation["id"], None)


def test_cancel_sets_the_turn_event_and_is_idempotent(client, admin_bootstrap, agent_on):
    """Cancel signals the running generator. Cancelling an idle conversation is
    a no-op, not an error — the client races the turn's own completion."""
    from vestigo.api.routers import agent as agent_router

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    url = f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/cancel"

    # Idle: reports nothing to cancel rather than 404/409.
    assert client.post(url).json() == {"cancelled": False}

    turn = _reserve_turn(agent_router, conversation["id"])
    try:
        assert client.post(url).json() == {"cancelled": True}
        assert turn.cancel.is_set()
    finally:
        agent_router._active_turns.pop(conversation["id"], None)

    # A stop truncates the record, so it has to be attributable afterwards.
    audit = client.get("/api/admin/audit", params={"action": "agent.turn_cancelled"}).json()[
        "audit"
    ]
    assert len(audit) == 1
    assert audit[0]["target_id"] == conversation["id"]


def test_patch_conversation_tools_updates_and_audits(client, admin_bootstrap, agent_on):
    """Tool changes take effect from the next turn and land in the audit trail —
    the row carries only the current restriction, so who narrowed the agent's
    reach and when has to be recorded somewhere durable."""
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    url = f"/api/cases/{case_id}/agent/conversations/{conversation['id']}"

    resp = client.patch(url, json={"disabled_tools": ["histogram", "field_terms"]})
    assert resp.status_code == 200
    assert resp.json()["disabled_tools"] == ["field_terms", "histogram"]

    # Round-trips on read.
    assert client.get(url).json()["disabled_tools"] == ["field_terms", "histogram"]

    # An empty list means "re-enable everything", not "no change".
    assert client.patch(url, json={"disabled_tools": []}).json()["disabled_tools"] == []

    audit = client.get(
        "/api/admin/audit", params={"action": "agent.conversation_tools_changed"}
    ).json()["audit"]
    assert len(audit) == 2
    # Newest first: the clearing change, then the narrowing one.
    assert audit[0]["detail"]["disabled_tools_after"] == []
    assert audit[1]["detail"]["disabled_tools_after"] == ["field_terms", "histogram"]


def test_patch_conversation_without_tools_leaves_them_alone(client, admin_bootstrap, agent_on):
    """Omitting the field means "no change", not "clear" — a PATCH that says
    nothing about tools must not silently widen the agent's reach."""
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations",
        json={"timeline_id": timeline_id, "disabled_tools": ["histogram"]},
    ).json()
    url = f"/api/cases/{case_id}/agent/conversations/{conversation['id']}"

    assert client.patch(url, json={}).json()["disabled_tools"] == ["histogram"]
    assert client.get(url).json()["disabled_tools"] == ["histogram"]
    # A no-op must not manufacture an audit row either.
    audit = client.get(
        "/api/admin/audit", params={"action": "agent.conversation_tools_changed"}
    ).json()["audit"]
    assert audit == []


def test_stranded_turn_reservation_expires(client, admin_bootstrap, agent_on):
    """`send_message` reserves before the generator starts, so a reservation can
    strand if the ASGI task dies in between. Without an age ceiling that
    conversation would 409 forever and show a Stop button that does nothing."""
    from vestigo.api.routers import agent as agent_router

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    url = f"/api/cases/{case_id}/agent/conversations/{conversation['id']}"

    _reserve_turn(agent_router, conversation["id"], age=agent_router._TURN_STALE_AFTER + 1)
    try:
        assert client.get(url).json()["active"] is False
        # ...and the entry is pruned, so the conversation is usable again.
        assert conversation["id"] not in agent_router._active_turns
        assert client.post(f"{url}/cancel").json() == {"cancelled": False}
    finally:
        agent_router._active_turns.pop(conversation["id"], None)


@pytest.mark.asyncio
async def test_cancelled_turn_persists_what_streamed(store, monkeypatch):
    """A stopped turn stays part of the record.

    The cancel check has to live *inside* the turn generator: signalling it
    from the caller and breaking out closes the generator with a
    ``GeneratorExit``, which — deriving from ``BaseException`` — no ``except
    Exception`` catches, so the streamed text would vanish.
    """
    from vestigo.api.routers import agent as agent_router

    await store.init_schema()
    user = await store.create_user("u1", "analyst", is_admin=True)
    case = await store.create_case("c1", "Case 1", owner_id=user.id)
    timeline = await store.create_timeline(case.id, "tl1", "Timeline 1", source_ids=[])
    conversation = await store.create_agent_conversation(
        case.id, timeline.id, user.id, model_id="stub:stub"
    )

    turn = _reserve_turn(agent_router, conversation.id)

    async def fake_stream_turn(scope, *, user_text, history, view_filters=None):
        yield {"type": "text_delta", "text": "partial "}
        yield {"type": "text_delta", "text": "answer"}
        turn.cancel.set()  # analyst hits Stop mid-turn
        yield {"type": "text_delta", "text": "never streamed"}
        raise AssertionError("the turn should have stopped before this")

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)

    payload = agent_router.SendMessageRequest(content="look into this")
    chunks = [
        chunk async for chunk in agent_router._message_stream(case.id, conversation, payload, user)
    ]

    events = [json.loads(c.removeprefix("data: ").strip()) for c in chunks]
    assert events[-1] == {"type": "cancelled"}
    # The reservation is released, so the conversation is usable again.
    assert conversation.id not in agent_router._active_turns

    messages = await store.list_agent_messages(conversation.id)
    assistant = [m for m in messages if m.role == "assistant"]
    assert len(assistant) == 1
    # Marked, and carrying exactly what streamed before the stop.
    assert assistant[0].content == "partial answer [stopped]"


async def _turn_ending_with(store, monkeypatch, exc: Exception) -> tuple[list[dict], list]:
    """Run one turn whose stream raises *exc* after streaming some text."""
    from vestigo.api.routers import agent as agent_router

    await store.init_schema()
    user = await store.create_user("u1", "analyst", is_admin=True)
    case = await store.create_case("c1", "Case 1", owner_id=user.id)
    timeline = await store.create_timeline(case.id, "tl1", "Timeline 1", source_ids=[])
    conversation = await store.create_agent_conversation(
        case.id, timeline.id, user.id, model_id="stub:stub"
    )

    async def fake_stream_turn(scope, *, user_text, history, view_filters=None):
        yield {"type": "text_delta", "text": "partial answer"}
        raise exc

    monkeypatch.setattr(agent_router, "stream_turn", fake_stream_turn)

    payload = agent_router.SendMessageRequest(content="chart this")
    chunks = [
        chunk async for chunk in agent_router._message_stream(case.id, conversation, payload, user)
    ]
    events = [json.loads(c.removeprefix("data: ").strip()) for c in chunks]
    return events, await store.list_agent_messages(conversation.id)


@pytest.mark.asyncio
async def test_tool_retry_exhaustion_is_named_not_generic(store, monkeypatch):
    """A model that cannot get one tool's arguments right within its retry
    budget used to kill the turn with 'Agent turn failed — see server logs',
    which tells the analyst nothing (a propose_chart heatmap/pivot mix-up cost
    a real turn on 2026-07-20)."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    events, messages = await _turn_ending_with(
        store, monkeypatch, UnexpectedModelBehavior("Tool 'propose_chart' exceeded max retries")
    )

    error = next(e for e in events if e["type"] == "error")
    assert error["code"] == "tool_retry_exhausted"
    assert "propose_chart" in error["detail"]
    # What streamed before the failure stays part of the record.
    assistant = [m for m in messages if m.role == "assistant"]
    assert assistant[0].content == "partial answer [interrupted]"


@pytest.mark.asyncio
async def test_spent_turn_budget_is_named_not_generic(store, monkeypatch):
    """Exhausting UsageLimits is a 'ask something narrower' situation, not a
    server error — and following up on many findings can reach it."""
    from pydantic_ai.exceptions import UsageLimitExceeded

    events, _ = await _turn_ending_with(
        store, monkeypatch, UsageLimitExceeded("The next request would exceed the request_limit")
    )

    error = next(e for e in events if e["type"] == "error")
    assert error["code"] == "turn_limit_reached"
    assert "max_turns" in error["detail"]


def test_patch_conversation_rejects_unknown_tool(client, admin_bootstrap, agent_on):
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    resp = client.patch(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}",
        json={"disabled_tools": ["no_such_tool"]},
    )
    assert resp.status_code == 422


def test_send_message_409_while_turn_active(client, admin_bootstrap, agent_on, monkeypatch):
    """One turn at a time per conversation — a concurrent POST gets a 409."""
    from vestigo.api.routers import agent as agent_router

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()

    url = f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages"

    # Simulate an in-flight turn: the reservation is what send_message checks.
    _reserve_turn(agent_router, conversation["id"])
    try:
        resp = client.post(url, json={"content": "hello"})
        assert resp.status_code == 409
    finally:
        agent_router._active_turns.pop(conversation["id"], None)

    # After the reservation is released a turn runs — and releases itself.
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.models.function import FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: FastMCP("stub"))

    async def model_stream(messages, info):
        yield "ok"

    monkeypatch.setattr(
        runtime,
        "build_model",
        lambda config=None, http_client=None: FunctionModel(stream_function=model_stream),
    )
    resp = client.post(url, json={"content": "hello again"})
    assert resp.status_code == 200
    assert conversation["id"] not in agent_router._active_turns


@pytest.mark.asyncio
async def test_stream_turn_closes_its_http_client(monkeypatch):
    """A turn that builds its own model must close the HTTP client it opened."""
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.models.function import FunctionModel

    from vestigo.agent import runtime

    closed = {"n": 0}

    class SpyClient:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            closed["n"] += 1

    monkeypatch.setattr(runtime.httpx, "AsyncClient", SpyClient)
    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: FastMCP("stub"))

    async def model_stream(messages, info):
        yield "done"

    monkeypatch.setattr(
        runtime,
        "build_model",
        lambda config=None, http_client=None: FunctionModel(stream_function=model_stream),
    )
    monkeypatch.setenv("VESTIGO_AGENT_MODEL", "m")
    monkeypatch.setenv("VESTIGO_AGENT_API_BASE_URL", "http://localhost:9/v1")
    get_settings.cache_clear()
    try:
        scope = AgentScope(
            case_id="c1",
            timeline_id="t1",
            user=None,
            source_ids=["s1"],
            field_mappings=None,
            source_offsets=None,
        )
        async for _ in runtime.stream_turn(scope, user_text="hi", history=[]):
            pass
    finally:
        get_settings.cache_clear()
    assert closed["n"] == 1


# ---------------------------------------------------------------------------
# Kimi coding-plan shim
# ---------------------------------------------------------------------------


def test_is_kimi_coding_endpoint():
    from vestigo.agent.config import is_kimi_coding_endpoint

    assert is_kimi_coding_endpoint("https://api.kimi.com/coding")
    assert is_kimi_coding_endpoint("https://api.kimi.com/coding/v1")
    assert not is_kimi_coding_endpoint("https://api.moonshot.ai/v1")
    assert not is_kimi_coding_endpoint("https://evil.example/coding")
    assert not is_kimi_coding_endpoint(None)


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


# ---------------------------------------------------------------------------
# Reasoning-effort translation (A7)
# ---------------------------------------------------------------------------


def _agent_config(**overrides):
    from vestigo.agent.config import AgentConfig

    fields = {
        "model": "m",
        "provider": "openai",
        "api_base_url": None,
        "api_key": None,
        "user_agent": None,
        "extra_headers": None,
        "max_turns": 15,
        "reasoning_effort": "off",
        "sources": {},
    }
    fields.update(overrides)
    return AgentConfig(**fields)


def test_effort_settings_off_is_none():
    from vestigo.agent.runtime import effort_model_settings

    for provider in ("openai", "anthropic"):
        config = _agent_config(provider=provider, reasoning_effort="off")
        assert effort_model_settings(config) is None

    kimi_config = _agent_config(
        provider="anthropic",
        api_base_url="https://api.kimi.com/coding",
        reasoning_effort="off",
    )
    assert effort_model_settings(kimi_config) is None


def test_effort_settings_openai_verbatim():
    from pydantic_ai.models.openai import OpenAIChatModelSettings

    from vestigo.agent.runtime import effort_model_settings

    config = _agent_config(provider="openai", reasoning_effort="high")
    settings = effort_model_settings(config)
    assert isinstance(settings, dict)
    assert settings == OpenAIChatModelSettings(openai_reasoning_effort="high")
    assert settings["openai_reasoning_effort"] == "high"


def test_effort_settings_anthropic_budget():
    from vestigo.agent.runtime import effort_model_settings

    config = _agent_config(provider="anthropic", reasoning_effort="medium")
    settings = effort_model_settings(config)
    assert settings["anthropic_thinking"] == {"type": "enabled", "budget_tokens": 8192}


def test_effort_settings_kimi_mapping():
    from vestigo.agent.runtime import effort_model_settings

    expected = {"low": "low", "medium": "high", "high": "high", "max": "max"}
    for effort, kimi_effort in expected.items():
        config = _agent_config(
            provider="anthropic",
            api_base_url="https://api.kimi.com/coding",
            reasoning_effort=effort,
        )
        settings = effort_model_settings(config)
        assert settings["extra_body"] == {"reasoning_effort": kimi_effort}

    off_config = _agent_config(
        provider="anthropic",
        api_base_url="https://api.kimi.com/coding",
        reasoning_effort="off",
    )
    assert effort_model_settings(off_config) is None


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


async def test_delete_conversation_removes_proposals(store):
    """Conversation delete cascades to its proposals, not just its messages."""
    await store.init_schema()
    conv = await store.create_agent_conversation("c1", "t1", "u1", model_id="m")
    await store.add_agent_message(conv.id, "user", "q")
    await store.create_agent_proposal(
        case_id="c1",
        timeline_id="t1",
        conversation_id=conv.id,
        tag="t",
        comment=None,
        rationale="",
        events=[{"source_id": "s1", "event_id": "e1"}],
    )
    assert await store.delete_agent_conversation("c1", conv.id) is True
    assert await store.list_agent_messages(conv.id) == []
    assert await store.list_agent_proposals(conv.id) == []


# ---------------------------------------------------------------------------
# Agent v2: tool toggles, /api/agent info + preferences, thinking, export,
# auto-compaction
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolver_agent_v2_fields(store, monkeypatch):
    """context_window / compact_threshold / disabled_tools resolve env→db→default."""
    monkeypatch.setenv("VESTIGO_AGENT_CONTEXT_WINDOW", "200000")
    monkeypatch.setenv("VESTIGO_AGENT_DISABLED_TOOLS", '["histogram"]')
    get_settings.cache_clear()
    try:
        config = await resolve_agent_config()
        assert config.context_window == 200000
        assert config.sources["context_window"] == "env"
        assert config.disabled_tools == ["histogram"]
        assert config.sources["disabled_tools"] == "env"
        # No env/db value: threshold falls back to its hardcoded default.
        assert config.compact_threshold == 0.85
        assert config.sources["compact_threshold"] == "default"
    finally:
        get_settings.cache_clear()


def test_admin_agent_settings_toggles_roundtrip(client, admin_bootstrap, store):
    from vestigo.agent.tools import TOOL_REGISTRY

    as_admin(client, admin_bootstrap)
    body = client.get("/api/admin/agent-settings").json()
    assert len(body["tools"]) == len(TOOL_REGISTRY)
    names = {t["name"] for t in body["tools"]}
    assert {"search_events", "propose_annotation", "semantic_search"} <= names

    put = client.put(
        "/api/admin/agent-settings",
        json={
            "disabled_tools": ["semantic_search", "similar_events"],
            "context_window": 128000,
            "compact_threshold": 0.5,
        },
    )
    assert put.status_code == 200, put.text
    effective = put.json()["effective"]
    assert effective["disabled_tools"] == ["semantic_search", "similar_events"]
    assert effective["context_window"] == 128000
    assert effective["compact_threshold"] == 0.5

    bad = client.put("/api/admin/agent-settings", json={"disabled_tools": ["not_a_tool"]})
    assert bad.status_code == 422


def test_create_conversation_with_disabled_tools(client, admin_bootstrap, agent_on):
    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations",
        json={"timeline_id": timeline_id, "disabled_tools": ["histogram"]},
    ).json()
    assert conversation["disabled_tools"] == ["histogram"]

    bad = client.post(
        f"/api/cases/{case_id}/agent/conversations",
        json={"timeline_id": timeline_id, "disabled_tools": ["not_a_tool"]},
    )
    assert bad.status_code == 422


def test_send_message_composes_admin_and_chat_disabled_tools(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """The turn's scope carries admin-denied ∪ conversation-denied tools."""
    from mcp.server.fastmcp import FastMCP

    from vestigo.agent import runtime

    monkeypatch.setenv("VESTIGO_AGENT_DISABLED_TOOLS", '["semantic_search"]')
    get_settings.cache_clear()

    captured: dict[str, object] = {}

    def spy_build_tool_server(scope):
        captured["disabled"] = scope.disabled_tools
        return FastMCP("stub")

    monkeypatch.setattr(runtime, "build_tool_server", spy_build_tool_server)

    async def model_stream(messages, info):
        yield "ok"

    from pydantic_ai.models.function import FunctionModel

    monkeypatch.setattr(
        runtime,
        "build_model",
        lambda config=None, http_client=None: FunctionModel(stream_function=model_stream),
    )

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations",
        json={"timeline_id": timeline_id, "disabled_tools": ["histogram"]},
    ).json()
    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "hi"},
    )
    assert resp.status_code == 200
    assert captured["disabled"] == frozenset({"semantic_search", "histogram"})


def test_agent_info_503_when_unconfigured(client, admin_bootstrap):
    as_admin(client, admin_bootstrap)
    get_settings.cache_clear()
    assert client.get("/api/agent/info").status_code == 503


def test_agent_info_and_preferences(client, admin_bootstrap, agent_on, monkeypatch):
    as_admin(client, admin_bootstrap)
    monkeypatch.setenv("VESTIGO_AGENT_DISABLED_TOOLS", '["histogram"]')
    get_settings.cache_clear()

    info = client.get("/api/agent/info")
    assert info.status_code == 200, info.text
    body = info.json()
    # The OPSEC notice's data: model + endpoint visible, key never.
    assert body["model"] == "test-model"
    assert body["api_base_url"] == "http://localhost:9/v1"
    assert "api_key" not in json.dumps(body)
    histogram = next(t for t in body["tools"] if t["name"] == "histogram")
    assert histogram["admin_disabled"] is True
    search = next(t for t in body["tools"] if t["name"] == "search_events")
    assert search["admin_disabled"] is False
    assert body["user_disabled_tools"] == []

    put = client.put("/api/agent/preferences", json={"disabled_tools": ["semantic_search"]})
    assert put.status_code == 200, put.text
    assert put.json()["disabled_tools"] == ["semantic_search"]
    assert client.get("/api/agent/info").json()["user_disabled_tools"] == ["semantic_search"]

    bad = client.put("/api/agent/preferences", json={"disabled_tools": ["not_a_tool"]})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_stream_turn_maps_thinking_events(monkeypatch):
    """ThinkingPart deltas stream as thinking_delta; PartEndEvent flushes the
    completed segment as a terminal thinking event."""
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.models.function import DeltaThinkingPart, FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: FastMCP("stub"))

    async def model_stream(messages, info):
        yield {0: DeltaThinkingPart(content="the analyst wants ")}
        yield {0: DeltaThinkingPart(content="anomalies")}
        yield "here is my answer"

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
        scope, user_text="hi", history=[], model=FunctionModel(stream_function=model_stream)
    ):
        events.append(event)

    deltas = "".join(e["text"] for e in events if e["type"] == "thinking_delta")
    assert deltas == "the analyst wants anomalies"
    thinking = [e for e in events if e["type"] == "thinking"]
    assert [e["text"] for e in thinking] == ["the analyst wants anomalies"]
    assert "".join(e["text"] for e in events if e["type"] == "text_delta") == "here is my answer"
    assert events[-1]["type"] == "result"


def _sse_events(resp) -> list[dict]:
    return [
        json.loads(line[len("data: ") :])
        for line in resp.text.splitlines()
        if line.startswith("data: ")
    ]


def test_send_message_persists_thinking_rows(client, admin_bootstrap, agent_on, store, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.models.function import DeltaThinkingPart, FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: FastMCP("stub"))

    async def model_stream(messages, info):
        yield {0: DeltaThinkingPart(content="reasoning here")}
        yield "the answer"

    monkeypatch.setattr(
        runtime,
        "build_model",
        lambda config=None, http_client=None: FunctionModel(stream_function=model_stream),
    )

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "why"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    assert any(e["type"] == "thinking_delta" for e in events)
    assert any(e["type"] == "thinking" and e["text"] == "reasoning here" for e in events)

    async def _fetch():
        return await store.list_agent_messages(conversation["id"])

    messages = asyncio.run(_fetch())
    roles = [m.role for m in messages]
    assert roles.index("thinking") < roles.index("assistant")
    thinking_row = next(m for m in messages if m.role == "thinking")
    assert thinking_row.content == "reasoning here"


def test_export_conversation(client, admin_bootstrap, agent_on, store, monkeypatch):
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.models.function import FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: FastMCP("stub"))

    async def model_stream(messages, info):
        yield "exported answer"

    monkeypatch.setattr(
        runtime,
        "build_model",
        lambda config=None, http_client=None: FunctionModel(stream_function=model_stream),
    )

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "question"},
    )

    resp = client.get(f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/export")
    assert resp.status_code == 200, resp.text
    assert "attachment" in resp.headers["content-disposition"]
    body = resp.json()
    assert body["export_version"] == 1
    assert body["conversation"]["id"] == conversation["id"]
    roles = [m["role"] for m in body["messages"]]
    assert "user" in roles and "assistant" in roles
    # raw_history: the provider-wire pydantic-ai blob survives in the export.
    assert body["raw_history"], "expected the replayable history blob"
    assert body["vestigo_version"]

    async def _audit():
        return await store.query_audit(case_id=case_id)

    assert any(a.action == "agent.conversation_export" for a in asyncio.run(_audit()))

    # Export works while the agent endpoint is down (no _require_agent gate).
    get_settings.cache_clear()

    # Conversations are personal: another admin gets a 404, not the export.
    client.post(
        "/api/admin/users",
        json={"username": "analyst9", "password": "abcdefgh12", "is_admin": True},
    )
    other = client.__class__(client.app)
    login(other, "analyst9", "abcdefgh12")
    assert (
        other.get(f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/export")
    ).status_code == 404


def _seed_long_history(
    store, conversation_id: str, *, last_prompt_tokens: int | None = None, turns: int = 3
):
    """``turns`` user turns of replayable history (+ optionally a measured assistant row)."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    from vestigo.agent.runtime import dump_history

    history = []
    for i in range(turns):
        history.append(ModelRequest(parts=[UserPromptPart(content=f"question {i}")]))
        history.append(ModelResponse(parts=[TextPart(content=f"answer {i}")]))

    async def _seed():
        await store.update_agent_conversation(conversation_id, history=dump_history(history))
        if last_prompt_tokens is not None:
            await store.add_agent_message(
                conversation_id,
                "assistant",
                "prior answer",
                prompt_tokens=last_prompt_tokens,
                completion_tokens=10,
            )

    asyncio.run(_seed())


def _thinking_free_model(monkeypatch, *, fail_first_stream: str | None = None, fail_count: int = 1):
    """Patch build_model with a FunctionModel serving both the turn (stream)
    and the compaction summarizer (non-stream). Optionally the first
    ``fail_count`` stream calls raise ModelHTTPError with the given body."""
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from vestigo.agent import runtime

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: FastMCP("stub"))
    calls = {"stream": 0}

    async def model_stream(messages, info):
        calls["stream"] += 1
        if fail_first_stream is not None and calls["stream"] <= fail_count:
            raise ModelHTTPError(status_code=400, model_name="m", body=fail_first_stream)
        yield "post-compaction answer"

    async def model_call(messages, info):
        return ModelResponse(parts=[TextPart(content="a dense summary of turns 0-1")])

    model = FunctionModel(model_call, stream_function=model_stream)
    monkeypatch.setattr(runtime, "build_model", lambda config=None, http_client=None: model)
    return calls


def _tool_calling_model(monkeypatch, *, overflow_body: str, fail_count: int):
    """A model whose turn actually calls a fidelity-tiered tool.

    The tier drop is only a lever when the attempt fetched event records
    through one of ``FIDELITY_TIERED_TOOLS`` — with `_thinking_free_model`'s
    tool-free stub the router correctly refuses to spend a retry on it. Each
    attempt here runs the same two requests: one that calls
    ``run_anomaly_detector``, then one that either overflows or answers.
    """
    from mcp.server.fastmcp import FastMCP
    from pydantic_ai.exceptions import ModelHTTPError
    from pydantic_ai.messages import ModelResponse, TextPart, ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from vestigo.agent import runtime

    server = FastMCP("stub")

    @server.tool()
    def run_anomaly_detector(detector: str = "value_novelty") -> dict:
        return {"status": "ok", "results": [], "fidelity": "full"}

    monkeypatch.setattr(runtime, "build_tool_server", lambda scope: server)
    calls = {"stream": 0, "failures": 0}

    def _tool_ran(messages) -> bool:
        return any(
            isinstance(part, ToolReturnPart) for message in messages for part in message.parts
        )

    async def model_stream(messages, info):
        calls["stream"] += 1
        if not _tool_ran(messages):
            yield {0: DeltaToolCall(name="run_anomaly_detector", json_args="{}")}
            return
        if calls["failures"] < fail_count:
            calls["failures"] += 1
            raise ModelHTTPError(status_code=400, model_name="m", body=overflow_body)
        yield "answer after the retry"

    async def model_call(messages, info):
        return ModelResponse(parts=[TextPart(content="a dense summary of turns 0-1")])

    model = FunctionModel(model_call, stream_function=model_stream)
    monkeypatch.setattr(runtime, "build_model", lambda config=None, http_client=None: model)
    return calls


def test_send_message_compacts_at_threshold(client, admin_bootstrap, agent_on, store, monkeypatch):
    from vestigo.agent.compaction import COMPACTION_MARKER

    monkeypatch.setenv("VESTIGO_AGENT_CONTEXT_WINDOW", "2048")
    get_settings.cache_clear()
    _thinking_free_model(monkeypatch)

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    # Last measured prompt already far beyond 0.85 × 2048.
    _seed_long_history(store, conversation["id"], last_prompt_tokens=5000)

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "keep going"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    compaction = next(e for e in events if e["type"] == "compaction")
    assert compaction["reason"] == "threshold"
    assert "summary" in compaction and compaction["summary"]
    assert any(e["type"] == "done" for e in events)

    async def _state():
        conv = await store.get_agent_conversation(case_id, conversation["id"])
        msgs = await store.list_agent_messages(conversation["id"])
        audit = await store.query_audit(case_id=case_id)
        return conv, msgs, audit

    conv, msgs, audit = asyncio.run(_state())
    row = next(m for m in msgs if m.role == "compaction")
    assert row.content == "a dense summary of turns 0-1"
    assert row.tool_result["reason"] == "threshold"
    # 3 user turns, 2 kept: only the first turn's 2 messages get folded.
    assert row.tool_result["messages_summarized"] == 2
    # Forensic trail: the exact pre-compaction wire blob rides on the row.
    assert len(row.tool_result["pre_compaction_history"]) == 6
    assert COMPACTION_MARKER in json.dumps(conv.history)
    assert any(a.action == "agent.compaction" for a in audit)


def test_send_message_overflow_compacts_and_retries(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """A provider 400 mentioning context length triggers compact-then-retry —
    even without a configured context_window.

    The stub turn calls no tool, so the tier drop is not a lever here (see
    test_overflow_without_a_tiered_tool_goes_straight_to_compaction) and
    compaction is the first thing tried."""
    calls = _thinking_free_model(monkeypatch, fail_first_stream="maximum context length exceeded")

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    _seed_long_history(store, conversation["id"])

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "keep going"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    compaction = next(e for e in events if e["type"] == "compaction")
    assert compaction["reason"] == "overflow"
    done = next(e for e in events if e["type"] == "done")
    assert done["content"] == "post-compaction answer"
    assert calls["stream"] == 2


def test_overflow_drops_fidelity_before_spending_a_summarizer_call(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """Cheapest lever first.

    Compaction summarizes *older* turns, so it cannot help a single broad turn
    — the case that actually overflowed a 64k model (2026-07-20). Handing the
    model less of each example record can, costs no LLM call, and re-runs the
    tools through a fresh server rather than rewriting anything in history."""
    calls = _tool_calling_model(
        monkeypatch, overflow_body="maximum context length exceeded", fail_count=1
    )

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    # Long enough that compaction *would* have been possible — the point is
    # that it is not what gets tried first.
    _seed_long_history(store, conversation["id"])

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "keep going"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    fidelity = next(e for e in events if e["type"] == "fidelity")
    # Default is "full", so one drop lands on "message".
    assert fidelity["fidelity"] == "message"
    assert fidelity["reason"] == "overflow"
    assert not any(e["type"] == "compaction" for e in events)
    assert next(e for e in events if e["type"] == "done")["content"] == "answer after the retry"
    # Two requests per attempt (tool call, then answer) across two attempts.
    assert calls["stream"] == 4

    # The SSE event is gone on reload, so the drop is persisted the way a
    # compaction is: a marker row (which also separates the re-run's tool rows
    # from the failed attempt's) plus an audit row.
    async def _record():
        return (
            await store.list_agent_messages(conversation["id"]),
            await store.query_audit(case_id=case_id, action="agent.fidelity_drop"),
        )

    messages, audit = asyncio.run(_record())
    markers = [m for m in messages if m.role == "fidelity"]
    assert [m.tool_result for m in markers] == [
        {"from": "full", "to": "message", "attempt": 0, "reason": "overflow"}
    ]
    assert "context window" in markers[0].content
    assert [row.detail for row in audit] == [markers[0].tool_result]


def test_overflow_without_a_tiered_tool_goes_straight_to_compaction(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """A drop that cannot change the prompt is not a lever.

    When the overflowed attempt fetched no event records, re-sending a
    byte-identical request would only delay the compaction that can actually
    help — so no `fidelity` event is emitted at all, even from the default
    `full`."""
    calls = _thinking_free_model(monkeypatch, fail_first_stream="maximum context length exceeded")

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    _seed_long_history(store, conversation["id"])

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "keep going"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    assert not any(e["type"] == "fidelity" for e in events)
    assert next(e for e in events if e["type"] == "compaction")["reason"] == "overflow"
    assert calls["stream"] == 2

    # A drop that never happened must not leave a record saying it did.
    async def _record():
        return (
            await store.list_agent_messages(conversation["id"]),
            await store.query_audit(case_id=case_id, action="agent.fidelity_drop"),
        )

    messages, audit = asyncio.run(_record())
    assert not [m for m in messages if m.role == "fidelity"]
    assert audit == []


def test_send_message_overflow_with_nothing_to_compact(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """Overflow on an already-short conversation degrades to a specific,
    friendly error — not the generic 'see server logs'. The stub turn calls no
    tool, so there is no fidelity lever to spend either."""
    _thinking_free_model(monkeypatch, fail_first_stream="prompt is too long: maximum tokens")

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "hello"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    error = next(e for e in events if e["type"] == "error")
    assert error["code"] == "context_overflow"
    assert "context window" in error["detail"]
    assert not any(e["type"] == "compaction" for e in events)


def test_is_context_overflow_matches_known_phrasings_only():
    """The overflow heuristic must not fire on unrelated 400s — a false
    positive burns a summarizer call and shows a misleading error."""
    from pydantic_ai.exceptions import ModelHTTPError

    from vestigo.api.routers.agent import _is_context_overflow

    def _exc(body: str, status: int = 400) -> ModelHTTPError:
        return ModelHTTPError(status_code=status, model_name="m", body=body)

    # Real overflow phrasings across providers.
    assert _is_context_overflow(_exc("This model's maximum context length is 8192 tokens"))
    assert _is_context_overflow(_exc("prompt is too long: 210000 tokens > 200000 maximum"))
    assert _is_context_overflow(_exc('{"code": "context_length_exceeded"}'))
    assert _is_context_overflow(_exc("input is too long for requested model", status=413))
    assert _is_context_overflow(_exc("request exceeds the token limit"))
    # LiteLLM proxy wording, observed against a local model 2026-07-20. This
    # one was a miss: it skipped compact-and-retry and lost the turn.
    assert _is_context_overflow(
        _exc(
            "litellm.BadRequestError: Custom_openaiException - request (81855 tokens) "
            "exceeds the available context size (65536 tokens), try increasing it."
        )
    )
    # Unrelated 400s that share individual words.
    assert not _is_context_overflow(_exc("Invalid token provided"))
    assert not _is_context_overflow(_exc("max_tokens must be greater than 0"))
    assert not _is_context_overflow(_exc("maximum temperature is 2.0"))
    assert not _is_context_overflow(_exc("field 'length' is required"))
    # Overflow wording on a non-overflow status stays a model_error.
    assert not _is_context_overflow(_exc("maximum context length exceeded", status=500))


def test_send_message_second_overflow_folds_to_one_turn(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """Overflow → compact keeping 2 turns → overflow again → compact keeping
    1 turn → success. Both compactions land on the record with their
    keep_turns, and re-run tool audit rows would carry the attempt tag.

    The stub turn calls no tool, so the compaction schedule is the whole
    ladder here; the tier drops are covered by the escalation test below."""
    calls = _thinking_free_model(
        monkeypatch, fail_first_stream="maximum context length exceeded", fail_count=2
    )

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    _seed_long_history(store, conversation["id"], turns=4)

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "keep going"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    compactions = [e for e in events if e["type"] == "compaction"]
    assert [e["reason"] for e in compactions] == ["overflow", "overflow"]
    done = next(e for e in events if e["type"] == "done")
    assert done["content"] == "post-compaction answer"
    assert calls["stream"] == 3

    async def _rows():
        return await store.list_agent_messages(conversation["id"])

    rows = [m for m in asyncio.run(_rows()) if m.role == "compaction"]
    assert [r.tool_result["keep_turns"] for r in rows] == [2, 1]


def test_send_message_third_overflow_gives_up(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """After every escalation step the turn fails with the friendly
    context_overflow error instead of retrying forever.

    The stub turn calls no tool, so only the compaction steps remain; the full
    ladder (two tier drops, then two compactions) is covered by
    test_overflow_exhausts_every_lever_in_order."""
    calls = _thinking_free_model(
        monkeypatch, fail_first_stream="maximum context length exceeded", fail_count=3
    )

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    _seed_long_history(store, conversation["id"], turns=4)

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "keep going"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    assert sum(e["type"] == "compaction" for e in events) == 2
    error = next(e for e in events if e["type"] == "error")
    assert error["code"] == "context_overflow"
    assert calls["stream"] == 3


def test_overflow_exhausts_every_lever_in_order(
    client, admin_bootstrap, agent_on, store, monkeypatch
):
    """From the default `full`: two tier drops (no LLM call each), then the
    two compactions, then the friendly error — cheapest lever first, and the
    turn is never lost to a raw crash."""
    calls = _tool_calling_model(
        monkeypatch, overflow_body="maximum context length exceeded", fail_count=5
    )

    as_admin(client, admin_bootstrap)
    case_id, timeline_id = _make_case_and_timeline(client)
    conversation = client.post(
        f"/api/cases/{case_id}/agent/conversations", json={"timeline_id": timeline_id}
    ).json()
    _seed_long_history(store, conversation["id"], turns=4)

    resp = client.post(
        f"/api/cases/{case_id}/agent/conversations/{conversation['id']}/messages",
        json={"content": "keep going"},
    )
    assert resp.status_code == 200
    events = _sse_events(resp)
    ladder = [e["type"] for e in events if e["type"] in {"fidelity", "compaction", "error"}]
    assert ladder == ["fidelity", "fidelity", "compaction", "compaction", "error"]
    assert [e["fidelity"] for e in events if e["type"] == "fidelity"] == ["message", "minimal"]
    assert next(e for e in events if e["type"] == "error")["code"] == "context_overflow"
    # Five attempts, each spending a tool-call request and an answer request.
    assert calls["stream"] == 10


@pytest.mark.asyncio
async def test_get_last_agent_usage_ignores_pre_compaction_rows(store):
    """Usage measured before a compaction describes the pre-compaction
    history size — trusting it would re-compact an already-small history."""
    await store.init_schema()
    await store.create_case("c1", "Case 1")
    conv = await store.create_agent_conversation("c1", "t1", "u1")

    await store.add_agent_message(
        conv.id, "assistant", "big answer", prompt_tokens=150_000, completion_tokens=900
    )
    assert await store.get_last_agent_usage(conv.id) == (150_000, 900)

    await store.add_agent_message(conv.id, "compaction", "summary", tool_result={"reason": "x"})
    assert await store.get_last_agent_usage(conv.id) == (None, None)

    await store.add_agent_message(
        conv.id, "assistant", "small answer", prompt_tokens=4_000, completion_tokens=200
    )
    assert await store.get_last_agent_usage(conv.id) == (4_000, 200)


@pytest.mark.asyncio
async def test_get_last_agent_usage_ignores_pre_fidelity_drop_rows(store):
    """A tier drop invalidates a measurement for the same reason a compaction
    does: the next request is a different size than the number describes —
    every tool result from here on carries less. Trusting it would spend a
    summarizer call the drop had already made unnecessary."""
    await store.init_schema()
    await store.create_case("c1", "Case 1")
    conv = await store.create_agent_conversation("c1", "t1", "u1")

    await store.add_agent_message(
        conv.id, "assistant", "big answer", prompt_tokens=60_000, completion_tokens=400
    )
    assert await store.get_last_agent_usage(conv.id) == (60_000, 400)

    await store.add_agent_message(
        conv.id,
        "fidelity",
        "reduced from full to message",
        tool_result={"from": "full", "to": "message", "attempt": 0, "reason": "overflow"},
    )
    assert await store.get_last_agent_usage(conv.id) == (None, None)
