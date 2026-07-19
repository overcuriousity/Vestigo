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
unsigned ones. The Anthropic ``thinking`` request parameter itself is still
never sent by pydantic-ai for this endpoint; when reasoning effort is
configured (``reasoning_effort`` != ``"off"``), :func:`effort_model_settings`
instead sends a top-level ``reasoning_effort`` field via ``extra_body`` (see
that function's docstring for the wire-field verification).
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
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import UsageLimits

from vestigo.agent.availability import probe_headers
from vestigo.agent.config import AgentConfig, resolve_agent_config
from vestigo.agent.tools import AgentScope, build_tool_server

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


_ANTHROPIC_THINKING_BUDGETS = {"low": 2048, "medium": 8192, "high": 24576, "max": 32768}

# Kimi's own effort vocabulary is coarser (low/high/max) than Vestigo's
# closed enum (off/low/medium/high/max); "medium" and "high" both collapse
# to Kimi's "high" tier. See effort_model_settings() docstring for sourcing.
_KIMI_EFFORT = {"low": "low", "medium": "high", "high": "high", "max": "max"}


def effort_model_settings(config: AgentConfig) -> ModelSettings | None:
    """Translate the closed reasoning-effort enum into provider model settings.

    ``config.reasoning_effort`` is one of ``EFFORT_VALUES``
    (``vestigo.agent.config``): ``"off"`` (no reasoning-effort setting is
    sent — current/default behavior), ``"low"``, ``"medium"``, ``"high"``,
    ``"max"``. See the effort table in docs/AGENT.md for the full per-provider
    mapping.

    - OpenAI-protocol endpoints: passed verbatim as
      ``OpenAIChatModelSettings(openai_reasoning_effort=effort)`` (pydantic-ai
      forwards it as the OpenAI ``reasoning_effort`` request field).
    - Anthropic-protocol endpoints (non-Kimi): translated to an explicit
      thinking-token budget via ``AnthropicModelSettings(anthropic_thinking=...)``
      since stock Anthropic has no discrete effort enum, only a token budget.
    - Kimi's ``https://api.kimi.com/coding`` endpoint (also Anthropic-protocol
      on the wire): Kimi's own K3 API takes a coarser, discrete
      ``low``/``high``/``max`` effort tier via a **top-level `reasoning_effort`
      field in the JSON request body** (not the Anthropic `thinking` object).
      Verified against: platform.kimi.ai's "Thinking Effort" guide
      (https://platform.kimi.ai/docs/guide/use-thinking-effort), which shows
      `reasoning_effort` as a top-level request field for kimi-k3 on Kimi's
      chat-completions API and calls out explicit OpenAI `reasoning_effort`
      compatibility; and Kimi Code's "Using in Third-Party Coding Agents" docs
      (https://www.kimi.com/code/docs/en/third-party-tools/other-coding-agents.html),
      which give the exact effort-tier mapping used here for the `/coding`
      agent-facing endpoint: low->low, medium->high, high->high, xhigh->max,
      max->max (Vestigo has no "xhigh" tier, so "max" covers it). Neither
      source shows a raw HTTP request body captured specifically against
      `/coding`'s Anthropic-protocol (`/v1/messages`) path, so the field name
      is corroborated rather than directly observed on that exact route --
      pydantic-ai's ``ModelSettings.extra_body`` merges it into the JSON body
      regardless of protocol, which is the safe construction either way.
      Treat as experimental pending a direct request/response capture.
    """
    effort = config.reasoning_effort
    if effort == "off":
        return None
    if _is_kimi_coding_endpoint(config.api_base_url):
        return ModelSettings(extra_body={"reasoning_effort": _KIMI_EFFORT[effort]})
    if config.provider == "anthropic":
        return AnthropicModelSettings(
            anthropic_thinking={
                "type": "enabled",
                "budget_tokens": _ANTHROPIC_THINKING_BUDGETS[effort],
            }
        )
    return OpenAIChatModelSettings(openai_reasoning_effort=effort)


def build_model(config: AgentConfig) -> Model:
    """Build the pydantic-ai model from a resolved :class:`AgentConfig`."""
    if not config.model:
        raise RuntimeError("VESTIGO_AGENT_MODEL is not configured")
    http_client = httpx.AsyncClient(headers=probe_headers(config), timeout=_LLM_TIMEOUT)
    if config.provider == "anthropic":
        provider = AnthropicProvider(
            api_key=config.api_key,
            base_url=config.api_base_url,
            http_client=http_client,
        )
        model_cls = (
            KimiAnthropicModel if _is_kimi_coding_endpoint(config.api_base_url) else AnthropicModel
        )
        return model_cls(config.model, provider=provider)
    provider = OpenAIProvider(
        base_url=config.api_base_url,
        # The OpenAI SDK insists on a key even for keyless local endpoints
        # (ollama, vllm) — send a placeholder there.
        api_key=config.api_key or "unused",
        http_client=http_client,
    )
    return OpenAIChatModel(config.model, provider=provider)


@dataclass
class TurnResult:
    """Outcome of one streamed turn: final text, new history, measured usage."""

    output_text: str
    new_messages: list[ModelMessage]
    prompt_tokens: int | None = None
    completion_tokens: int | None = None


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
    config = await resolve_agent_config()
    model = model or build_model(config)
    server = build_tool_server(scope)
    toolset = MCPToolset(FastMCPClient(server), id="vestigo")
    agent = Agent(
        model,
        system_prompt=SYSTEM_PROMPT,
        toolsets=[toolset],
        model_settings=effort_model_settings(config),
    )
    limits = UsageLimits(request_limit=config.max_turns)

    context = (
        f"Case: {scope.case_id}. Timeline: {scope.timeline_id} "
        f"({len(scope.source_ids)} sources). {_view_context(view_filters)}\n\n{user_text}"
    )

    async with agent.run_stream_events(
        context, message_history=history or None, usage_limits=limits
    ) as stream:
        async for event in stream:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                # A text part's first chunk arrives in the start event, not as
                # a delta — dropping it clips the opening of every segment.
                if event.part.content:
                    yield {"type": "text_delta", "text": event.part.content}
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
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
                # `AgentRunResult.usage` is a property in pydantic-ai 2.13.0
                # (not the callable `.usage()` some older docs/snippets show).
                usage = result.usage
                yield {
                    "type": "result",
                    "turn": TurnResult(
                        output_text=str(result.output),
                        new_messages=result.new_messages(),
                        # 0 means the endpoint reported nothing — store NULL,
                        # never a fake count.
                        prompt_tokens=usage.input_tokens or None,
                        completion_tokens=usage.output_tokens or None,
                    ),
                }
