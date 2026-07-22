"""Read-only investigation tools for the AI agent, exposed as an MCP server.

The tools are defined once on a standard MCP server (``mcp.server.fastmcp``)
so the same definitions serve the built-in pydantic-ai loop today (in-memory
transport) and an externally mounted MCP endpoint later (roadmap). Every tool
is scoped to one case + timeline at server-build time — the model never
supplies IDs, so it can never read outside the conversation's scope.

Query behavior deliberately reuses the events router's building blocks
(`_resolve_timeline_scope`, `_resolve_tags_filter`, `_run_stat_detector`, …)
instead of re-implementing filter semantics: an agent finding applied to the
Explorer must match exactly what the agent saw.
"""

from __future__ import annotations

import asyncio
import difflib
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from fastapi.concurrency import run_in_threadpool
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, ConfigDict, Field, model_validator

from vestigo.agent.chart_meta import (
    CHART_META,
    LEGACY_KIND_MAP,
    METRIC_INFO,
    PIE_COMFORTABLE_MAX,
    ChartType,
    Metric,
    Scale,
    chart_types_for,
    compare_capable,
    metric_available,
    requires_field,
)
from vestigo.agent.encoding import columnar, columnar_auto
from vestigo.agent.fidelity import DEFAULT_FIDELITY, Fidelity
from vestigo.agent.schema_slim import slim_tool_schema, spec_reference_block
from vestigo.db._time_fields import TIME_FIELD_SPECS, resolve_time_field
from vestigo.db.postgres import User
from vestigo.db.queries import EventQuery, TagFilter
from vestigo.models.embeddings import embeddings_available

# Result budgets: tool output goes into the model's context window, so every
# list is capped and every string truncated. The analyst inspects full events
# in the Explorer — the agent only needs enough to reason and to hand back
# filters.
MAX_EVENTS_PER_SEARCH = 50
MESSAGE_TRUNCATE = 500
# Tighter body cap for the bulk annotation *list* (200 rows resent every turn);
# get_event_annotations keeps the fuller MESSAGE_TRUNCATE for one event's detail.
ANNOTATION_LIST_CONTENT_TRUNCATE = 160
# The message cap below Fidelity.FULL — the one line that survives when an
# event record is reduced, whether it is an anomaly finding's example event
# (`_deflate_findings`) or a search/similarity hit (`_slim_event`). Tighter than
# MESSAGE_TRUNCATE because it is carried tens of rows at a time by exactly the
# tools a small window cannot afford at full detail.
SLIM_MESSAGE_TRUNCATE = 200
ATTR_VALUE_TRUNCATE = 200
MAX_ATTRS_PER_EVENT = 40

# Cap on propose_annotation's event_ids — keeps proposals focused/reviewable
# and bounds the ClickHouse resolution query.
MAX_PROPOSAL_EVENTS = 500

# Cap for the metadata list tools (baselines, views, annotations, sigma runs,
# dispositions). These were unbounded: a long-running case could push an
# arbitrarily large blob into the history, where it is resent every turn.
# Every capped result reports `returned` alongside `total` (see `_listing`) —
# a model that cannot tell a capped list from a complete one would reason over
# a silently partial set, which is exactly what the evidence rule forbids.
MAX_LIST_ROWS = 200

# A9: viz-tool result caps — tighter than the Visualize page's own bounds
# (e.g. field_scatter's UI cap is 20000 points, series_limit up to 50) since
# viz series are dense and every row/point/cell counts against the model's
# context window, same discipline as MAX_EVENTS_PER_SEARCH above.
VIZ_TIMESERIES_MAX_BUCKETS = 60
VIZ_TIMESERIES_MAX_SERIES = 8
VIZ_PIVOT_MAX_LIMIT = 12
VIZ_SCATTER_MAX_POINTS = 1000
VIZ_MAX_BUCKETS = 60
VIZ_MAX_TERMS = 30
VIZ_MAX_BINS = 30
VIZ_GROUPS_MAX = 8
VIZ_CORR_MAX_FIELDS = 8
VIZ_FACET_MAX = 12
VIZ_POINTS_OVERLAY_MAX = 1000


@dataclass(frozen=True)
class ToolInfo:
    """Catalog entry for one agent tool — drives toggle UIs and validation.

    Must stay in sync with the ``@server.tool()`` registrations in
    :func:`build_tool_server` (a registry-parity test enforces this).
    """

    name: str
    description: str
    # Registered but answers with an error when embeddings are off.
    embeddings_gated: bool = False
    # Only registered for in-app conversations (absent from external /mcp).
    requires_conversation: bool = False
    # "core" tools make up the lean profile offered in the tool selector, for
    # small-context local models: enough to run the investigation cycle in
    # SYSTEM_PROMPT end to end (terrain -> aggregate -> search -> confirm ->
    # propose). Everything else is "extended" — useful, but droppable when
    # context is scarcer than capability. Purely a UI preset; disabling is
    # still the analyst's per-conversation deny list.
    tier: Literal["core", "extended"] = "extended"


TOOL_REGISTRY: tuple[ToolInfo, ...] = (
    ToolInfo("search_events", "Search events with Explorer-equivalent filters.", tier="core"),
    ToolInfo(
        "get_event", "Fetch a single event by its event_id (full attribute set).", tier="core"
    ),
    ToolInfo(
        "list_fields",
        "List queryable fields: fixed columns, attribute keys, time parts.",
        tier="core",
    ),
    ToolInfo(
        "describe_field",
        "Probe one field: coverage, numeric-ness, suggested scale/charts.",
        tier="core",
    ),
    ToolInfo(
        "list_artifacts", "List the distinct artifact types present in this timeline.", tier="core"
    ),
    ToolInfo(
        "field_terms",
        "Top-N value distribution for a field, honoring optional filters.",
        tier="core",
    ),
    ToolInfo("field_numeric_stats", "Summary stats + histogram for a numeric field."),
    ToolInfo(
        "field_correlation",
        "Pairwise Pearson/Spearman correlations across several numeric fields.",
    ),
    ToolInfo(
        "field_numeric_grouped",
        "Per-group numeric distributions — one numeric field split by a categorical field.",
    ),
    ToolInfo("histogram", "Time-bucketed event counts — the timeline's shape.", tier="core"),
    ToolInfo(
        "field_timeseries", "Per-value event counts bucketed over time for a field.", tier="core"
    ),
    ToolInfo("time_punchcard", "Event counts by day-of-week x hour-of-day (UTC)."),
    ToolInfo("field_pivot", "Top-X x top-Y co-occurrence count matrix for two fields."),
    ToolInfo("field_scatter", "Random sample of (x, y) numeric value pairs for two fields."),
    ToolInfo("compare", "Compare two filtered layers (time/terms/numeric) of the same timeline."),
    ToolInfo(
        "run_anomaly_detector", "Run a statistical anomaly detector over the timeline.", tier="core"
    ),
    ToolInfo(
        "propose_finding", "Propose a distilled finding card with applicable filters.", tier="core"
    ),
    ToolInfo("propose_chart", "Propose a chart card, validated by executing the underlying query."),
    ToolInfo(
        "propose_annotation",
        "Propose tagging/commenting specific events — the analyst must confirm.",
        requires_conversation=True,
        tier="core",
    ),
    ToolInfo(
        "semantic_search",
        "Find events semantically similar to free text (needs embeddings).",
        embeddings_gated=True,
    ),
    ToolInfo(
        "similar_events",
        "Find events semantically similar to an existing event (needs embeddings).",
        embeddings_gated=True,
    ),
    ToolInfo("list_baselines", "List saved baseline definitions (range + suspect windows)."),
    ToolInfo("list_dispositions", "List analyst verdicts on anomaly findings."),
    ToolInfo("list_saved_views", "List the analyst's saved filter views for this case."),
    ToolInfo("list_annotations", "List annotations across this timeline's sources."),
    ToolInfo("get_event_annotations", "List all annotations attached to one event."),
    ToolInfo("list_sigma_rules", "List Sigma detection rules available to this case."),
    ToolInfo("get_sigma_rule", "Fetch one Sigma rule including its full YAML content."),
    ToolInfo("list_sigma_runs", "List past Sigma evaluations over this timeline."),
    ToolInfo("get_sigma_run", "Fetch one Sigma run's full per-rule results."),
)

TOOL_NAMES: frozenset[str] = frozenset(t.name for t in TOOL_REGISTRY)

# Which tools honour `AgentScope.fidelity` is a policy fact, so the set lives
# beside the tiers it selects: `agent/fidelity.py::FIDELITY_TIERED_TOOLS`.


class FilterSpec(BaseModel):
    """Event filters, mirroring the Explorer's filter shape.

    This is the contract that makes findings applicable: the exact same spec
    the agent used for a search is handed to the frontend, which maps it onto
    Explorer URL params (`frontend/src/lib/queryParams.ts`).
    """

    q: str | None = Field(default=None, description="Free-text search across all fields.")
    q_regex: bool = Field(
        default=False,
        description="Treat q as an RE2 regex (case-sensitive; prefix (?i) to ignore case).",
    )
    artifacts: list[str] | None = Field(
        default=None, description="Artifact types to include (OR'd)."
    )
    source_id: str | None = Field(default=None, description="Restrict to one source.")
    start: datetime | None = Field(default=None, description="Events at/after this time (ISO).")
    end: datetime | None = Field(default=None, description="Events before this time (ISO).")
    filters: dict[str, list[str]] = Field(
        default_factory=dict,
        description=(
            "Field filters: {field: [values]}. Values under one field are OR'd, "
            "distinct fields AND'ed. Use attribute keys as reported by list_fields."
        ),
    )
    exclusions: dict[str, list[str]] = Field(
        default_factory=dict, description="Field exclusions, same shape as filters, negated."
    )
    filter_modes: dict[str, str] = Field(
        default_factory=dict,
        description='Per-field match mode for filters: "exact" (default) | "wildcard" | "regex".',
    )
    exclusion_modes: dict[str, str] = Field(
        default_factory=dict, description="Per-field match mode for exclusions."
    )
    tags_include: list[str] | None = Field(
        default=None, description="Only events carrying any of these tags."
    )
    tags_exclude: list[str] | None = Field(
        default=None, description="Drop events carrying any of these tags."
    )
    annotated: list[str] | None = Field(
        default=None,
        description=(
            'Restrict to annotated events: any of "tag" (user tags, optionally '
            'narrowed by annotation_tag_value) and "anomaly" (system anomaly '
            "marks; unioned with run_id findings when set)."
        ),
    )
    annotation_tag_value: str | None = Field(
        default=None, description='Narrow annotated=["tag"] to one exact tag value.'
    )
    run_id: str | None = Field(
        default=None,
        description=(
            "A persisted detector run id (from run_anomaly_detector) — its "
            'finding event ids are unioned into the "anomaly" branch of '
            '`annotated`. Only effective when annotated includes "anomaly".'
        ),
    )
    event_ids: list[str] | None = Field(
        default=None,
        description="Explicit event_id allowlist, intersected with the other id-based filters.",
    )
    collapse_routine: bool = Field(
        default=False,
        description="Hide events belonging to analyst-marked routine motifs (kind='routine' dispositions).",
    )


class ChartCompareSpec(BaseModel):
    """The optional second layer a chart is measured against."""

    mode: Literal["off", "baseline", "custom"] = Field(
        default="off",
        description=(
            'Comparison layer: "off" (single layer), "baseline" (this timeline\'s '
            'events with no filters — "the whole" the primary is a part of), or '
            '"custom" (an explicit second filter set). Only chart_type "time", '
            '"bar" and "histogram" support a comparison at all.'
        ),
    )
    filters: FilterSpec | None = Field(
        default=None, description='Comparison layer filters — required when mode="custom".'
    )


class ChartFacetSpec(BaseModel):
    """Small multiples: draw the chart once per value of a categorical field.

    Tufte's small-multiple layout — the same mark repeated across subsets, so
    differences between subsets are read as position rather than remembered
    across chart switches. The facet values are the field's top *limit*
    values by event count; the rest are reported as omitted, never merged.
    """

    model_config = ConfigDict(extra="forbid")

    field: str = Field(description="Categorical field token to facet by.")
    limit: int = Field(
        default=6, ge=2, description="How many top values to draw panels for (max 12)."
    )


class ChartOptionsSpec(BaseModel):
    """Presentation and sizing knobs, mirroring the Visualize page's controls.

    Every field is optional; omitting one takes the same default the analyst
    sees. Each chart type reads only some of these (``reads_options`` in
    ``agent/chart_meta.py``) — sending one a chart ignores is reported as a
    warning, never an error.
    """

    model_config = ConfigDict(extra="forbid")

    orientation: Literal["horizontal", "vertical"] | None = Field(
        default=None, description="Bar direction — bar only. Default horizontal."
    )
    sort: Literal["count", "value"] | None = Field(
        default=None,
        description=(
            'Bar ordering — bar only. "count" (descending, the default) or "value" '
            "(ascending, which is what you want for an ordinal axis like an hour of day)."
        ),
    )
    log_scale: bool | None = Field(
        default=None, description="Log-scale the count axis — bar/histogram/scatter."
    )
    series_mode: Literal["overlay", "stacked"] | None = Field(
        default=None, description="Line rendering — line only. Default overlay."
    )
    legend: bool | None = Field(default=None, description="Show the legend — line only.")
    top_n: int | None = Field(
        default=None, ge=1, description="Top-N values to keep — terms and timeseries charts."
    )
    bins: int | None = Field(
        default=None,
        ge=2,
        description=(
            "Histogram bin count — numeric charts. Omit for the automatic "
            "Freedman–Diaconis bin width (the default)."
        ),
    )
    show_density: bool | None = Field(
        default=None,
        description="Smoothed density (KDE) curve over the histogram — histogram only. Default on.",
    )
    buckets: int | None = Field(
        default=None, ge=4, description="Time bucket count — time and timeseries charts."
    )
    limit_x: int | None = Field(default=None, ge=1, description="X-axis top-N — pivot/sankey.")
    limit_y: int | None = Field(default=None, ge=1, description="Y-axis top-N — pivot/sankey.")
    sample_limit: int | None = Field(default=None, ge=1, description="Point cap — scatter only.")
    groups: int | None = Field(
        default=None,
        ge=2,
        description=(
            "Top-N grouping-value cap when box/violin get a categorical field_y "
            "(grouped distributions). Default 8."
        ),
    )
    show_points: bool | None = Field(
        default=None,
        description=(
            "Overlay a uniform random sample of raw data points — box/violin "
            "(jittered strip) and line (markers at real data points)."
        ),
    )


class ChartSpec(BaseModel):
    """A chart, described exactly as the Visualize page describes one.

    This mirrors the frontend's `ChartConfig` field for field, so anything an
    analyst can build by hand you can propose — and the reasoning steps are
    the same ones they take: pick a field, learn its scale (`describe_field`),
    pick a chart_type legal for that scale, optionally add a comparison layer.

    An illegal combination is rejected with a message naming the legal
    alternatives; that error is your equivalent of the analyst's dropdown
    refusing to offer an impossible chart. The result echoes back a `resolved`
    block describing what will actually be drawn — read it, and never assume a
    chart rendered the way you asked without checking there.
    """

    model_config = ConfigDict(extra="ignore")

    chart_type: ChartType = Field(
        description=(
            "The visual mark to draw. Field-free: "
            '"time" (events over time), "punchcard" (day x hour). One field: '
            '"bar", "pie", "waffle" (shares of a whole as a 10x10 cell grid — '
            'prefer it over "pie" past four categories), "histogram", "box", '
            '"violin", "ecdf" (numeric), "line", "heatmap" (one field over time '
            "— NOT field x field). "
            'Two fields: "pivot" (the field x field heatmap grid), "sankey" (flow), '
            '"scatter" (numeric x numeric). "box"/"violin" additionally take an '
            "OPTIONAL categorical field_y to split the distribution into one "
            "box/violin per group."
        )
    )
    scale: Scale | None = Field(
        default=None,
        description=(
            "Scale of measurement of `field`: nominal (unordered categories), "
            "ordinal (ordered categories), interval (numeric, no true zero), "
            "ratio (numeric with a true zero). Constrains which chart types are "
            "legal. Omit to accept the chart type's natural default."
        ),
    )
    field: str | None = Field(
        default=None,
        description=(
            'The field to chart. Required except for "time" and "punchcard", which '
            "chart the whole event count. Use a token from list_fields, including "
            'the virtual "time:" fields (time:hour_of_day, time:day_of_week, '
            "time:month, ...) to put a time part on an axis."
        ),
    )
    field_y: str | None = Field(
        default=None,
        description=(
            "Second field — required for pivot, sankey and scatter; optional on "
            "box and violin, where it is a CATEGORICAL grouping variable that "
            "splits the distribution into one box/violin per top group."
        ),
    )
    fields: list[str] | None = Field(
        default=None,
        description=(
            'Field list for chart_type="corr" only: 2-8 numeric field tokens '
            "whose pairwise correlations form the matrix."
        ),
    )
    metric: Metric = Field(
        default="count",
        description=(
            "How counts are transformed for display. Anything other than "
            '"count" needs chart_type="time": delta/rate/cumulative need ordered '
            'time bins, and "ratio" (% of baseline) additionally needs a comparison layer.'
        ),
    )
    filters: FilterSpec | None = Field(default=None, description="Primary layer filters.")
    compare: ChartCompareSpec = Field(
        default_factory=ChartCompareSpec, description="Optional comparison layer."
    )
    facet: ChartFacetSpec | None = Field(
        default=None,
        description=(
            "Draw one panel per value of a categorical field (small multiples). "
            "Not combinable with `compare`, and only on the single-layer marks "
            "(time, bar, pie, waffle, histogram, box, violin, ecdf)."
        ),
    )
    options: ChartOptionsSpec = Field(
        default_factory=ChartOptionsSpec, description="Presentation and sizing options."
    )

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_kind(cls, data: Any) -> Any:
        """Translate the retired `kind` enum into the current shape.

        Deliberately invisible in the JSON schema the model reads: a documented
        alias would double the contract and the old shape would never die. This
        exists for exactly one case — a conversation in flight across a server
        restart, whose model still holds the previous tool schema in context —
        and is deletable once no such conversation can predate the change.
        """
        if not isinstance(data, dict):
            return data
        kind = data.get("kind")
        if not kind or data.get("chart_type"):
            return data
        mapped = LEGACY_KIND_MAP.get(kind)
        if mapped is None:
            return data
        chart_type, scale = mapped
        data = dict(data)
        data["chart_type"] = chart_type
        data.setdefault("scale", scale)
        options = dict(data.get("options") or {})
        # `limit` was overloaded — its meaning depended on `kind`.
        limit = data.pop("limit", None)
        if limit is not None:
            if kind == "pivot":
                options.setdefault("limit_x", limit)
            elif kind == "scatter":
                options.setdefault("sample_limit", limit)
            elif kind in {"numeric", "compare_numeric"}:
                options.setdefault("bins", limit)
            else:
                options.setdefault("top_n", limit)
        for old, new in (("limit_y", "limit_y"), ("buckets", "buckets"), ("series_limit", "top_n")):
            value = data.pop(old, None)
            if value is not None:
                options.setdefault(new, value)
        data["options"] = options
        if kind.startswith("compare_") and data.get("comparison_filters"):
            data["compare"] = {"mode": "custom", "filters": data["comparison_filters"]}
        data.pop("comparison_filters", None)
        return data


# The per-field prose that `_apply_schema_slimming` strips out of the repeated
# `$defs`, rendered once for the system prompt (A13). Generated from the models
# above, so it cannot drift from them.
SPEC_REFERENCE: str = spec_reference_block(
    (FilterSpec, ChartSpec, ChartCompareSpec, ChartOptionsSpec)
)

# How to read the columnar tool results (A13). Stated once and reused by both
# consumers of this tool server — `runtime.SYSTEM_PROMPT` for the in-app agent
# and `FastMCP(instructions=...)` for external /mcp clients — so the two can
# never describe the wire format differently.
RESULT_FORMAT_NOTE = """## Reading tabular results

Tables come back column-header-once: `{"columns": ["value", "count"], "rows":
[["alice", 12], ["bob", 3]]}` means value=alice count=12, value=bob count=3.
Read each row positionally against `columns`. Nothing is abbreviated — the
values are exactly what the query returned. `field_timeseries` additionally
hoists the shared time axis into `bucket_starts`, and each series' `counts`
array lines up with it index by index.

A list result reports both `total` (how many exist) and `returned` (how many
are in this payload). When they differ the list was capped — say so rather
than reasoning as if you had seen all of them.
"""


@dataclass
class AgentScope:
    """Frozen case/timeline scope a tool server operates in."""

    case_id: str
    timeline_id: str
    user: User
    source_ids: list[str]
    field_mappings: dict[str, list[str]] | None
    source_offsets: dict[str, int] | None
    conversation_id: str | None = None
    # Tools removed from the server after registration (admin hard-deny plus
    # any per-chat restriction) — invisible to the model, not error stubs.
    disabled_tools: frozenset[str] = frozenset()
    # How much of an example record tool results carry (see agent/fidelity.py).
    # A deployment property, and on a retry after an overflow one tier lower —
    # the router rebuilds the scope with `replace(scope, fidelity=...)` rather
    # than rewriting anything already in history.
    fidelity: Fidelity = DEFAULT_FIDELITY
    # Which run of *this* turn the tool server belongs to: 0 for the first, 1+
    # for each re-run the overflow ladder ordered (a fidelity drop or a
    # compaction, `api/routers/agent.py`). A re-run re-executes every tool the
    # model calls again, including the two that write — `run_anomaly_detector`
    # persists another DetectorRun, `propose_annotation` another proposal — so
    # the number is stamped on those rows to tell a superseded re-run apart from
    # a genuine second scan. Not part of the conversation's identity: the router
    # passes `replace(scope, attempt=...)` per attempt.
    attempt: int = 0


async def build_scope(
    case_id: str,
    timeline_id: str,
    user: User,
    conversation_id: str | None = None,
    disabled_tools: frozenset[str] = frozenset(),
    fidelity: Fidelity = DEFAULT_FIDELITY,
) -> AgentScope:
    """Resolve the timeline's source scope once for a conversation turn."""
    from vestigo.api.routers.events import _resolve_timeline_scope

    source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(case_id, timeline_id)
    return AgentScope(
        case_id=case_id,
        timeline_id=timeline_id,
        user=user,
        source_ids=source_ids,
        field_mappings=field_mappings,
        source_offsets=source_offsets,
        conversation_id=conversation_id,
        disabled_tools=disabled_tools,
        fidelity=fidelity,
    )


def _truncate(value: Any, limit: int) -> Any:
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + "…"
    return value


def _columnize(result: Any, *keys: str) -> Any:
    """Re-encode named list-of-dict fields of a tool result columnar (A13).

    Applied at the agent boundary rather than inside ``EventQueryService``:
    the same methods serve the Explorer and Visualize HTTP APIs, whose
    response shapes the frontend depends on. Only the model's copy changes.

    Values are untouched — see ``agent/encoding.py`` for why this is a
    reshaping and not a truncation.
    """
    if not isinstance(result, dict):
        return result
    out = dict(result)
    for key in keys:
        rows = out.get(key)
        if isinstance(rows, list) and rows and all(isinstance(row, dict) for row in rows):
            out[key] = columnar_auto(rows)
    return out


# The full resolved `event` object each anomaly finding carries — the whole
# attribute bag, source_file, byte offsets, raw message. The Analysis page
# renders it inline, so `_serialize_finding` keeps it; but for the agent it was
# ~85% of a finding's size (one value_novelty result measured 37k chars, 31k of
# it `event`), and seven detectors in one turn overflowed a 64k model on it
# alone (2026-07-20).
#
# Its `message` survives, though, and deliberately: for the value-shaped
# detectors the message *is* the finding. "username=gitlab-prometheus seen
# once" is not actionable; "login attempt [gitlab-prometheus/rock] succeeded"
# is, and succeeded-vs-failed is invisible without it. Dropping it outright
# would also invert the saving — `get_event` returns the *full* attribute set,
# more than the inline event it replaced — and every such follow-up spends one
# of the turn's `request_limit` model requests, so a 25-finding sweep that
# followed up honestly would trade the overflow for a turn-limit crash.
#
# How far to reduce is `agent/fidelity.py`'s decision, not this function's.
_FINDING_NOTE = {
    Fidelity.MESSAGE: (
        "Each finding's example event is reduced to its `message`; call get_event(event_id) "
        "for the full record (attributes, offsets) before reasoning about one in detail."
    ),
    Fidelity.MINIMAL: (
        "Each finding's example event is omitted entirely (the model's context is tight); "
        "call get_event(event_id) for any finding you intend to reason about."
    ),
}

# The same contract for the tools that return many whole events rather than
# findings (`search_events`, `semantic_search`, `similar_events`).
_EVENT_NOTE = {
    Fidelity.MESSAGE: (
        "Each event is reduced to its identity fields and `message` — attributes are omitted; "
        "call get_event(event_id) for the full record before reasoning about one in detail."
    ),
    Fidelity.MINIMAL: (
        "Each event is reduced to its identity fields (the model's context is tight) — message "
        "and attributes are omitted; call get_event(event_id) for any event you intend to "
        "reason about."
    ),
}


def _stamp_fidelity(
    payload: dict[str, Any], fidelity: Fidelity, notes: dict[Fidelity, str], *, reduced: bool = True
) -> dict[str, Any]:
    """Record which tier produced *payload*, and admit it when data was dropped.

    ``fidelity`` is stamped at *every* tier, ``FULL`` included: a result with no
    marker at all cannot be told apart from one produced before tiers existed,
    and an exported conversation has to answer "why is there less here than
    there" from the record rather than from configuration the reader may not
    have.

    ``note`` is added only below ``FULL`` and only when something was actually
    reduced — the same self-admitting-truncation contract as :func:`_listing`'s
    ``returned``/``total``. The lookup tolerates a tier with no note rather than
    raising inside a live tool call.
    """
    out = dict(payload)
    out["fidelity"] = fidelity.value
    if reduced and fidelity is not Fidelity.FULL:
        note = notes.get(fidelity)
        if note:
            out["note"] = note
    return out


def _deflate_findings(payload: Any, fidelity: Fidelity) -> Any:
    """Reduce each finding's inline `event` to what *fidelity* admits.

    The model's copy only — persistence has already stored the full payload by
    the time this runs (see ``run_anomaly_detector``). ``event_id`` and
    ``details`` always stay: the id is the handle for ``get_event``, and
    ``details`` is small and carries the surprise/allowlist/method the model
    reasons on. ``Fidelity.FULL`` keeps every event inline and only stamps the
    tier.

    Adds ``note`` when anything was actually reduced, so the model never
    believes it saw the whole record — see :func:`_stamp_fidelity`.
    """
    if not isinstance(payload, dict):
        return payload
    findings = payload.get("results")
    if not isinstance(findings, list):
        return payload
    if fidelity is Fidelity.FULL:
        return _stamp_fidelity(payload, fidelity, _FINDING_NOTE)

    deflated = False
    rows: list[Any] = []
    for row in findings:
        if not isinstance(row, dict) or "event" not in row:
            rows.append(row)
            continue
        event = row["event"]
        deflated = deflated or _finding_event_reduced(event, fidelity)
        slim = {k: v for k, v in row.items() if k != "event"}
        if fidelity is Fidelity.MESSAGE and isinstance(event, dict) and event.get("message"):
            slim["message"] = _truncate(event["message"], SLIM_MESSAGE_TRUNCATE)
        rows.append(slim)

    out = dict(payload)
    out["results"] = rows
    return _stamp_fidelity(out, fidelity, _FINDING_NOTE, reduced=deflated)


def _listing(key: str, rows: list[dict[str, Any]], total: int) -> dict[str, Any]:
    """Build a capped, columnar list result that admits its own truncation.

    ``total`` is how many rows exist, ``returned`` how many are in this
    payload. Reporting only ``total`` (the pre-A13 shape) would hand the model
    ``MAX_LIST_ROWS`` rows under a count of 5,000 with nothing to distinguish
    that from a complete answer — a silently partial set it would then reason
    over as if whole.
    """
    capped = rows[:MAX_LIST_ROWS]
    return {"total": total, "returned": len(capped), key: columnar_auto(capped)}


def _compact_timeseries(result: Any) -> Any:
    """Hoist a timeseries' shared bucket starts out of its per-series rows.

    ``field_value_timeseries`` repeats the same bucket start timestamp in
    every series — up to 8 series x 60 buckets = 480 copies of ~30 chars.
    The starts are identical across series by construction, so state them
    once as ``bucket_starts`` and give each series a bare count array
    positionally aligned to it. Lossless, and it takes the dominant term out
    of the result.

    Bails out unchanged if the series ever disagree on their starts, so a
    future change to the query can't make this silently drop data.
    """
    if not isinstance(result, dict):
        return result
    series = result.get("series")
    if not isinstance(series, list) or not series:
        return result

    starts: list[Any] | None = None
    counts: list[dict[str, Any]] = []
    for entry in series:
        if not isinstance(entry, dict) or not isinstance(entry.get("buckets"), list):
            return result
        entry_starts = [b.get("start") for b in entry["buckets"] if isinstance(b, dict)]
        if len(entry_starts) != len(entry["buckets"]):
            return result
        if starts is None:
            starts = entry_starts
        elif entry_starts != starts:
            return result
        counts.append(
            {"value": entry.get("value"), "counts": [b.get("count") for b in entry["buckets"]]}
        )

    out = dict(result)
    out["bucket_starts"] = starts or []
    out["series"] = columnar(counts, ["value", "counts"])
    return out


def _slim_annotation(row: Any, content_limit: int = MESSAGE_TRUNCATE) -> dict[str, Any]:
    """Compact an Annotation row for model consumption.

    ``content_limit`` truncates the free-text body. The bulk ``list_annotations``
    scan passes a tighter limit than the default (200 rows of 500-char bodies is
    ~7k tokens resent every turn); ``get_event_annotations``, the one-event
    detail tool, keeps the fuller default.
    """
    return {
        "event_id": row.event_id,
        "source_id": row.source_id,
        "type": row.annotation_type,
        "content": _truncate(row.content, content_limit),
        "origin": row.origin,
        "detector": row.detector,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _slim_event(event: dict[str, Any], fidelity: Fidelity) -> dict[str, Any]:
    """Compact an event row for model consumption, down to what *fidelity* admits.

    ``FULL`` is the shape every caller had before tiers existed. ``MESSAGE``
    drops the attribute bag and truncates the message to the tighter
    ``SLIM_MESSAGE_TRUNCATE``; ``MINIMAL`` keeps identity fields only.

    The tier is required rather than defaulted: an exempt caller
    (``get_event``) states its exemption at the call site, where a reader can
    see it, instead of inheriting it silently from this signature.

    The identity fields survive at every tier on purpose: ``event_id`` is the
    handle for ``get_event``, and ``source_id`` is a required argument of
    ``get_event_annotations`` — reducing a result must never strip the means of
    un-reducing it.
    """
    slim: dict[str, Any] = {}
    for key in ("event_id", "timestamp", "source_id", "artifact", "display_name"):
        if event.get(key) not in (None, ""):
            slim[key] = event[key]
    if fidelity is Fidelity.MINIMAL:
        return slim
    if event.get("message"):
        limit = MESSAGE_TRUNCATE if fidelity is Fidelity.FULL else SLIM_MESSAGE_TRUNCATE
        slim["message"] = _truncate(event["message"], limit)
    if fidelity is not Fidelity.FULL:
        return slim
    attrs = event.get("attributes") or {}
    if isinstance(attrs, dict) and attrs:
        slim_attrs = {}
        for i, (k, v) in enumerate(attrs.items()):
            if i >= MAX_ATTRS_PER_EVENT:
                slim_attrs["…"] = f"{len(attrs) - MAX_ATTRS_PER_EVENT} more keys omitted"
                break
            if v in (None, ""):
                continue
            slim_attrs[k] = _truncate(v, ATTR_VALUE_TRUNCATE)
        slim["attributes"] = slim_attrs
    return slim


def _event_reduced(event: dict[str, Any], fidelity: Fidelity) -> bool:
    """Whether *fidelity* actually drops anything from this event.

    Drives the ``note`` on the event-returning tools. A tier below ``FULL``
    does not by itself mean data was lost — an event with no attributes and a
    short message survives ``MESSAGE`` intact — and claiming otherwise puts an
    untruth in the exported record, the same failure ``_listing`` avoids by
    reporting ``returned`` alongside ``total``.
    """
    if fidelity is Fidelity.FULL:
        return False
    attrs = event.get("attributes")
    if isinstance(attrs, dict) and attrs:
        return True
    message = event.get("message")
    if not message:
        return False
    if fidelity is Fidelity.MINIMAL:
        return True
    return len(str(message)) > SLIM_MESSAGE_TRUNCATE


def _finding_event_reduced(event: Any, fidelity: Fidelity) -> bool:
    """Whether *fidelity* actually drops anything from a finding's example event.

    :func:`_event_reduced`'s sibling for the anomaly path, where the *whole*
    event object goes rather than just its attribute bag — so a finding whose
    event carries a timestamp or a source_id loses something even at
    ``MESSAGE``, while one with nothing but a short ``message`` loses nothing at
    all. Claiming otherwise would put the same untruth in the exported record
    that ``_event_reduced`` exists to keep out of the search results.
    """
    if fidelity is Fidelity.FULL:
        return False
    if not isinstance(event, dict) or not event:
        return False
    if fidelity is Fidelity.MINIMAL:
        return True
    if any(key != "message" for key in event):
        return True
    message = event.get("message")
    return bool(message) and len(str(message)) > SLIM_MESSAGE_TRUNCATE


async def _build_query(
    scope: AgentScope,
    spec: FilterSpec | None,
    *,
    limit: int = MAX_EVENTS_PER_SEARCH,
    offset: int = 0,
    order: str = "desc",
) -> EventQuery:
    from vestigo.api.routers.events import (
        _intersect_optional,
        _resolve_annotated_event_ids,
        _resolve_routine_collapse,
        _resolve_tags_filter,
    )

    spec = spec or FilterSpec()
    tags_include: TagFilter | None = None
    tags_exclude: TagFilter | None = None
    if spec.tags_include:
        tags_include = await _resolve_tags_filter(
            scope.case_id, scope.source_ids, spec.tags_include
        )
    if spec.tags_exclude:
        tags_exclude = await _resolve_tags_filter(
            scope.case_id, scope.source_ids, spec.tags_exclude
        )
    annotated_ids = await _resolve_annotated_event_ids(
        scope.case_id,
        scope.source_ids,
        ",".join(spec.annotated) if spec.annotated else None,
        spec.annotation_tag_value,
        spec.run_id,
    )
    event_ids = _intersect_optional(annotated_ids, spec.event_ids)
    routine_scope = await _resolve_routine_collapse(
        scope.case_id, scope.timeline_id, scope.source_ids, spec.collapse_routine
    )
    return EventQuery(
        case_id=scope.case_id,
        source_ids=scope.source_ids,
        q=spec.q,
        q_regex=spec.q_regex,
        artifacts=spec.artifacts,
        source_id=spec.source_id,
        start=spec.start,
        end=spec.end,
        field_filters=spec.filters,
        field_exclusions=spec.exclusions,
        filter_modes=spec.filter_modes,
        exclusion_modes=spec.exclusion_modes,
        tags_include=tags_include,
        tags_exclude=tags_exclude,
        event_ids=event_ids,
        exclude_routine_disposition_ids=routine_scope.motif_disposition_ids,
        exclude_template_hashes=routine_scope.template_hashes,
        # Clamp both ends — the model can pass anything, and a negative
        # LIMIT/OFFSET would surface as a ClickHouse error.
        limit=max(1, min(limit, MAX_EVENTS_PER_SEARCH)),
        offset=max(0, offset),
        order=order if order in ("asc", "desc") else "desc",  # type: ignore[arg-type]
        field_mappings=scope.field_mappings,
        source_offsets=scope.source_offsets,
    )


async def _resolve_event_sources(
    scope: AgentScope, event_ids: list[str]
) -> tuple[dict[str, str], list[str]]:
    """Resolve event_id -> source_id within scope; also return unknown ids."""
    from vestigo.api.routers.events import _get_query_service

    query = await _build_query(scope, FilterSpec(event_ids=event_ids))
    query.limit = len(event_ids)
    page = await run_in_threadpool(_get_query_service().query, query)
    found = {e["event_id"]: e["source_id"] for e in page.events}
    return found, [i for i in event_ids if i not in found]


def _pie_readability_warning(terms: dict[str, Any]) -> str | None:
    """Warn when a pie's slices stop being readable — same rule as the UI.

    Mirrors ``frontend/src/components/viz/lib/pieReadability.ts``: too many
    slices, or two slices within 10% of each other, and angle comparison
    stops carrying the information the chart claims to show. Advisory —
    the chart still validates, the model just learns a better mark exists.
    """
    counts = [v["count"] for v in terms.get("values", []) if v.get("count")]
    slices = len(counts) + (1 if terms.get("other_count") else 0)
    if slices > PIE_COMFORTABLE_MAX:
        return (
            f"{slices} slices — past about {PIE_COMFORTABLE_MAX}, judging angles gets "
            'unreliable. chart_type="bar" (length) or "waffle" (countable cells) reads '
            "more accurately."
        )
    ordered = sorted(counts, reverse=True)
    for bigger, smaller in zip(ordered, ordered[1:], strict=False):
        if bigger and (bigger - smaller) / bigger < 0.1:
            return (
                "Two slices differ by less than 10% — that gap is not readable as an "
                'angle. chart_type="bar" compares them by length instead.'
            )
    return None


def build_tool_server(scope: AgentScope) -> FastMCP:
    """Build an MCP server whose tools are bound to *scope*.

    A fresh (cheap) server instance per conversation turn keeps scoping
    airtight without threading per-call context through the MCP protocol.
    """
    from vestigo.api.routers.events import (
        _get_query_service,
        _get_similarity_service,
        _persist_detector_run,
        _run_stat_detector,
        _serialize_stat_result,
        _validate_field_regexes,
        _validate_regex,
    )

    # These instructions reach *external* MCP clients only. pydantic-ai's
    # MCPToolset does not forward them (it needs include_instructions=True),
    # so the in-app agent is steered entirely by runtime.SYSTEM_PROMPT.
    #
    # They carry the spec reference and the result-format note because the
    # schema slimming (A13) applies to this surface too: an external client
    # sees the same prose-free `$defs` and the same columnar results as the
    # in-app agent, and this is the only channel that can tell it how to read
    # either. Slimming without relocating here would silently leave external
    # clients with less guidance than before A13.
    server = FastMCP(
        "vestigo-investigation",
        instructions=(
            "Read-only forensic log investigation tools, scoped to one case "
            "timeline. Iterate: inspect fields, search, aggregate, then "
            "return refined filters as findings.\n\n"
            f"{RESULT_FORMAT_NOTE}\n{SPEC_REFERENCE}"
        ),
    )
    service = _get_query_service()

    def _validated(spec: FilterSpec | None) -> FilterSpec:
        spec = spec or FilterSpec()
        _validate_regex(spec.q, spec.q_regex)
        _validate_field_regexes(spec.filters, spec.filter_modes)
        _validate_field_regexes(spec.exclusions, spec.exclusion_modes)
        return spec

    # Field vocabulary for chart validation, resolved once per tool server
    # (it is a per-scope constant) rather than per call. ``None`` — not an
    # empty set — marks "not yet resolved", so a legitimately empty timeline
    # is cached instead of re-queried on every call.
    field_vocabulary: set[str] | None = None

    async def _known_fields() -> set[str]:
        """Every token that names real data in this timeline.

        Deliberately the same set ``list_fields`` reports, not the viz picker's
        inventory: a "no such field" error has to be judged against what the
        model was actually told exists. A bare attribute key and its explicit
        ``attr:`` form are both valid, so both are admitted.
        """
        nonlocal field_vocabulary
        if field_vocabulary is None:
            listed = await run_in_threadpool(
                service.list_fields, scope.case_id, scope.source_ids, scope.field_mappings
            )
            attrs = listed.get("attributes") or []
            field_vocabulary = set(listed.get("top_level") or [])
            field_vocabulary.update(attrs)
            field_vocabulary.update(f"attr:{key}" for key in attrs)
            field_vocabulary.update(TIME_FIELD_SPECS)
        return field_vocabulary

    async def _check_chart_field(token: str | None, label: str) -> None:
        """Reject a field token that names nothing, with near-miss suggestions.

        An unknown attribute key resolves to an empty Map lookup rather than an
        error, so without this a typo yields a cheerful ``ok: true`` over zero
        rows — the same silent-success failure mode as the pie-becomes-bar bug.
        """
        if not token:
            return
        # Time tokens are matched the way the query layer matches them
        # (``resolve_time_field`` normalises case/whitespace), so the tool
        # never rejects a spelling that ``_field_column_expr`` would resolve.
        if resolve_time_field(token) is not None:
            return
        known = await _known_fields()
        if token in known:
            return
        close = difflib.get_close_matches(token, sorted(known), n=3, cutoff=0.6)
        hint = f" Closest matches: {', '.join(close)}." if close else ""
        raise ValueError(
            f'{label} "{token}" is not a field in this timeline.{hint} '
            "Call list_fields for the full set."
        )

    def _reject_time_fields(tokens: str | None, label: str) -> None:
        """Reject virtual ``time:`` tokens where the detectors cannot honour them.

        ``db/anomaly_stats.py``'s ``_col_expr`` has no ``time:`` branch, so such
        a token falls through to ``attributes['time:hour_of_day']`` — a lookup
        that is empty for every row. The detector then completes cleanly with
        zero findings, which reads as "nothing anomalous" rather than "that
        field was never scanned". ``list_fields`` advertises these tokens
        (they are real for charts and filters), so the scoping has to be said
        somewhere; an error beats a confidently empty result.
        """
        for token in (t.strip() for t in (tokens or "").split(",")):
            if token and resolve_time_field(token) is not None:
                raise ValueError(
                    f'{label} "{token}" is a virtual time field, which the anomaly '
                    "detectors cannot scan — they read stored columns and attributes, "
                    "and a time part is computed per query. Use it with propose_chart "
                    "or as a filter instead. For temporal anomalies use the frequency "
                    "or interval_periodicity detectors, which bucket time themselves."
                )

    @server.tool()
    async def search_events(
        filters: FilterSpec | None = None,
        limit: int = 20,
        offset: int = 0,
        order: str = "desc",
    ) -> dict[str, Any]:
        """Search events with Explorer-equivalent filters.

        Returns the total match count plus up to `limit` (max 50) compacted
        events. How much of each event comes back depends on the deployment —
        the result's `fidelity`/`note` say which, and get_event returns the
        whole record either way. Iterate by refining `filters` rather than
        paging deeply — aggregations (field_terms, histogram) summarize better
        than paging.
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec, limit=limit, offset=offset, order=order)
        page = await run_in_threadpool(service.query, query)
        return _stamp_fidelity(
            {
                "total": page.total,
                "returned": len(page.events),
                "events": columnar_auto([_slim_event(e, scope.fidelity) for e in page.events]),
            },
            scope.fidelity,
            _EVENT_NOTE,
            reduced=any(_event_reduced(e, scope.fidelity) for e in page.events),
        )

    @server.tool()
    async def get_event(event_id: str) -> dict[str, Any]:
        """Fetch a single event by its event_id (full attribute set, truncated values).

        Always the complete record: this is the escape hatch the reduced
        results point at, so it is exempt from the deployment's tool-result
        detail setting.
        """
        query = await _build_query(scope, FilterSpec(), limit=1)
        query.event_ids = [event_id]
        page = await run_in_threadpool(service.query, query)
        if not page.events:
            return {"error": f"event {event_id} not found in this timeline"}
        # FULL regardless of `scope.fidelity`: this tool is absent from
        # FIDELITY_TIERED_TOOLS precisely so it can un-reduce what the tiered
        # ones handed back.
        return _slim_event(page.events[0], Fidelity.FULL)

    @server.tool()
    async def list_fields() -> dict[str, Any]:
        """List queryable fields: fixed columns, attribute keys, and time parts.

        `time_fields` are virtual: they are defined for every dated event and
        let you put a time part on an axis or in a filter (an hour-of-day x
        country heatmap; "weekends only"). They are computed per query, not
        stored, so they work with propose_chart and the viz/search tools but
        **not** with run_anomaly_detector, which scans stored columns and
        attributes. Use `describe_field` before charting a field you have not
        charted yet — it reports the scale.
        """
        listed = await run_in_threadpool(
            service.list_fields, scope.case_id, scope.source_ids, scope.field_mappings
        )
        return {**listed, "time_fields": sorted(TIME_FIELD_SPECS)}

    @server.tool()
    async def describe_field(field: str, filters: FilterSpec | None = None) -> dict[str, Any]:
        """Describe one field so you can pick a chart type for it.

        The analyst's Visualize page probes a field the moment they pick it and
        auto-suggests a scale; this is that probe. Call it before propose_chart
        for any field you have not charted yet, rather than guessing a scale.

        Returns `non_empty_total` (events with a value for this field under
        these filters) and `distinct`, its numeric stats when it parses as a
        number (`numeric: null` means categorical), the suggested `scale`, and
        `suggested_chart_types` — the chart types legal for that scale.

        `non_empty_total` is a *count*, not the 0-1 `coverage` fraction the
        viz field list reports — don't compare the two.

        Costs two scans for a real field, so probe the fields you intend to
        chart, not every field in the timeline. Virtual `time:` fields are
        answered from their definition and cost nothing.
        """
        time_spec = resolve_time_field(field)
        if time_spec is not None:
            domain = list(time_spec.domain) if time_spec.domain else []
            return {
                "field": field,
                "exists": True,
                "virtual": True,
                "label": time_spec.label,
                # No non_empty_total: a virtual field is derived, not measured.
                # Claiming full coverage would assert something about the data
                # that nothing here checked (undated events have no time part).
                "distinct": len(domain) if domain else None,
                "numeric": None,
                "top_values": domain[:10],
                "suggested_scale": time_spec.scale,
                "suggested_chart_types": chart_types_for(time_spec.scale),
                "notes": [
                    "Virtual time field: defined for every dated event, extracted in UTC. "
                    "Undated (sentinel-timestamp) events are excluded."
                ]
                + (
                    ["Values are a complete, ordered domain — use options.sort='value'."]
                    if domain
                    else []
                ),
            }

        known = await _known_fields()
        if field not in known:
            close = difflib.get_close_matches(field, sorted(known), n=3, cutoff=0.6)
            return {
                "field": field,
                "exists": False,
                "suggestions": close,
                "notes": ["Not a field in this timeline. Call list_fields for the full set."],
            }

        spec = _validated(filters)
        query = await _build_query(scope, spec)
        terms, numeric = await asyncio.gather(
            run_in_threadpool(service.field_terms, query, field, 5),
            run_in_threadpool(service.field_numeric_stats, query, field),
        )
        # `count == 0` is the documented categorical signal (see
        # EventQueryService.field_numeric_stats) — the same test the Visualize
        # page's auto-probe uses to pick nominal vs ratio.
        is_numeric = bool(numeric["count"])
        scale: Scale = "ratio" if is_numeric else "nominal"
        notes: list[str] = []
        if terms["distinct"] == 1:
            notes.append("Only one distinct value — a chart of it will show a single category.")
        if not terms["total"]:
            notes.append("No event has a non-empty value for this field under these filters.")
        if not is_numeric:
            notes.append(
                "Values do not parse as numbers, so numeric charts "
                "(histogram/box/violin/ecdf/scatter) would render empty."
            )
        return {
            "field": field,
            "exists": True,
            "virtual": False,
            "non_empty_total": terms["total"],
            "distinct": terms["distinct"],
            "numeric": (
                {
                    "count": numeric["count"],
                    "min": numeric["min"],
                    "max": numeric["max"],
                    "mean": numeric["mean"],
                }
                if is_numeric
                else None
            ),
            "top_values": terms["values"],
            "suggested_scale": scale,
            "suggested_chart_types": chart_types_for(scale),
            "notes": notes,
        }

    @server.tool()
    async def list_artifacts() -> list[str]:
        """List the distinct artifact types present in this timeline."""
        return await run_in_threadpool(
            service.list_distinct_artifacts, scope.case_id, scope.source_ids
        )

    @server.tool()
    async def field_terms(
        field: str, filters: FilterSpec | None = None, limit: int = 30
    ) -> dict[str, Any]:
        """Top-N value distribution for a field, honoring optional filters.

        The primary primitive for spotting dominant/rare values. `other_count`
        is the tail beyond the top N.
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        result = await run_in_threadpool(service.field_terms, query, field, max(1, min(limit, 100)))
        return _columnize(result, "values")

    @server.tool()
    async def field_numeric_stats(field: str, filters: FilterSpec | None = None) -> dict[str, Any]:
        """Summary stats + fixed-width histogram for a numeric field. count==0 means non-numeric."""
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        result = await run_in_threadpool(service.field_numeric_stats, query, field)
        return _columnize(result, "bins")

    @server.tool()
    async def field_correlation(
        fields: list[str], filters: FilterSpec | None = None
    ) -> dict[str, Any]:
        """Pairwise Pearson/Spearman correlations across 2-8 numeric fields.

        Each pair reports the number of events where BOTH fields are numeric
        (pairwise-complete), so a sparse field cannot silently shrink the
        other pairs. Correlation is not causation, and a coefficient near 0
        only rules out the relationship it measures (Pearson: straight-line;
        Spearman: monotonic).
        """
        spec_f = _validated(filters)
        query = await _build_query(scope, spec_f)
        chosen = list(dict.fromkeys(fields))[:VIZ_CORR_MAX_FIELDS]
        for token in chosen:
            await _check_chart_field(token, "fields")
        return await run_in_threadpool(service.field_correlation, query, chosen)

    @server.tool()
    async def field_numeric_grouped(
        field: str,
        group_field: str,
        filters: FilterSpec | None = None,
        groups: int = 8,
    ) -> dict[str, Any]:
        """Per-group numeric distributions: one numeric field split by a categorical field.

        Powers grouped box/violin plots. Groups are the top-N group_field
        values by numeric-value count; the rest are omitted (reported via
        omitted_groups/omitted_count, never rolled into an "Other" group).
        """
        spec_f = _validated(filters)
        query = await _build_query(scope, spec_f)
        result = await run_in_threadpool(
            service.field_numeric_grouped,
            query,
            field,
            group_field,
            min(max(groups, 2), VIZ_GROUPS_MAX),
        )
        # Bins per group are card-rendering detail; the model reads the
        # quantile summaries.
        for g in result.get("groups", []):
            g.pop("bins", None)
        return result

    @server.tool()
    async def histogram(filters: FilterSpec | None = None, buckets: int = 48) -> dict[str, Any]:
        """Time-bucketed event counts honoring optional filters — the timeline's shape."""
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        result = await run_in_threadpool(service.histogram, query, min(max(buckets, 4), 120))
        return _columnize(result, "buckets")

    @server.tool()
    async def field_timeseries(
        field: str,
        filters: FilterSpec | None = None,
        buckets: int = 30,
        series_limit: int = 6,
    ) -> dict[str, Any]:
        """Per-value event counts bucketed over time for `field` — how a field's top values trend.

        Capped at the top `series_limit` values by overall count (max 8) so a
        high-cardinality field doesn't explode into dozens of series; run
        `field_terms` first to see the full distribution before deciding
        which values are worth trending.
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        result = await run_in_threadpool(
            service.field_value_timeseries,
            query,
            field,
            min(max(buckets, 4), VIZ_TIMESERIES_MAX_BUCKETS),
            max(1, min(series_limit, VIZ_TIMESERIES_MAX_SERIES)),
        )
        return _compact_timeseries(result)

    @server.tool()
    async def time_punchcard(filters: FilterSpec | None = None) -> dict[str, Any]:
        """Event counts by (day-of-week x hour-of-day), UTC — surfaces weekly/daily rhythm."""
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        result = await run_in_threadpool(service.time_punchcard, query)
        return _columnize(result, "cells")

    @server.tool()
    async def field_pivot(
        field_x: str,
        field_y: str,
        filters: FilterSpec | None = None,
        limit_x: int = 8,
        limit_y: int = 8,
    ) -> dict[str, Any]:
        """Top-X x top-Y co-occurrence count matrix for two fields (each axis capped at 12).

        `total` counts only events where both fields are non-empty. Useful
        for spotting which value-pairs cluster (e.g. user x workstation).
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        result = await run_in_threadpool(
            service.field_pivot,
            query,
            field_x,
            field_y,
            max(1, min(limit_x, VIZ_PIVOT_MAX_LIMIT)),
            max(1, min(limit_y, VIZ_PIVOT_MAX_LIMIT)),
        )
        return _columnize(result, "cells")

    @server.tool()
    async def field_scatter(
        field_x: str, field_y: str, filters: FilterSpec | None = None, limit: int = 300
    ) -> dict[str, Any]:
        """Uniform random sample of (x, y) numeric value pairs for two fields (capped at 1000 points).

        `sampled`/`total` in the response tell you what fraction of matching
        pairs the sample represents — read correlation cautiously below a
        few hundred points.
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec)
        return await run_in_threadpool(
            service.field_scatter,
            query,
            field_x,
            field_y,
            max(1, min(limit, VIZ_SCATTER_MAX_POINTS)),
        )

    @server.tool()
    async def compare(
        kind: str,
        primary_filters: FilterSpec | None = None,
        comparison_filters: FilterSpec | None = None,
        field: str | None = None,
        buckets: int = 30,
        limit: int = 20,
    ) -> dict[str, Any]:
        """Compare two filtered layers of the SAME timeline side by side.

        `kind`: "time" (event counts on a shared bucket grid — `buckets`,
        max 60), "terms" (top-value counts for `field` — `limit`, max 30, and
        `field` is required), or "numeric" (fixed-width histograms for
        `field` on shared bin edges — `limit` used as bin count, max 30,
        `field` required). `primary_filters`/`comparison_filters` are two
        independent FilterSpecs (e.g. two time windows, or an artifact type
        vs the rest) — both scoped to this same case/timeline.
        """
        if kind not in ("time", "terms", "numeric"):
            raise ValueError('kind must be "time", "terms", or "numeric"')
        if kind in ("terms", "numeric") and not field:
            raise ValueError(f'kind="{kind}" requires field')
        primary_spec = _validated(primary_filters)
        comparison_spec = _validated(comparison_filters)
        primary_query = await _build_query(scope, primary_spec)
        comparison_query = await _build_query(scope, comparison_spec)
        # Each kind returns its rows under a different key; all three are
        # dict-per-row and among the heaviest results the agent can ask for.
        if kind == "time":
            result = await run_in_threadpool(
                service.compare_time_histogram,
                primary_query,
                comparison_query,
                min(max(buckets, 4), VIZ_MAX_BUCKETS),
            )
            return _columnize(result, "buckets")
        if kind == "terms":
            result = await run_in_threadpool(
                service.compare_field_terms,
                primary_query,
                comparison_query,
                field,
                max(1, min(limit, VIZ_MAX_TERMS)),
            )
            return _columnize(result, "values")
        result = await run_in_threadpool(
            service.compare_field_numeric,
            primary_query,
            comparison_query,
            field,
            max(1, min(limit, VIZ_MAX_BINS)),
        )
        return _columnize(result, "bins")

    @server.tool()
    async def run_anomaly_detector(
        detector: str,
        fields: str | None = None,
        series_field: str = "artifact",
        baseline_id: str | None = None,
        limit: int = 30,
        z_threshold: float | None = Field(default=None, gt=0),
        min_skew_seconds: float | None = Field(default=None, ge=0),
        fdr_q: float | None = Field(default=None, gt=0, le=1),
        min_ratio: float | None = Field(default=None, gt=1),
        ngram_size: int | None = Field(default=None, ge=2, le=5),
        min_support: int | None = Field(default=None, ge=2),
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, Any]:
        """Run a statistical anomaly detector over the timeline.

        Detectors: value_novelty (rare/first-seen values), value_combo,
        frequency (volume spikes/silences), timestamp_order, numeric_range,
        charset, entropy, proportion_shift, interval_periodicity,
        sequence_novelty, sequence_motif, value_distribution_drift.
        `fields` is a comma-separated field list for value detectors (omit to
        auto-recommend); `series_field` groups frequency/sequence detectors.
        Temporal detectors need a `baseline_id` from list_baselines (omit for
        a self-baseline run). Tuning knobs (all optional, server defaults
        otherwise): z_threshold (frequency |z| cutoff), min_skew_seconds
        (timestamp_order), fdr_q (BH false-discovery ceiling), min_ratio
        (effect-size floor), ngram_size (sequence length, 2-5), min_support
        (sequence_motif), start/end (sequence_motif mining window).
        Returns findings plus a persisted run_id the analyst can open. Each
        finding carries an example `event_id`; how much of that event comes
        with it depends on the deployment, and the result's `fidelity`/`note`
        say which. Call get_event on the id for the full record.

        The virtual `time:` fields from list_fields are **not** detector
        fields — they are for charting and filtering only. Passing one is
        rejected rather than run.
        """
        _reject_time_fields(fields, "fields")
        _reject_time_fields(series_field, "series_field")
        result, resolution = await _run_stat_detector(
            scope.case_id,
            scope.timeline_id,
            scope.source_ids,
            detector=detector,
            fields=fields,
            series_field=series_field,
            z_threshold=z_threshold,
            baseline_id=baseline_id,
            limit=max(1, min(limit, 100)),
            min_skew_seconds=min_skew_seconds,
            fdr_q=fdr_q,
            min_ratio=min_ratio,
            ngram_size=ngram_size,
            min_support=min_support,
            start=start,
            end=end,
            field_mappings=scope.field_mappings,
            source_offsets=scope.source_offsets,
        )
        payload = _serialize_stat_result(result)
        run_id = None
        if result.status == "ok":
            run_id = await _persist_detector_run(
                scope.case_id,
                scope.timeline_id,
                detector=detector,
                fields=fields,
                series_field=series_field,
                z_threshold=z_threshold,
                limit=max(1, min(limit, 100)),
                payload=payload,
                resolution=resolution,
                source_offsets=scope.source_offsets,
                # Non-zero when the overflow ladder re-ran this turn: the same
                # scan is being persisted a second time, and the analyst opening
                # the Analysis page has to be able to tell that from two scans.
                agent_retry_attempt=scope.attempt,
            )
        payload["run_id"] = run_id
        # Slim then columnize the model's copy only, and only *after*
        # persistence: `_persist_detector_run` stored `payload` as the run's
        # reproducible result, and the Analysis page reads that back in its
        # full dict-row shape. `_deflate_findings` drops each finding's inline
        # `event` (the model keeps `event_id` + `get_event`); `_columnize` then
        # states the shared keys once. Together they take a seven-detector
        # sweep from tens of thousands of tokens back inside a small window.
        return _columnize(_deflate_findings(payload, scope.fidelity), "results")

    @server.tool()
    async def propose_finding(title: str, description: str, filters: FilterSpec) -> dict[str, Any]:
        """Propose a distilled finding to the analyst.

        Call this when a filter set isolates something worth attention. The
        analyst sees a card with your title/description and an "apply to
        Explorer" button carrying exactly these filters. The returned total
        is the filter's current hit count — verify it is what you expect.
        Propose only filters you have actually run via search_events.
        """
        spec = _validated(filters)
        query = await _build_query(scope, spec, limit=1)
        page = await run_in_threadpool(service.query, query)
        return {"accepted": True, "title": title, "total": page.total}

    @server.tool()
    async def propose_chart(title: str, description: str, spec: ChartSpec) -> dict[str, Any]:
        """Propose a chart to the analyst.

        Validates `spec` against the same legality rules the Visualize page
        enforces through its dropdowns, then by actually executing the
        underlying query — this tool never writes anything. The analyst sees a
        live chart card with "Open in Visualize" and "Save"; their click is
        what persists a saved chart, never this call.

        An illegal spec errors with a message naming the legal alternatives, so
        you can correct it; no card is shown for a failed proposal. A legal one
        returns `resolved` — the chart_type, scale, metric, comparison mode and
        options that will actually be drawn. Read it. Do not report what a chart
        shows without checking `resolved` matches what you asked for.

        `warnings` lists non-fatal issues: options this chart type ignores, and
        any limit clamped for the validation query. Those clamps bound *this*
        call's result size for your context only — the analyst's card is drawn
        at the full size you asked for.
        """
        chart_type = spec.chart_type
        meta = CHART_META[chart_type]
        data_kind = meta.data_kind
        opts = spec.options
        warnings: list[str] = []

        # ── legality, before any query ───────────────────────────────────────
        scale = spec.scale or meta.default_scale
        if scale not in meta.scales:
            raise ValueError(
                f'chart_type="{chart_type}" requires scale in '
                f"{{{', '.join(chr(34) + s + chr(34) for s in meta.scales)}}}, "
                f'got "{scale}". Chart types legal for scale="{scale}": '
                f"{', '.join(chart_types_for(scale))}."
            )

        if requires_field(chart_type) and not meta.multi_field and not spec.field:
            raise ValueError(
                f'chart_type="{chart_type}" requires field. Only chart_type="time" and '
                '"punchcard" chart the whole event count with no field.'
            )
        if meta.requires_second_field and not spec.field_y:
            raise ValueError(
                f'chart_type="{chart_type}" requires field_y — it charts '
                "field x field_y, not a single distribution. For one field over "
                'time use chart_type="heatmap" instead.'
            )
        if spec.field_y and not meta.requires_second_field and not meta.accepts_second_field:
            # Naming trap worth spelling out rather than only enumerating: our
            # "heatmap" is one field x time, and the field x field grid an
            # analyst also calls a heatmap is "pivot". A model that reached for
            # the word alone burned both its retries on the same rejection.
            two_field = [c for c in CHART_META if CHART_META[c].requires_second_field]
            hint = (
                ' chart_type="heatmap" is one field over time; for a field x field '
                'heatmap grid use chart_type="pivot".'
                if chart_type == "heatmap"
                else ""
            )
            raise ValueError(
                f'chart_type="{chart_type}" takes no field_y. '
                f"Two-field chart types: {', '.join(two_field)}.{hint}"
            )

        if meta.multi_field:
            if not spec.fields or len(spec.fields) < 2:
                raise ValueError(
                    f'chart_type="{chart_type}" needs `fields`: 2-'
                    f"{VIZ_CORR_MAX_FIELDS} numeric field tokens to correlate. "
                    "`field`/`field_y` are not used by this chart."
                )
            if len(set(spec.fields)) != len(spec.fields):
                raise ValueError("`fields` must not repeat a field token.")
        elif spec.fields:
            multi = [c for c in CHART_META if CHART_META[c].multi_field]
            raise ValueError(
                f'chart_type="{chart_type}" takes no `fields` list. '
                f"Charts that do: {', '.join(multi)}."
            )

        if spec.facet is not None:
            if not meta.supports_facet:
                faceted = [c for c in CHART_META if CHART_META[c].supports_facet]
                raise ValueError(
                    f'chart_type="{chart_type}" cannot be facetted. Facettable '
                    f"chart types: {', '.join(faceted)}."
                )
            if spec.compare.mode != "off":
                raise ValueError(
                    "facet and compare cannot both be set — one splits the data into "
                    "panels, the other overlays two layers in one panel. Pick one."
                )
            # A virtual `time:` token is a perfectly good facet (one panel
            # per weekday), so unlike the detectors this does not reject them.
            await _check_chart_field(spec.facet.field, "facet.field")

        compare_on = spec.compare.mode != "off"
        if compare_on and not meta.supports_compare:
            raise ValueError(
                f'chart_type="{chart_type}" does not support a comparison layer. '
                f"Compare-capable chart types: {', '.join(compare_capable())}."
            )
        if spec.compare.mode == "custom" and spec.compare.filters is None:
            raise ValueError(
                'compare.mode="custom" needs compare.filters. Use mode="baseline" to '
                "compare against this timeline's whole unfiltered event set."
            )
        if not metric_available(spec.metric, chart_type, compare_on):
            info = METRIC_INFO[spec.metric]
            if info.requires_compare and not compare_on:
                raise ValueError(
                    f'metric="{spec.metric}" ({info.label}) needs a comparison layer — '
                    'set compare.mode to "baseline" or "custom".'
                )
            raise ValueError(
                f'metric="{spec.metric}" ({info.label}) is only defined on '
                f'chart_type="time", which is the one chart with ordered time bins. '
                f"Its formula is {info.formula}."
            )

        # Options this chart never reads are inert, not fatal — but silence
        # would leave the model believing it had set something.
        ignored = sorted(
            key
            for key, value in opts.model_dump().items()
            if value is not None and key not in meta.reads_options
        )
        if ignored:
            reads = ", ".join(meta.reads_options) or "no options"
            warnings.append(
                f'options {", ".join(ignored)} ignored by chart_type="{chart_type}" '
                f"(it reads: {reads})."
            )

        await _check_chart_field(spec.field, "field")
        await _check_chart_field(spec.field_y, "field_y")
        for token in spec.fields or []:
            await _check_chart_field(token, "fields")

        def _capped(value: int | None, default: int, cap: int, name: str, floor: int = 1) -> int:
            resolved = max(floor, min(value or default, cap))
            if value is not None and resolved != value:
                warnings.append(
                    f"options.{name}={value} clamped to {resolved} for this validation "
                    "query (agent context budget); the analyst's card is not capped."
                )
            return resolved

        primary_filters = _validated(spec.filters)
        primary_query = await _build_query(scope, primary_filters)
        comparison_query = None
        if compare_on:
            # "baseline" is the timeline's whole event set — the same unfiltered
            # resolution POST /viz/compare does for mode="baseline".
            comparison_filters = _validated(
                spec.compare.filters if spec.compare.mode == "custom" else FilterSpec()
            )
            comparison_query = await _build_query(scope, comparison_filters)

        applied: dict[str, Any] = {}
        #: Options this chart type nominally reads but that this *particular*
        #: spec made inert (a bounded time axis ignores its limit). Kept out
        #: of the `resolved` echo below, which otherwise re-adds them.
        inert_options: set[str] = set()

        # ── execute, dispatching on the aggregation the mark needs ───────────
        if data_kind == "terms":
            applied["top_n"] = _capped(opts.top_n, 30, VIZ_MAX_TERMS, "top_n")
            if comparison_query is not None:
                result = await run_in_threadpool(
                    service.compare_field_terms,
                    primary_query,
                    comparison_query,
                    spec.field,
                    applied["top_n"],
                )
                summary = {
                    "primary_total": result["primary_total"],
                    "comparison_total": result["comparison_total"],
                    "distinct": result["distinct"],
                }
            else:
                result = await run_in_threadpool(
                    service.field_terms, primary_query, spec.field, applied["top_n"]
                )
                summary = {
                    "total": result["total"],
                    "distinct": result["distinct"],
                    "top_values": result["values"][:5],
                }
                if chart_type == "pie":
                    readability = _pie_readability_warning(result)
                    if readability:
                        warnings.append(readability)
        elif data_kind == "numeric" and spec.field_y and meta.accepts_second_field:
            # Grouped box/violin: numeric response × categorical grouping field.
            applied["groups"] = _capped(opts.groups, 8, VIZ_GROUPS_MAX, "groups", floor=2)
            applied["bins"] = _capped(opts.bins, 30, VIZ_MAX_BINS, "bins")
            result = await run_in_threadpool(
                service.field_numeric_grouped,
                primary_query,
                spec.field,
                spec.field_y,
                applied["groups"],
                applied["bins"],
                bool(opts.show_points),
                VIZ_POINTS_OVERLAY_MAX,
            )
            if not result["total"]:
                raise ValueError(
                    f'field "{spec.field}" has no numeric values under these filters, so '
                    f'chart_type="{chart_type}" would render empty. Treat it as '
                    'categorical: chart_type "bar"/"pie"/"heatmap" with scale "nominal".'
                )
            summary = {
                "total": result["total"],
                "groups": [
                    {"value": g["value"], "count": g["count"], "median": g["quantiles"]["0.5"]}
                    for g in result["groups"]
                ],
                "omitted_groups": result["omitted_groups"],
                "omitted_count": result["omitted_count"],
            }
        elif data_kind == "numeric":
            if comparison_query is not None:
                # The comparison aggregation has no auto-bin path (shared bin
                # edges are negotiated between the two layers), so an omitted
                # bins falls back to the manual default.
                applied["bins"] = _capped(opts.bins, 30, VIZ_MAX_BINS, "bins")
                result = await run_in_threadpool(
                    service.compare_field_numeric,
                    primary_query,
                    comparison_query,
                    spec.field,
                    applied["bins"],
                )
                summary = {
                    "primary_total": result["primary_total"],
                    "comparison_total": result["comparison_total"],
                }
            else:
                # bins omitted → the service picks Freedman–Diaconis; echo the
                # resolved count so the model knows what will be drawn.
                bins_arg = (
                    _capped(opts.bins, 30, VIZ_MAX_BINS, "bins") if opts.bins is not None else None
                )
                result = await run_in_threadpool(
                    service.field_numeric_stats, primary_query, spec.field, bins_arg
                )
                applied["bins"] = len(result["bins"]) or None
                applied["bin_rule"] = result.get("bin_rule", "manual")
                if not result["count"]:
                    raise ValueError(
                        f'field "{spec.field}" has no numeric values under these filters, so '
                        f'chart_type="{chart_type}" would render empty. Treat it as '
                        'categorical: chart_type "bar"/"pie"/"heatmap" with scale "nominal".'
                    )
                summary = {
                    "count": result["count"],
                    "min": result["min"],
                    "max": result["max"],
                    "mean": result["mean"],
                    "skewness": result.get("skewness"),
                }
        elif data_kind == "timeseries":
            applied["buckets"] = _capped(
                opts.buckets, 30, VIZ_TIMESERIES_MAX_BUCKETS, "buckets", floor=4
            )
            applied["top_n"] = _capped(opts.top_n, 6, VIZ_TIMESERIES_MAX_SERIES, "top_n")
            result = await run_in_threadpool(
                service.field_value_timeseries,
                primary_query,
                spec.field,
                applied["buckets"],
                applied["top_n"],
            )
            summary = {
                "series_count": len(result["series"]),
                "interval_seconds": result["interval_seconds"],
            }
        elif data_kind == "time":
            applied["buckets"] = _capped(opts.buckets, 30, VIZ_MAX_BUCKETS, "buckets", floor=4)
            if comparison_query is not None:
                result = await run_in_threadpool(
                    service.compare_time_histogram,
                    primary_query,
                    comparison_query,
                    applied["buckets"],
                )
                summary = {
                    "primary_total": result["primary_total"],
                    "comparison_total": result["comparison_total"],
                }
            else:
                result = await run_in_threadpool(
                    service.histogram, primary_query, applied["buckets"]
                )
                summary = {
                    "buckets": len(result["buckets"]),
                    "interval_seconds": result["interval_seconds"],
                }
        elif data_kind == "punchcard":
            result = await run_in_threadpool(service.time_punchcard, primary_query)
            summary = {"total": result["total"], "max_count": result["max_count"]}
        elif data_kind == "pivot":
            applied["limit_x"] = _capped(opts.limit_x, 8, VIZ_PIVOT_MAX_LIMIT, "limit_x")
            applied["limit_y"] = _capped(opts.limit_y, 8, VIZ_PIVOT_MAX_LIMIT, "limit_y")
            result = await run_in_threadpool(
                service.field_pivot,
                primary_query,
                spec.field,
                spec.field_y,
                applied["limit_x"],
                applied["limit_y"],
            )
            # A bounded `time:` axis is charted as its whole natural-order
            # domain (an hour with no events is a finding, not a value to
            # hide), so its limit never applied. Say so and stop echoing a
            # limit that did nothing — silence here would leave the model
            # believing it had bounded a matrix it had not.
            for axis, token in (("x", spec.field), ("y", spec.field_y)):
                axis_spec = resolve_time_field(token or "")
                if axis_spec is None or axis_spec.domain is None:
                    continue
                applied.pop(f"limit_{axis}", None)
                inert_options.add(f"limit_{axis}")
                warnings.append(
                    f'options.limit_{axis} does not apply to "{token}": a bounded time '
                    f"axis is charted as its full {len(axis_spec.domain)}-value domain, "
                    "so empty slots stay visible."
                )
            summary = {
                "total": result["total"],
                # `*_distinct` carries two units — a measured distinct count
                # the axis may have been truncated against, or the size of a
                # bounded time domain charted whole. `*_bounded` says which,
                # so "12 of 400 distinct" and "12 of 12" are not read alike.
                "x_distinct": result["x_distinct"],
                "y_distinct": result["y_distinct"],
                "x_bounded": result["x_bounded"],
                "y_bounded": result["y_bounded"],
                # Size of the matrix the model is about to reason over —
                # what the axes actually resolved to, which for a bounded
                # time axis is its whole domain rather than a limit.
                "matrix_size": len(result["x_values"]) * len(result["y_values"]),
            }
        elif data_kind == "corr":
            fields = (spec.fields or [])[:VIZ_CORR_MAX_FIELDS]
            if len(fields) < len(spec.fields or []):
                warnings.append(f"fields truncated to the first {VIZ_CORR_MAX_FIELDS} tokens.")
            result = await run_in_threadpool(service.field_correlation, primary_query, fields)
            dropped = [d["field"] for d in result["dropped_fields"]]
            if dropped:
                warnings.append(
                    f"no numeric values for {', '.join(dropped)} under these filters — "
                    "their row/column will be empty. Check them with describe_field."
                )
            summary = {
                "total": result["total"],
                "pairs": [
                    {
                        "x": p["x"],
                        "y": p["y"],
                        "n": p["n"],
                        "pearson": p["pearson"],
                        "spearman": p["spearman"],
                    }
                    for p in result["pairs"]
                ],
                "dropped_fields": dropped,
            }
        else:  # scatter
            applied["sample_limit"] = _capped(
                opts.sample_limit, 300, VIZ_SCATTER_MAX_POINTS, "sample_limit"
            )
            result = await run_in_threadpool(
                service.field_scatter,
                primary_query,
                spec.field,
                spec.field_y,
                applied["sample_limit"],
            )
            if not result["sampled"]:
                raise ValueError(
                    f'no event has numeric values for both "{spec.field}" and '
                    f'"{spec.field_y}" under these filters, so chart_type="scatter" '
                    "would render empty. Check both fields with describe_field."
                )
            summary = {"total": result["total"], "sampled": result["sampled"]}
            stats_block = result.get("stats")
            if stats_block:
                # The correlation verdict, compressed for the model — full
                # detail renders on the analyst's card from the same response.
                summary["stats"] = {
                    "pearson_r": stats_block["pearson"]["r"],
                    "pearson_p": stats_block["pearson"]["p"],
                    "spearman_rho": stats_block["spearman"]["rho"],
                    "spearman_p": stats_block["spearman"]["p"],
                    "regression": stats_block["regression"],
                    "recommendation": stats_block["recommendation"],
                }

        # Facet panels: enumerate the values the grid will draw. Only the
        # value list is fetched here, not one aggregation per panel — the
        # analyst's card re-queries every panel itself, and running K heavy
        # scans to validate a proposal would spend the scan budget K times
        # over for a number the model never reads. The single aggregation
        # above already proves the mark works against these filters.
        if spec.facet is not None:
            facet_limit = _capped(spec.facet.limit, 6, VIZ_FACET_MAX, "facet.limit", floor=2)
            facet_terms = await run_in_threadpool(
                service.field_terms, primary_query, spec.facet.field, facet_limit
            )
            shown = [v["value"] for v in facet_terms["values"]]
            summary["facet"] = {
                "field": spec.facet.field,
                "panels": shown,
                "distinct": facet_terms["distinct"],
                "omitted_values": max(0, facet_terms["distinct"] - len(shown)),
                "omitted_count": facet_terms["other_count"],
            }
            if not shown:
                raise ValueError(
                    f'facet field "{spec.facet.field}" has no values under these '
                    "filters, so the facet grid would be empty."
                )

        # Presentation options don't reach the query, but belong in the echo —
        # they are part of what the analyst will see.
        for key in meta.reads_options:
            if key in applied or key in inert_options:
                continue
            value = getattr(opts, key)
            if value is not None:
                applied[key] = value

        return {
            "ok": True,
            "resolved": {
                "chart_type": chart_type,
                "scale": scale,
                "metric": spec.metric,
                "compare_mode": spec.compare.mode,
                "data_kind": data_kind,
                "field": spec.field,
                "field_y": spec.field_y,
                "fields": spec.fields,
                "facet": (
                    {"field": spec.facet.field, "limit": spec.facet.limit} if spec.facet else None
                ),
                "options": applied,
            },
            "warnings": warnings,
            "summary": summary,
        }

    if scope.conversation_id is not None:

        @server.tool()
        async def propose_annotation(
            event_ids: list[str],
            tag: str | None = None,
            comment: str | None = None,
            rationale: str = "",
        ) -> dict[str, Any]:
            """Propose tagging/commenting specific events — the analyst must confirm.

            Nothing is written until the analyst clicks Confirm on the proposal
            card. Provide exact event_ids (max 500) you have inspected via
            search_events/get_event, at least one of tag/comment, and a short
            rationale. Annotation is a deliberate act: propose focused,
            verified sets, not broad sweeps.
            """
            from vestigo.api.deps import get_store

            if not tag and not comment:
                return {"error": "provide at least one of tag or comment"}
            if not event_ids:
                return {"error": "event_ids must not be empty"}
            if len(event_ids) > MAX_PROPOSAL_EVENTS:
                return {
                    "error": (
                        f"too many events ({len(event_ids)} > "
                        f"{MAX_PROPOSAL_EVENTS}) — narrow the set"
                    )
                }
            found, unknown = await _resolve_event_sources(scope, list(dict.fromkeys(event_ids)))
            if unknown:
                return {"error": f"event ids not found in this timeline: {unknown[:20]}"}
            proposal = await get_store().create_agent_proposal(
                case_id=scope.case_id,
                timeline_id=scope.timeline_id,
                conversation_id=scope.conversation_id,
                tag=tag,
                comment=comment,
                rationale=rationale,
                events=[{"source_id": s, "event_id": e} for e, s in found.items()],
            )
            return {
                "proposal_id": proposal.id,
                "status": "proposed",
                "event_count": len(found),
            }

    @server.tool()
    async def semantic_search(q: str, limit: int = 10) -> dict[str, Any]:
        """Find events semantically similar to free text (needs embeddings)."""
        if not embeddings_available():
            return {"error": "embeddings are not available in this installation"}
        svc = _get_similarity_service()
        result = await run_in_threadpool(
            svc.find_similar_by_text,
            scope.case_id,
            scope.source_ids,
            q,
            limit=max(1, min(limit, 50)),
        )
        return _stamp_fidelity(
            {
                "status": result.status,
                "results": [
                    {
                        "event_id": r.event_id,
                        "score": r.score,
                        "event": _slim_event(r.event or {}, scope.fidelity),
                    }
                    for r in result.results
                ],
            },
            scope.fidelity,
            _EVENT_NOTE,
            reduced=any(_event_reduced(r.event or {}, scope.fidelity) for r in result.results),
        )

    @server.tool()
    async def similar_events(event_id: str, limit: int = 10) -> dict[str, Any]:
        """Find events semantically similar to an existing event (needs embeddings)."""
        svc = _get_similarity_service()
        result = await run_in_threadpool(
            svc.find_similar,
            scope.case_id,
            scope.source_ids,
            event_id,
            limit=max(1, min(limit, 50)),
        )
        return _stamp_fidelity(
            {
                "status": result.status,
                "results": [
                    {
                        "event_id": r.event_id,
                        "score": r.score,
                        "event": _slim_event(r.event or {}, scope.fidelity),
                    }
                    for r in result.results
                ],
            },
            scope.fidelity,
            _EVENT_NOTE,
            reduced=any(_event_reduced(r.event or {}, scope.fidelity) for r in result.results),
        )

    @server.tool()
    async def list_baselines() -> dict[str, Any]:
        """List saved baseline definitions (baseline range + suspect windows).

        Pass a baseline's id as `baseline_id` to run_anomaly_detector to run
        temporal detection (proportion_shift, interval_periodicity,
        sequence_novelty, frequency, value_distribution_drift) against it.
        """
        from vestigo.api.deps import get_store

        rows = await get_store().list_baseline_definitions(scope.case_id, scope.timeline_id)
        return _listing(
            "baselines",
            [
                {
                    "id": r.id,
                    "name": r.name,
                    **r.windows_payload(),
                    "created_by": r.created_by,
                }
                for r in rows
            ],
            len(rows),
        )

    @server.tool()
    async def list_dispositions(
        kind: str | None = None, detector: str | None = None
    ) -> dict[str, Any]:
        """List analyst verdicts on anomaly findings visible from this timeline.

        Kinds: 'normal' (expected behavior, suppresses detection), 'dismissed'
        (noise), 'confirmed' (escalated true positive), 'routine' (recurring
        expected motif). Use these to avoid re-reporting what the analyst has
        already judged.
        """
        from vestigo.api.deps import get_store

        rows = await get_store().list_dispositions(
            scope.case_id,
            timeline_id=scope.timeline_id,
            source_ids=scope.source_ids,
            kinds=[kind] if kind else None,
            detector=detector,
        )
        return _listing(
            "dispositions",
            [
                {
                    "id": r.id,
                    "kind": r.kind,
                    "detector": r.detector,
                    "field": r.field,
                    "value": _truncate(r.value, ATTR_VALUE_TRUNCATE),
                    "source_id": r.source_id,
                    "event_id": r.event_id,
                    "note": _truncate(r.note, MESSAGE_TRUNCATE),
                    "created_by": r.created_by,
                }
                for r in rows[:MAX_LIST_ROWS]
            ],
            len(rows),
        )

    @server.tool()
    async def list_saved_views() -> dict[str, Any]:
        """List the analyst's saved filter views for this case (name, query, filter payload)."""
        from vestigo.api.deps import get_store

        rows = await get_store().list_views(scope.case_id)
        return _listing(
            "views",
            [
                {"id": r.id, "name": r.name, "query": r.query, "filter": r.view_filter or {}}
                for r in rows[:MAX_LIST_ROWS]
            ],
            len(rows),
        )

    @server.tool()
    async def list_annotations(annotation_type: str | None = None) -> dict[str, Any]:
        """List annotations (tags/comments/system anomaly marks) across this timeline's sources.

        `annotation_type` filters to 'tag', 'comment', or 'anomaly'. Results
        are capped at 200 rows, oldest first (compare `returned` against
        `total`) — use get_event_annotations for one event's full detail.
        """
        from vestigo.api.deps import get_store

        rows = await get_store().list_source_annotations(scope.case_id, scope.source_ids)
        if annotation_type:
            rows = [r for r in rows if r.annotation_type == annotation_type]
        return _listing(
            "annotations",
            [
                _slim_annotation(r, content_limit=ANNOTATION_LIST_CONTENT_TRUNCATE)
                for r in rows[:MAX_LIST_ROWS]
            ],
            len(rows),
        )

    @server.tool()
    async def get_event_annotations(source_id: str, event_id: str) -> dict[str, Any]:
        """List all annotations attached to one event (full content, oldest first)."""
        from vestigo.api.deps import get_store

        if source_id not in scope.source_ids:
            return {"error": f"source {source_id} is not part of this timeline"}
        rows = await get_store().list_annotations(scope.case_id, source_id, event_id)
        return _listing(
            "annotations", [_slim_annotation(r) for r in rows[:MAX_LIST_ROWS]], len(rows)
        )

    @server.tool()
    async def list_sigma_rules() -> dict[str, Any]:
        """List Sigma detection rules available to this case (metadata only).

        Covers both the global rule directory and case-uploaded rules. Use
        get_sigma_rule with a case rule's id to read its YAML body.
        """
        from vestigo.api.deps import get_store
        from vestigo.api.routers.sigma import _global_rule_dict, _load_global

        global_rules, case_rows = await asyncio.gather(
            _load_global(), get_store().list_sigma_rules(scope.case_id)
        )
        rules = [_global_rule_dict(r) for r in global_rules]
        for row in case_rows:
            rules.append(
                {
                    "origin": "case",
                    "id": row.id,
                    "rule_key": row.rule_key,
                    "title": row.title,
                    "level": row.level,
                    "logsource": row.logsource,
                    "enabled": row.enabled,
                }
            )
        return _listing("rules", rules, len(rules))

    @server.tool()
    async def get_sigma_rule(rule_id: str) -> dict[str, Any]:
        """Fetch one case-uploaded Sigma rule including its full YAML content."""
        from vestigo.api.deps import get_store

        row = await get_store().get_sigma_rule(scope.case_id, rule_id)
        if row is None:
            return {"error": f"no case-uploaded sigma rule with id {rule_id}"}
        return row.to_dict()

    @server.tool()
    async def list_sigma_runs() -> dict[str, Any]:
        """List past Sigma evaluations over this timeline (newest first, no per-rule detail)."""
        from vestigo.api.deps import get_store

        rows = await get_store().list_sigma_runs(scope.case_id, timeline_id=scope.timeline_id)
        return _listing(
            "runs",
            [
                {
                    "id": r.id,
                    "status": r.status,
                    "created_by": r.created_by,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                    "completed_at": r.completed_at.isoformat() if r.completed_at else None,
                    "rule_count": len(r.results or []),
                }
                for r in rows[:MAX_LIST_ROWS]
            ],
            len(rows),
        )

    @server.tool()
    async def get_sigma_run(run_id: str) -> dict[str, Any]:
        """Fetch one Sigma run's full per-rule results (match counts, statuses, compiled SQL)."""
        from vestigo.api.deps import get_store

        row = await get_store().get_sigma_run(scope.case_id, run_id)
        if row is None or row.timeline_id != scope.timeline_id:
            return {"error": f"no sigma run with id {run_id} in this timeline"}
        return row.to_dict()

    if scope.disabled_tools:
        # Remove after registration rather than skipping registration: the
        # closures stay uniform above, and the intersection guards names that
        # were never registered for this scope (propose_annotation outside a
        # conversation). A removed tool is absent from tools/list, so it never
        # enters the model's prompt. The registered set derives from
        # TOOL_REGISTRY (parity-tested against the actual registrations)
        # rather than FastMCP internals.
        registered = {
            t.name
            for t in TOOL_REGISTRY
            if not t.requires_conversation or scope.conversation_id is not None
        }
        for name in scope.disabled_tools & registered:
            server.remove_tool(name)

    _apply_schema_slimming(server)

    return server


def _apply_schema_slimming(server: FastMCP) -> None:
    """Replace each tool's advertised parameter schema with a slimmed one (A13).

    Rewriting ``Tool.parameters`` changes what ``tools/list`` reports without
    touching ``Tool.fn_metadata``, which is what FastMCP actually validates
    call arguments against — so this shrinks the prompt, never the contract.

    Done here rather than via a pydantic-ai schema transformer so it applies
    identically across providers (the OpenAI profile already strips ``title``,
    the Anthropic one strips nothing) and to the external ``/mcp`` surface.
    """
    # The only internal-attribute reach in this module; a test guards it
    # against an MCP SDK bump.
    manager = server._tool_manager
    for tool in manager.list_tools():
        tool.parameters = slim_tool_schema(tool.parameters)
