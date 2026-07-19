"""pydantic-ai runtime for the investigation agent.

Builds the LLM model from ``VESTIGO_AGENT_*`` settings, connects the
scope-bound MCP tool server, and streams one conversation turn as a sequence
of plain dict events the router forwards over SSE.

Kimi coding-plan compatibility (verified against the hermes-agent source and
Kimi CLI docs, see docs/AGENT.md): the ``https://api.kimi.com/coding``
endpoint speaks the Anthropic Messages protocol but (a) rejects requests
whose User-Agent doesn't identify a coding agent — handled via
``agent_user_agent`` — and (b) with server-side thinking active requires
replayed assistant tool-call messages to carry a thinking block, unsigned
(``reasoning_content`` semantics, hermes-agent#13848). Stock pydantic-ai only
replays *signed* thinking blocks, so :class:`KimiAnthropicModel` re-injects
unsigned ones. The Anthropic ``thinking`` request parameter is never sent
(pydantic-ai omits it unless explicitly configured), matching hermes'
behavior for this endpoint.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from fastmcp.client import Client as FastMCPClient
from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    PartDeltaEvent,
    TextPartDelta,
)
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.usage import UsageLimits

from vestigo.agent.availability import probe_headers
from vestigo.agent.tools import AgentScope, build_tool_server
from vestigo.core.config import Settings, get_settings

_LLM_TIMEOUT = 300.0

SYSTEM_PROMPT = """\
You are a forensic log-investigation assistant embedded in Vestigo, working on
one case timeline. You have read-only MCP tools to search events, inspect
field distributions, run statistical anomaly detectors, and (when available)
semantic search.

Method: work iteratively — inspect available fields and artifacts first,
aggregate before you page (field_terms and histogram beat reading raw
events), refine filters step by step, and verify a hypothesis against the
data before reporting it.

When you have distilled a result worth the analyst's attention, call
propose_finding with a title, a short explanation, and the exact filter spec
that reproduces it — the analyst gets a card with an "apply to Explorer"
button. Propose only filters you have actually run. Cite event_ids and
counts in your prose. Never invent data; if the tools return nothing
conclusive, say so.
"""


def _is_kimi_coding_endpoint(base_url: str | None) -> bool:
    """True for Kimi's coding-plan endpoint (Anthropic protocol, UA-gated)."""
    if not base_url:
        return False
    try:
        parsed = urlparse(base_url)
    except ValueError:
        return False
    host = (parsed.hostname or "").lower()
    return host == "api.kimi.com" and parsed.path.rstrip("/").startswith("/coding")


class KimiAnthropicModel(AnthropicModel):
    """AnthropicModel with Kimi /coding replay semantics (see module docstring)."""

    async def _map_message(self, messages, model_request_parameters, model_settings):  # type: ignore[override]
        system_prompt, anthropic_messages = await super()._map_message(
            messages, model_request_parameters, model_settings
        )
        for message in anthropic_messages:
            content = message.get("content")
            if message.get("role") != "assistant" or not isinstance(content, list):
                continue
            has_tool_use = any(isinstance(b, dict) and b.get("type") == "tool_use" for b in content)
            has_thinking = any(
                isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking")
                for b in content
            )
            if has_tool_use and not has_thinking:
                # Kimi accepts (and, with server-side thinking, requires) an
                # unsigned thinking block; it must precede text/tool_use.
                content.insert(0, {"type": "thinking", "thinking": "", "signature": ""})
        return system_prompt, anthropic_messages


def build_model(settings: Settings | None = None) -> Model:
    """Build the pydantic-ai model from ``VESTIGO_AGENT_*`` settings."""
    settings = settings or get_settings()
    if not settings.agent_model:
        raise RuntimeError("VESTIGO_AGENT_MODEL is not configured")
    http_client = httpx.AsyncClient(headers=probe_headers(settings), timeout=_LLM_TIMEOUT)
    if settings.agent_provider == "anthropic":
        provider = AnthropicProvider(
            api_key=settings.agent_api_key,
            base_url=settings.agent_api_base_url,
            http_client=http_client,
        )
        model_cls = (
            KimiAnthropicModel
            if _is_kimi_coding_endpoint(settings.agent_api_base_url)
            else AnthropicModel
        )
        return model_cls(settings.agent_model, provider=provider)
    provider = OpenAIProvider(
        base_url=settings.agent_api_base_url,
        # The OpenAI SDK insists on a key even for keyless local endpoints
        # (ollama, vllm) — send a placeholder there.
        api_key=settings.agent_api_key or "unused",
        http_client=http_client,
    )
    return OpenAIChatModel(settings.agent_model, provider=provider)


@dataclass
class TurnResult:
    """Outcome of one streamed turn: the model's final text + full new history."""

    output_text: str
    new_messages: list[ModelMessage]


def dump_history(messages: list[ModelMessage]) -> list[Any]:
    """Serialize pydantic-ai message history to JSON-safe data."""
    return json.loads(ModelMessagesTypeAdapter.dump_json(messages))


def load_history(data: list[Any] | None) -> list[ModelMessage]:
    """Deserialize stored message history."""
    if not data:
        return []
    return list(ModelMessagesTypeAdapter.validate_python(data))


def _view_context(view_filters: dict[str, Any] | None) -> str:
    if not view_filters:
        return "The analyst's Explorer view currently has no filters applied."
    return (
        "The analyst's Explorer view currently applies these filters "
        f"(JSON): {json.dumps(view_filters, default=str)}"
    )


async def stream_turn(
    scope: AgentScope,
    *,
    user_text: str,
    history: list[ModelMessage],
    view_filters: dict[str, Any] | None = None,
    model: Model | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Run one agent turn, yielding SSE-ready event dicts.

    The final yielded event has ``type="result"`` and carries the
    :class:`TurnResult` under ``"turn"`` (consumed by the router, not
    forwarded to the client).
    """
    settings = get_settings()
    model = model or build_model(settings)
    server = build_tool_server(scope)
    toolset = MCPToolset(FastMCPClient(server), id="vestigo")
    agent = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        toolsets=[toolset],
    )
    limits = UsageLimits(request_limit=settings.agent_max_turns)

    context = (
        f"Case: {scope.case_id}. Timeline: {scope.timeline_id} "
        f"({len(scope.source_ids)} sources). {_view_context(view_filters)}\n\n{user_text}"
    )

    async with agent.run_stream_events(
        context, message_history=history or None, usage_limits=limits
    ) as stream:
        async for event in stream:
            if isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                if event.delta.content_delta:
                    yield {"type": "text_delta", "text": event.delta.content_delta}
            elif isinstance(event, FunctionToolCallEvent):
                yield {
                    "type": "tool_call",
                    "tool_call_id": event.part.tool_call_id,
                    "tool": event.part.tool_name,
                    "args": event.part.args_as_dict(),
                }
            elif isinstance(event, FunctionToolResultEvent):
                content = event.part.content
                summary = content if isinstance(content, (dict, list)) else str(content)
                yield {
                    "type": "tool_result",
                    "tool_call_id": event.part.tool_call_id,
                    "tool": event.part.tool_name,
                    "result": summary,
                }
            elif isinstance(event, AgentRunResultEvent):
                result = event.result
                yield {
                    "type": "result",
                    "turn": TurnResult(
                        output_text=str(result.output),
                        new_messages=result.new_messages(),
                    ),
                }
