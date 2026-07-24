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
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
from fastmcp.client import Client as FastMCPClient
from pydantic_ai import Agent, AgentRunResultEvent
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.openai import OpenAIChatModel, OpenAIChatModelSettings
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import WrapperToolset
from pydantic_ai.usage import UsageLimits

from vestigo.agent.availability import probe_headers
from vestigo.agent.config import AgentConfig, is_kimi_coding_endpoint, resolve_agent_config
from vestigo.agent.tools import (
    RESULT_FORMAT_NOTE,
    SPEC_REFERENCE,
    AgentScope,
    build_tool_server,
)
from vestigo.agent.window import CHARS_PER_TOKEN_DEFAULT, WindowStats, make_window_processor

logger = logging.getLogger(__name__)

LLM_TIMEOUT = 300.0

#: Fraction of the token budget one model request's tool returns may occupy
#: before the request guard starts reducing them. The sliding window reserves
#: the rest for history, the system prompt and the model's own answer; a single
#: turn that fills the whole budget with tool output (three byte-identical
#: ``search_events`` returns did exactly that on 2026-07-23) leaves no room to
#: reason. Half is generous — most turns never approach it.
_TOOL_RETURN_BUDGET_FRACTION = 0.5

_BASE_SYSTEM_PROMPT = """\
You are a forensic log-investigation assistant embedded in Vestigo, working on
one case timeline. You have read-only MCP tools to search events, inspect
field distributions and time series, compare filtered layers, run statistical
anomaly detectors, and (when available) semantic search. Findings you produce
may end up in a forensic report, so every claim must be reproducible by the
analyst from the tool calls you made.

## Evidence rule (highest priority)

Your pretrained knowledge is a source of *questions*, never a source of
*facts about this case*. You do not know what is normal in this environment,
which hosts or accounts are privileged, what an ordinary event volume is, or
whether a value is rare — until you have measured it here. Anything not
returned by a tool in this conversation is an assumption, and assumptions
must be either measured or stated explicitly as unverified.

Concretely, never assert:
- that something is rare, new, unusual, spiking, or off-hours without a
  detector run, field_terms/field_timeseries/histogram/time_punchcard output,
  or a compare against a measured reference window;
- a count, a ratio, a first/last-seen time, or a trend you did not read off a
  tool result;
- that a pattern is "typical attacker behaviour" as if that settled it —
  general threat knowledge may motivate a hypothesis, it may not conclude one.

Prefer the statistical tools over your own eyeballing of raw rows: they
compute over the whole timeline, are recorded with a run_id, and are what
makes the result defensible. Reading a page of events is for confirming a
mechanism, not for establishing a rate.

## Investigation cycle

Work in explicit, repeating cycles. Keep each pass small; end it, then start
the next one.

1. **Observation / problem** — state what prompted this pass: the analyst's
   question, a detector hit, an oddity seen in a prior cycle.
2. **Investigative question** — sharpen it into one answerable question about
   this timeline. One question per cycle.
3. **Source research** — before theorising, learn the terrain and what is
   already known: list_fields, list_artifacts, list_baselines,
   list_saved_views, list_annotations, list_dispositions, list_sigma_rules /
   list_sigma_runs. Do not re-derive what the case already records.
4. **Hypothesis** — a falsifiable statement, phrased so that a specific tool
   output would refute it. "Account X authenticated from a host it never used
   before 2026-03-04" is a hypothesis; "there may be lateral movement" is not.
5. **Method design** — name, before running them, the tools and parameters
   that would test it, and say what result would falsify it. Choose the
   comparison explicitly: self-baseline, a baseline_id from list_baselines,
   or two windows via compare.
6. **Data acquisition** — run it. Aggregate before you page: field_terms,
   field_pivot, histogram, field_timeseries, field_numeric_stats and
   run_anomaly_detector beat reading raw events. Narrow filters stepwise, and
   note the hit counts you get at each step.
7. **Analysis** — read the numbers, not the narrative. Check effect size and
   support, not just presence of a hit; a detector finding on 2 events is
   weak. Consider ingest artefacts, timezone/offset skew, source coverage
   gaps, and duplicate ingestion as competing explanations.
8. **Hypothesis check** — decide: supported, falsified, or underpowered.
   - Falsified → say so plainly, keep the disconfirming evidence, and branch
     to a new hypothesis (back to step 4) or a new question (step 2).
   - Underpowered → design a better method (step 5), do not upgrade a weak
     signal into a conclusion by restating it.
   - Supported → try once to break it: an alternative explanation, a control
     window, or a second, independent detector or field. Only then conclude.
9. **Conclusion** — what is now established, with the evidence and its
   limits.
10. **New cycle** — what the conclusion opens up next. Continue until the
    analyst's question is answered or the data cannot answer it.

## Reporting

Cite event_ids, counts, time ranges, and detector run_ids in your prose;
these are what the analyst re-runs. Separate observation from inference:
"12 043 events, 3 of which carry user=svc_backup" is observation, "svc_backup
was likely used interactively" is inference — label it.

When a filter set isolates something worth attention, call propose_finding
with a title, a short explanation, and the exact filter spec that reproduces
it — the analyst gets a card with an "apply to Explorer" button. Propose only
filters you have actually run. Use propose_chart when the shape of the data
carries the argument.

## Charting

Build a chart the way the analyst does, in this order:

1. Pick the field. `list_fields` includes virtual `time:` fields
   (time:hour_of_day, time:day_of_week, time:month, ...) — use one to put a
   time part on an axis, e.g. country x hour-of-day as a `pivot` heatmap.
2. Call `describe_field` for its scale, rather than guessing. It reports the
   suggested scale and the chart types legal for it.
3. Pick a `chart_type` legal for that scale. An illegal combination is
   rejected with a message naming the alternatives — read it and retry. Watch
   the two grids: `heatmap` is one field over time and takes no `field_y`;
   the field x field heatmap is `pivot`.
4. Add a comparison layer only if you need one; only "time", "bar" and
   "histogram" support it. `compare.mode="baseline"` measures the filtered
   set against the whole timeline.

Metrics other than "count" exist only on `chart_type="time"`. Then read the
`resolved` block in the result — it is what will actually be drawn. Never
describe a chart to the analyst without checking `resolved` matches what you
asked for, and mention anything in `warnings`.

Be concise. State negative results — a falsified hypothesis is a real result
and belongs in the answer. If the tools return nothing conclusive, say so and
name what data would be needed; never fill the gap with plausible
reconstruction.

## Context window

Older tool results in this conversation may be replaced by elision stubs
(`{"elided": true, ...}`) to fit the model's context window; whole older turns
may be replaced by a drop marker. Do not treat an elided result as missing
data — re-run the tool with narrower filters or use get_event for the specific
records you still need, and prefer aggregation tools over bulk event listing so
results stay small.
"""

# The result-format note and the shared spec models' per-field prose are both
# stripped from / absent in the tool schemas, where the prose was being resent
# a dozen times per request, and paid once here instead (A13). Both are
# generated from the tool layer (see agent/schema_slim.py) and shared verbatim
# with the external /mcp instructions, so the two surfaces cannot diverge.
SYSTEM_PROMPT = f"{_BASE_SYSTEM_PROMPT}\n{RESULT_FORMAT_NOTE}\n{SPEC_REFERENCE}"


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
    if is_kimi_coding_endpoint(config.api_base_url):
        return ModelSettings(extra_body={"reasoning_effort": _KIMI_EFFORT[effort]})
    if config.provider == "anthropic":
        return AnthropicModelSettings(
            anthropic_thinking={
                "type": "enabled",
                "budget_tokens": _ANTHROPIC_THINKING_BUDGETS[effort],
            }
        )
    return OpenAIChatModelSettings(openai_reasoning_effort=effort)


def build_model(config: AgentConfig, http_client: httpx.AsyncClient) -> Model:
    """Build the pydantic-ai model from a resolved :class:`AgentConfig`.

    The caller owns ``http_client`` and must close it when the turn is done
    (``stream_turn`` does) — building it here and never closing leaked a
    connection pool per turn.
    """
    if not config.model:
        raise RuntimeError("VESTIGO_AGENT_MODEL is not configured")
    if config.provider == "anthropic":
        provider = AnthropicProvider(
            api_key=config.api_key,
            base_url=config.api_base_url,
            http_client=http_client,
        )
        model_cls = (
            KimiAnthropicModel if is_kimi_coding_endpoint(config.api_base_url) else AnthropicModel
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


class _RequestGuardToolset(WrapperToolset):
    """Per-model-request tool defenses, scoped to one ``run_step``.

    Two things one assistant turn can do that the sliding window cannot undo
    (it only rewrites *older* messages; the newest tool returns are exactly the
    ones it protects):

    * **Duplicate calls.** Three ``search_events`` calls with byte-identical
      arguments returned three byte-identical ~34 KB payloads on 2026-07-23 —
      ~100 KB of pure duplicate in one request. An identical ``(name, args)``
      already answered this request comes back as a back-reference instead.
    * **Runaway total.** Once this request's returns pass a byte ceiling
      derived from the budget, further returns come back reduced with a pointer
      to ``get_event`` and narrower filters.

    Determinism holds: the cache is keyed on canonical arguments and reset when
    ``run_step`` advances, so the same call sequence always produces the same
    results. Both actions are counted on the turn's :class:`WindowStats`, so the
    persisted window row records that they happened — and the reduced/duplicate
    return is itself a ``tool_result`` row in the export.
    """

    def __init__(self, wrapped: Any, *, byte_ceiling: int, stats: WindowStats) -> None:
        super().__init__(wrapped)
        self._byte_ceiling = byte_ceiling
        self._stats = stats
        self._step: int | None = None
        self._seen: dict[tuple[str, str], str] = {}
        self._bytes = 0

    def _reset_for_step(self, step: int) -> None:
        if step != self._step:
            self._step = step
            self._seen = {}
            self._bytes = 0

    async def call_tool(self, name, tool_args, ctx, tool):  # noqa: ANN001 - match base
        self._reset_for_step(ctx.run_step)
        key = (name, json.dumps(tool_args, sort_keys=True, default=str))
        if (first_id := self._seen.get(key)) is not None:
            self._stats.duplicate_calls += 1
            logger.info("Deduped identical %s call within one request", name)
            return {
                "duplicate_of": first_id,
                "note": (
                    f"An identical {name} call was already made this turn — its result "
                    "stands. Change the arguments, or read a specific record with get_event, "
                    "rather than repeating the call."
                ),
            }
        # Only a successful call is cached: a rejected one raises before this
        # line, so the model's corrected retry re-runs rather than being deduped
        # against a call that never produced a result.
        result = await self.wrapped.call_tool(name, tool_args, ctx, tool)
        self._seen[key] = ctx.tool_call_id or key[1]

        size = len(json.dumps(result, default=str))
        # Never reduce the first return of a request — a lone oversized payload
        # is the sliding window's job (truncate), not this guard's; the ceiling
        # is about the *sum* running away across many calls.
        if self._byte_ceiling and self._bytes > 0 and self._bytes + size > self._byte_ceiling:
            self._stats.results_capped += 1
            logger.info(
                "Capped %s return: request already at %d bytes, ceiling %d",
                name,
                self._bytes,
                self._byte_ceiling,
            )
            return {
                "reduced": True,
                "note": (
                    "This turn's tool output already fills its share of the context window, "
                    "so this result was withheld. Narrow the filters, aggregate first "
                    "(field_terms / histogram), or fetch a specific record with get_event."
                ),
            }
        self._bytes += size
        return result


async def stream_turn(
    scope: AgentScope,
    *,
    user_text: str,
    history: list[ModelMessage],
    view_filters: dict[str, Any] | None = None,
    model: Model | None = None,
    window_budget: int | None = None,
    window_stats: WindowStats | None = None,
    chars_per_token: float = CHARS_PER_TOKEN_DEFAULT,
) -> AsyncIterator[dict[str, Any]]:
    """Run one agent turn, yielding SSE-ready event dicts.

    The final yielded event has ``type="result"`` and carries the
    :class:`TurnResult` under ``"turn"`` (consumed by the router, not
    forwarded to the client).

    ``window_budget`` enables the sliding context window (``agent/window.py``)
    on every model request of the turn; the caller's ``window_stats`` collects
    what it did so the router can persist one row per turn.
    ``chars_per_token`` is the estimator's divisor — the default, or a ratio a
    previous overflow measured against the provider's own token count. Fixed
    for the whole turn, so every request reduces the same way.
    """
    config = await resolve_agent_config()
    # When no model is injected (tests), the turn owns an HTTP client that
    # must be closed when the stream ends — see build_model's docstring.
    http_client: httpx.AsyncClient | None = None
    if model is None:
        http_client = httpx.AsyncClient(headers=probe_headers(config), timeout=LLM_TIMEOUT)
        model = build_model(config, http_client)
    try:
        server = build_tool_server(scope)
        stats = window_stats or WindowStats()
        toolset: Any = MCPToolset(FastMCPClient(server), id="vestigo")
        # Per-request tool defenses: collapse identical calls and cap one
        # request's total return bytes. The ceiling is a share of the token
        # budget converted back to bytes with the same divisor the estimator
        # uses, so it scales with the model; with no configured budget there is
        # nothing to derive it from, so the guard only dedupes.
        byte_ceiling = (
            int(window_budget * _TOOL_RETURN_BUDGET_FRACTION * chars_per_token)
            if window_budget is not None
            else 0
        )
        toolset = _RequestGuardToolset(toolset, byte_ceiling=byte_ceiling, stats=stats)
        # retries: a rejected tool call is the agent's version of the Visualize
        # page's dropdown refusing an impossible chart — the error names the
        # legal alternative and is meant to be acted on. pydantic-ai's default
        # of 1 gave a small model one correction attempt, after which the whole
        # turn died with UnexpectedModelBehavior (a propose_chart heatmap/pivot
        # mix-up cost a real turn on 2026-07-20). Three, not more: every retry
        # is also a model request against the `request_limit` below, so a tool
        # the model cannot get right must not eat the investigation's budget.
        # The sliding window rides as a ProcessHistory capability: it rewrites
        # what each model request carries (mid-turn included) while the run's
        # recorded history — and therefore the stored blob — stays complete.
        capabilities = []
        if window_budget is not None:
            capabilities.append(
                ProcessHistory(make_window_processor(window_budget, stats, chars_per_token))
            )
        agent = Agent(
            model,
            system_prompt=SYSTEM_PROMPT,
            toolsets=[toolset],
            model_settings=effort_model_settings(config),
            retries=3,
            capabilities=capabilities,
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
                elif isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
                    if event.part.content:
                        yield {"type": "thinking_delta", "text": event.part.content}
                elif isinstance(event, PartDeltaEvent) and isinstance(
                    event.delta, ThinkingPartDelta
                ):
                    if event.delta.content_delta:
                        yield {"type": "thinking_delta", "text": event.delta.content_delta}
                elif isinstance(event, PartEndEvent) and isinstance(event.part, ThinkingPart):
                    # The end event carries the fully-assembled part, so each
                    # completed thinking segment (there can be several per turn,
                    # interleaved with tool calls) flushes without manual
                    # buffering. Signatures stay in the history blob only.
                    if event.part.content:
                        yield {"type": "thinking", "text": event.part.content}
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
    finally:
        if http_client is not None:
            await http_client.aclose()
