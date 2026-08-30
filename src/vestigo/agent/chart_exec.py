"""Shared chart-spec execution: validate a ``ChartSpec`` and run its query.

The single server-side path from a chart spec to its data, used by the
agent's ``propose_chart`` tool and the Stories export resolver
(``vestigo.stories.export``). Extracted verbatim from ``propose_chart`` —
every legality rule and error message is the one the tool always raised,
so agent-facing behavior is unchanged.

Imports from ``vestigo.agent.tools`` happen inside the function: ``tools``
imports this module at load time, so a module-level import here would be
circular. The helpers a caller can inject (``service``, ``validated``,
``check_field``) default to standalone equivalents of the tool server's
closure state; ``propose_chart`` passes its own (which carry a per-scope
field-vocabulary cache), the export resolver uses the defaults.
"""

from __future__ import annotations

import functools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi.concurrency import run_in_threadpool

from vestigo.agent.chart_meta import (
    CHART_META,
    DERIVE_SOURCE_SCALES,
    METRIC_INFO,
    chart_types_for,
    compare_capable,
    metric_available,
    requires_field,
)
from vestigo.db._scan import ScanBusy
from vestigo.db._time_fields import TIME_FIELD_SPECS, resolve_time_field

if TYPE_CHECKING:
    from vestigo.agent.tools import AgentScope, ChartSpec


@dataclass(frozen=True)
class ChartLimits:
    """Per-option ``(default, cap)`` bounds for a chart execution.

    Two callers execute the same spec for different audiences. The agent runs
    a *validation* query whose result has to fit a context window, so it caps
    hard. An export runs the *analyst's* chart and freezes the answer into an
    attested report, so it must not silently show less than the interactive
    card did. Same code path, different bounds — see ``AGENT_CHART_LIMITS``
    and ``ANALYST_CHART_LIMITS``.

    ``reason`` is the phrase appended to a clamp warning; it ends up in the
    snapshot, so it has to describe the caller the reader is looking at.
    """

    reason: str
    terms_top_n: tuple[int, int]
    groups: tuple[int, int]
    bins: tuple[int, int]
    series_buckets: tuple[int, int]
    series_top_n: tuple[int, int]
    time_buckets: tuple[int, int]
    pivot_limit: tuple[int, int]
    table_rows: tuple[int, int]
    #: Instants one mark source may resolve to. ``None`` = the ``viz_marks_max``
    #: setting (the analyst's ceiling); the agent's is fixed and smaller because
    #: every resolved mark is summarised into the model's context.
    marks_per_source: int | None
    #: Weeks the calendar heatmap draws (latest kept, earlier disclosed).
    calendar_weeks: int
    #: Ranked change: top-N per window before the union, and the union's row cap.
    change_top_n: tuple[int, int]
    change_union: int
    #: Interval lanes: the lane cap (by event count) and the row cap of the
    #: ordered start/end scan the pairing runs over.
    lanes: tuple[int, int]
    lanes_rows: int
    scatter_sample: tuple[int, int]
    corr_max_fields: int
    points_overlay_max: int
    group_cardinality_caution: int


#: The agent's bounds: small, because every number here lands in the model's
#: context. Values match what ``propose_chart`` always used.
AGENT_CHART_LIMITS = ChartLimits(
    reason="for this validation query (agent context budget); the analyst's card is not capped",
    terms_top_n=(30, 30),
    groups=(8, 8),
    bins=(30, 30),
    series_buckets=(30, 60),
    series_top_n=(6, 8),
    time_buckets=(30, 60),
    pivot_limit=(8, 12),
    table_rows=(20, 30),
    marks_per_source=20,
    calendar_weeks=53,
    change_top_n=(10, 20),
    change_union=30,
    lanes=(10, 20),
    lanes_rows=2_000,
    scatter_sample=(300, 1000),
    corr_max_fields=8,
    points_overlay_max=1000,
    group_cardinality_caution=50,
)

#: The analyst's bounds: the same defaults and ceilings the ``/api/viz/*``
#: endpoints apply to the interactive card, so a frozen export shows what the
#: analyst saw rather than an agent-sized subset of it.
ANALYST_CHART_LIMITS = ChartLimits(
    reason="to the analyst chart ceiling",
    terms_top_n=(50, 500),
    groups=(8, 8),
    bins=(30, 200),
    series_buckets=(60, 200),
    series_top_n=(12, 50),
    time_buckets=(60, 200),
    pivot_limit=(10, 50),
    table_rows=(50, 500),
    marks_per_source=None,
    calendar_weeks=53,
    change_top_n=(10, 100),
    change_union=200,
    lanes=(10, 100),
    lanes_rows=50_000,
    scatter_sample=(5000, 20000),
    corr_max_fields=8,
    points_overlay_max=1000,
    group_cardinality_caution=50,
)


#: Per-chart-type ceiling on a terms chart's ``top_n``, narrowing whatever
#: ``ChartLimits.terms_top_n`` allows.
#:
#: ``terms_top_n``'s cap is what the *endpoint* can return; this is what the
#: *mark* can honestly draw. A bar axis scrolls, so it reaches the endpoint's
#: 500 — but pie and waffle share that same ``data_kind="terms"`` aggregation
#: while being bounded by legibility instead: a pie past a few dozen slices is
#: unreadable and a waffle only has 100 cells to share out. Without this, an
#: agent-proposed or exported pie could carry 500 slices that the Visualize
#: page clamps to 50, so the analyst could not reproduce or edit back the very
#: chart their story snapshot shows.
#:
#: Mirrors ``TOPN_MAX`` in ``frontend/src/components/viz/lib/chartOptions.ts``.
TERMS_TOP_N_BY_CHART: dict[str, int] = {
    "pie": 50,
    "waffle": 50,
}


async def run_gated_scan(fn: Any, *args: Any) -> Any:
    """Run a gated aggregation; a busy foreground lane is a tool error, not a crash.

    Shared with ``vestigo.agent.tools``: every agent-facing call into a
    ``@_foreground_scan`` method goes through here, because the bounded wait
    raises :class:`ScanBusy` and a bare ``run_in_threadpool`` would let that
    ``RuntimeError`` escape the tool — a 500 out of the MCP surface instead of
    something the model can act on.
    """
    try:
        return await run_in_threadpool(fn, *args)
    except ScanBusy as exc:
        raise ValueError(f"{exc}. Tell the analyst the chart lane is busy and try again.") from exc


async def execute_chart_spec(
    scope: AgentScope,
    spec: ChartSpec,
    *,
    service: Any = None,
    validated: Any = None,
    check_field: Any = None,
    limits: ChartLimits | None = None,
) -> dict[str, Any]:
    """Validate and execute a chart spec against the scope's timeline.

    Returns the ``propose_chart`` envelope (``ok``/``resolved``/``warnings``/
    ``summary``) plus ``result`` — the full aggregation payload the summary
    was compressed from, which the export resolver freezes into snapshots
    (``propose_chart`` drops it to protect the agent's context budget).
    Raises ValueError with an analyst-correctable message on an illegal spec.

    ``limits`` defaults to ``AGENT_CHART_LIMITS``; the export resolver passes
    ``ANALYST_CHART_LIMITS`` so a frozen chart matches the card it came from.
    """
    if limits is None:
        limits = AGENT_CHART_LIMITS
    from vestigo.agent.tools import FilterSpec, _build_query, _pie_readability_warning

    if service is None:
        from vestigo.api.routers.events import _get_query_service

        service = _get_query_service()

    if validated is None:
        from vestigo.api.routers.events import _validate_field_modes, _validate_regex

        def validated(fspec: FilterSpec | None) -> FilterSpec:
            fspec = fspec or FilterSpec()
            _validate_regex(fspec.q, fspec.q_regex)
            _validate_field_modes(fspec.filters, fspec.filter_modes)
            _validate_field_modes(fspec.exclusions, fspec.exclusion_modes)
            return fspec

    if check_field is None:
        # Standalone field check: same vocabulary rule as the tool server's
        # cached ``_check_chart_field``, resolved fresh per call.
        vocabulary: set[str] | None = None

        async def check_field(token: str | None, label: str) -> None:
            nonlocal vocabulary
            if not token:
                return
            if resolve_time_field(token) is not None:
                return
            if vocabulary is None:
                listed = await run_gated_scan(
                    service.list_fields, scope.case_id, scope.source_ids, scope.field_mappings
                )
                attrs = listed.get("attributes") or []
                vocabulary = set(listed.get("top_level") or [])
                vocabulary.update(attrs)
                vocabulary.update(f"attr:{key}" for key in attrs)
                vocabulary.update(TIME_FIELD_SPECS)
            if token not in vocabulary:
                raise ValueError(f'{label}="{token}" names no field in this timeline.')

    chart_type = spec.chart_type
    meta = CHART_META[chart_type]
    data_kind = meta.data_kind
    opts = spec.options
    warnings: list[str] = []

    # ── legality, before any query ───────────────────────────────────────
    scale = spec.scale or meta.default_scale
    if spec.derive is not None:
        # A derivation is validated from the registry, like everything else
        # here: only figures whose `derives` lists its kind admit one, a
        # virtual time: field is already a calendar part, and the result is
        # ordered categories whatever the field was — so an omitted scale
        # resolves to ordinal before the legality check below.
        if spec.derive.kind not in meta.derives:
            takers = [c for c in CHART_META if spec.derive.kind in CHART_META[c].derives]
            raise ValueError(
                f'chart_type="{chart_type}" admits no derivation. Figures that take '
                f"{spec.derive.kind}: {', '.join(takers)}."
            )
        if spec.field and resolve_time_field(spec.field) is not None:
            raise ValueError(
                f"{spec.field} is already a calendar part — chart it directly, without derive."
            )
        # `scale` is the treat-as the derivation is computed from (the page's
        # "Number or time" / "Measure"), never the categories it yields — the
        # effective scale is ordinal whatever the field was. "ordinal" itself
        # is still accepted: it was the only legal value for a while.
        admitted = DERIVE_SOURCE_SCALES[spec.derive.kind]
        if spec.scale is not None and spec.scale != "ordinal" and spec.scale not in admitted:
            raise ValueError(
                f"a {spec.derive.kind} derivation is computed from a field treated as "
                f"{' or '.join(admitted)}: set scale to that, or omit it."
            )
        scale = "ordinal"
    if spec.inputs is not None and spec.inputs.columns is not None and chart_type != "table":
        raise ValueError(
            f'chart_type="{chart_type}" takes no inputs.columns — only the table figure has columns.'
        )
    wants_distinct_second = (
        spec.inputs is not None
        and spec.inputs.columns is not None
        and "distinct_second" in spec.inputs.columns
    ) or opts.table_sort_by == "distinct_second"
    if chart_type == "table" and wants_distinct_second and not spec.field_y:
        raise ValueError(
            "distinct_second needs field_y — the second field whose distinct values each row counts."
        )
    if spec.marks and not meta.supports_marks:
        takers = [c for c in CHART_META if CHART_META[c].supports_marks]
        raise ValueError(
            f'chart_type="{chart_type}" takes no marks — figures that draw them: {", ".join(takers)}.'
        )
    lane_inputs = spec.inputs is not None and any(
        getattr(spec.inputs, key) is not None for key in ("pairing", "start_filter", "end_filter")
    )
    if lane_inputs and chart_type != "lanes":
        raise ValueError('inputs.pairing / start_filter / end_filter are chart_type="lanes" only.')
    # Before the per-figure rules below, not after: those are stated in terms
    # of the scale ("quantity=\"sum\" needs scale=\"ratio\""), so on a scale the
    # figure does not admit at all they send the model round a loop of
    # refusals that cannot be satisfied — which is exactly what cumulative at
    # scale="interval" did. Naming the illegal scale once ends it.
    if scale not in meta.scales:
        raise ValueError(
            f'chart_type="{chart_type}" requires scale in '
            f"{{{', '.join(chr(34) + s + chr(34) for s in meta.scales)}}}, "
            f'got "{scale}". Chart types legal for scale="{scale}": '
            f"{', '.join(chart_types_for(scale))}."
        )
    pairing: str | None = None
    if data_kind == "lanes":
        pairing = (spec.inputs.pairing if spec.inputs else None) or "first_last"
        has_start = spec.inputs is not None and spec.inputs.start_filter is not None
        has_end = spec.inputs is not None and spec.inputs.end_filter is not None
        if pairing == "next_end" and not (has_start and has_end):
            raise ValueError(
                'pairing="next_end" needs inputs.start_filter and inputs.end_filter — the '
                'events that open and close an interval; pairing="first_last" needs neither.'
            )
        if pairing == "first_last" and (has_start or has_end):
            warnings.append('inputs.start_filter/end_filter ignored under pairing="first_last".')
    quantity: str | None = None
    if data_kind == "cumulative":
        quantity = opts.quantity or (
            "events" if not spec.field else "sum" if scale == "ratio" else "distinct"
        )
        if quantity != "events" and not spec.field:
            raise ValueError(
                f'quantity="{quantity}" needs field — only quantity="events" counts without one.'
            )
        if quantity == "sum" and scale != "ratio":
            raise ValueError(
                'quantity="sum" needs scale="ratio" — a running sum over anything but a '
                'measure is not a quantity; use "distinct" (values seen so far) or "events".'
            )
        if quantity == "distinct" and scale not in ("nominal", "ordinal"):
            raise ValueError(
                'quantity="distinct" needs scale="nominal" or "ordinal" — distinct values of a '
                'measure are not a count of anything; use "sum" or "events".'
            )
        if quantity == "events" and spec.field:
            warnings.append(
                f'field is ignored — quantity="events" counts every event, not '
                f'field="{spec.field}"; use "sum" or "distinct" to accumulate the field.'
            )
    if data_kind == "calendar" and spec.field and resolve_time_field(spec.field) is not None:
        raise ValueError(
            f"{spec.field}: a calendar part is always present — a calendar over it counts "
            "every event; omit field."
        )

    if requires_field(chart_type) and not meta.multi_field and not spec.field:
        raise ValueError(
            f'chart_type="{chart_type}" requires field. Only chart_type="time" and '
            '"punchcard" chart every event with no field; "cumulative" and "calendar" '
            "take an optional one."
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
                f"{limits.corr_max_fields} numeric field tokens to correlate. "
                "`field`/`field_y` are not used by this chart."
            )
        if len(set(spec.fields)) != len(spec.fields):
            raise ValueError("`fields` must not repeat a field token.")
        # Reject, don't truncate: silently charting the first eight would
        # answer a question the model never asked and label it the answer
        # to the one it did — the same rule (and wording) as the
        # field_correlation tool and the HTTP endpoint's 422.
        if len(spec.fields) > limits.corr_max_fields:
            raise ValueError(
                f"a correlation matrix needs between 2 and {limits.corr_max_fields} fields, "
                f"got {len(spec.fields)}. Correlate the most promising ones, or run "
                "several matrices."
            )
    elif spec.fields:
        multi = [c for c in CHART_META if CHART_META[c].multi_field]
        raise ValueError(
            f'chart_type="{chart_type}" takes no `fields` list. Charts that do: {", ".join(multi)}.'
        )

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
    if meta.requires_compare and not compare_on:
        raise ValueError(
            f'chart_type="{chart_type}" needs a comparison layer — it ranks how each '
            "value's share of the window moved between two windows; set compare.mode "
            'to "baseline" or "custom".'
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

    await check_field(spec.field, "field")
    await check_field(spec.field_y, "field_y")
    for token in spec.fields or []:
        await check_field(token, "fields")

    def _capped(value: int | None, bounds: tuple[int, int], name: str, floor: int = 1) -> int:
        default, cap = bounds
        resolved = max(floor, min(value or default, cap))
        if value is not None and resolved != value:
            warnings.append(f"options.{name}={value} clamped to {resolved} {limits.reason}.")
        return resolved

    primary_filters = validated(spec.filters)
    primary_query = await _build_query(scope, primary_filters)
    comparison_query = None
    if compare_on:
        # "baseline" is the timeline's whole event set — the same unfiltered
        # resolution POST /viz/compare does for mode="baseline".
        comparison_filters = validated(
            spec.compare.filters if spec.compare.mode == "custom" else FilterSpec()
        )
        comparison_query = await _build_query(scope, comparison_filters)

    resolved_marks: dict[str, Any] | None = None
    if spec.marks:
        from vestigo.agent.marks import resolve_marks
        from vestigo.api.deps import get_store
        from vestigo.core.config import get_settings

        resolved_marks = await resolve_marks(
            scope,
            spec.marks,
            service=service,
            store=get_store(),
            cap=limits.marks_per_source or get_settings().viz_marks_max,
            validated=validated,
        )

    applied: dict[str, Any] = {}
    #: Passed only when set, so a positional fake service keeps working and an
    #: underived request is byte-for-byte the call it always was.
    derive_kw: dict[str, Any] = {"derive": spec.derive} if spec.derive is not None else {}
    #: Options this chart type nominally reads but that this *particular*
    #: spec made inert (a bounded time axis ignores its limit). Kept out
    #: of the `resolved` echo below, which otherwise re-adds them.
    inert_options: set[str] = set()

    # ── execute, dispatching on the aggregation the mark needs ───────────
    if data_kind == "terms":
        terms_default, terms_cap = limits.terms_top_n
        terms_cap = min(terms_cap, TERMS_TOP_N_BY_CHART.get(chart_type, terms_cap))
        applied["top_n"] = _capped(opts.top_n, (min(terms_default, terms_cap), terms_cap), "top_n")
        if comparison_query is not None:
            result = await run_gated_scan(
                functools.partial(service.compare_field_terms, **derive_kw),
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
            result = await run_gated_scan(
                functools.partial(service.field_terms, **derive_kw),
                primary_query,
                spec.field,
                applied["top_n"],
            )
            if spec.derive is not None and spec.derive.kind == "bins" and not result["total"]:
                raise ValueError(
                    f'field "{spec.field}" has no numeric values under these filters, so '
                    "bins would be empty — treat it as categories instead."
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
        applied["groups"] = _capped(opts.groups, limits.groups, "groups", floor=2)
        applied["bins"] = _capped(opts.bins, limits.bins, "bins")
        result = await run_gated_scan(
            service.field_numeric_grouped,
            primary_query,
            spec.field,
            spec.field_y,
            applied["groups"],
            applied["bins"],
            bool(opts.show_points),
            limits.points_overlay_max,
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
        # Omission belongs in `warnings`, not only in the summary the model
        # may skim: a chart that drops groups has to say so out loud.
        if result["omitted_groups"]:
            warnings.append(
                f"{result['omitted_groups']} further {spec.field_y} value(s) "
                f"({result['omitted_count']} events) fall outside the top "
                f"{applied['groups']} and are omitted — not merged into an "
                '"Other" box, which would be a distribution of unrelated things.'
            )
        # Advisory, like the pie rule: a grouping variable with hundreds of
        # values is usually an identifier, and a box per identifier is not
        # a comparison. Never a refusal — the analyst may know better.
        if result["distinct_groups"] > limits.group_cardinality_caution:
            warnings.append(
                f'"{spec.field_y}" has {result["distinct_groups"]} distinct values — '
                "that looks like an identifier rather than a grouping variable, and "
                "only the top groups are drawn. A categorical field with few values "
                "compares more honestly."
            )
    elif data_kind == "numeric":
        if comparison_query is not None:
            # The comparison aggregation has no auto-bin path (shared bin
            # edges are negotiated between the two layers), so an omitted
            # bins falls back to the manual default.
            applied["bins"] = _capped(opts.bins, limits.bins, "bins")
            result = await run_gated_scan(
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
            bins_arg = _capped(opts.bins, limits.bins, "bins") if opts.bins is not None else None
            result = await run_gated_scan(
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
        applied["buckets"] = _capped(opts.buckets, limits.series_buckets, "buckets", floor=4)
        applied["top_n"] = _capped(opts.top_n, limits.series_top_n, "top_n")
        result = await run_gated_scan(
            functools.partial(service.field_value_timeseries, **derive_kw),
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
        applied["buckets"] = _capped(opts.buckets, limits.time_buckets, "buckets", floor=4)
        if comparison_query is not None:
            result = await run_gated_scan(
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
            result = await run_gated_scan(service.histogram, primary_query, applied["buckets"])
            summary = {
                "buckets": len(result["buckets"]),
                "interval_seconds": result["interval_seconds"],
            }
    elif data_kind == "punchcard":
        result = await run_gated_scan(service.time_punchcard, primary_query)
        summary = {"total": result["total"], "max_count": result["max_count"]}
    elif data_kind == "cumulative":
        applied["buckets"] = _capped(opts.buckets, limits.time_buckets, "buckets", floor=4)
        applied["quantity"] = quantity
        result = await run_gated_scan(
            functools.partial(
                service.cumulative,
                field=spec.field,
                quantity=quantity,
                buckets=applied["buckets"],
            ),
            primary_query,
        )
        summary = {
            "total": result["total"],
            "events": result["events"],
            "unparsed": result["unparsed"],
            "buckets": len(result["buckets"]),
            "interval_seconds": result["interval_seconds"],
        }
    elif data_kind == "calendar":
        result = await run_gated_scan(
            functools.partial(service.calendar, field=spec.field, max_weeks=limits.calendar_weeks),
            primary_query,
        )
        summary = {
            "total": result["total"],
            "max_count": result["max_count"],
            "weeks": result["weeks"],
            "weeks_total": result["weeks_total"],
            "truncated": result["truncated"],
            "dropped": result["dropped"],
        }
    elif data_kind == "change":
        if comparison_query is None:  # unreachable: the requires_compare refusal above
            raise RuntimeError("change chart reached execution without a comparison layer")
        applied["top_n"] = _capped(opts.top_n, limits.change_top_n, "top_n")
        applied["layout"] = opts.layout or "dumbbell"
        result = await run_gated_scan(
            functools.partial(service.field_change, union_cap=limits.change_union, **derive_kw),
            primary_query,
            comparison_query,
            spec.field,
            applied["top_n"],
        )
        summary = {
            "primary_total": result["primary_total"],
            "comparison_total": result["comparison_total"],
            "union_size": result["union_size"],
            "rows_shown": result["rows_shown"],
            "truncated": result["truncated"],
            "top_rows": [
                {
                    "value": r["value"],
                    "status": r["status"],
                    "delta_share": round(r["delta_share"], 4),
                }
                for r in result["rows"][:5]
            ],
        }
    elif data_kind == "lanes":
        from dataclasses import replace

        if pairing is None:  # unreachable: resolved in the legality block above
            raise RuntimeError("lanes reached execution without a resolved pairing")
        applied["limit_y"] = _capped(opts.limit_y, limits.lanes, "limit_y")
        lanes_kw: dict[str, Any] = {
            "pairing": pairing,
            "limit_y": applied["limit_y"],
            "rows_cap": limits.lanes_rows,
        }
        if pairing == "next_end" and spec.inputs is not None:
            start_query = await _build_query(scope, validated(spec.inputs.start_filter))
            end_query = await _build_query(scope, validated(spec.inputs.end_filter))
            # Pinned to the primary's window, as the endpoint pins them.
            lanes_kw["start"] = replace(
                start_query, start=primary_query.start, end=primary_query.end
            )
            lanes_kw["end"] = replace(end_query, start=primary_query.start, end=primary_query.end)
        result = await run_gated_scan(
            functools.partial(service.field_lanes, **lanes_kw), primary_query, spec.field
        )
        summary = {
            "pairing": result["pairing"],
            "lanes_shown": len(result["lanes"]),
            "lanes_total": result["lanes_total"],
            "lane_cap_hit": result["lane_cap_hit"],
            "intervals": sum(len(lane["intervals"]) for lane in result["lanes"]),
            "unpaired_starts": result["unpaired_starts"],
            "orphan_ends": result["orphan_ends"],
            "rows_truncated": result["rows_truncated"],
            "undated": result["undated"],
            "top_lanes": [
                {"key": lane["key"], "count": lane["count"], "intervals": len(lane["intervals"])}
                for lane in result["lanes"][:5]
            ],
        }
    elif data_kind == "pivot":
        applied["limit_x"] = _capped(opts.limit_x, limits.pivot_limit, "limit_x")
        applied["limit_y"] = _capped(opts.limit_y, limits.pivot_limit, "limit_y")
        result = await run_gated_scan(
            functools.partial(
                service.field_pivot,
                **({"derive_x": spec.derive} if spec.derive is not None else {}),
            ),
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
    elif data_kind == "table":
        applied["top_n"] = _capped(opts.top_n, limits.table_rows, "top_n")
        sort_by = opts.table_sort_by or "count"
        sort_dir = opts.table_sort_dir or "desc"
        applied["table_sort_by"] = sort_by
        applied["table_sort_dir"] = sort_dir
        result = await run_gated_scan(
            functools.partial(
                service.field_table,
                second_field=spec.field_y,
                sort_by=sort_by,
                sort_dir=sort_dir,
                **derive_kw,
            ),
            primary_query,
            spec.field,
            applied["top_n"],
        )
        summary = {
            "total": result["total"],
            "distinct": result["distinct"],
            "rows": [
                {"value": r["value"], "count": r["count"], "share": r["share"]}
                for r in result["rows"][:5]
            ],
            "remainder": result["remainder"],
        }
        if opts.highlight:
            shown = {r["value"] for r in result["rows"]}
            missing = [v for v in opts.highlight if v not in shown]
            if missing:
                warnings.append(
                    f"options.highlight names value(s) not among the shown rows: {', '.join(missing)}."
                )
    elif data_kind == "corr":
        # `fields` is already validated (2–limits.corr_max_fields, distinct)
        # by the multi_field guard above, so no capping happens here.
        fields = spec.fields or []
        result = await run_gated_scan(service.field_correlation, primary_query, fields)
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
        applied["sample_limit"] = _capped(opts.sample_limit, limits.scatter_sample, "sample_limit")
        result = await run_gated_scan(
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
                # "default" means normality was never tested — quote the
                # coefficient, not a verdict nothing measured.
                "recommendation_basis": stats_block["recommendation_basis"],
            }

    # Presentation options don't reach the query, but belong in the echo —
    # they are part of what the analyst will see.
    for key in meta.reads_options:
        if key in applied or key in inert_options:
            continue
        value = getattr(opts, key)
        if value is not None:
            applied[key] = value

    if resolved_marks is not None:
        summary = dict(summary)
        summary["marks"] = {
            "shown": len(resolved_marks["marks"]),
            "sources": resolved_marks["sources"],
        }
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
            "options": applied,
            "derive": spec.derive.model_dump(exclude_none=True) if spec.derive else None,
            "inputs": spec.inputs.model_dump(exclude_none=True) if spec.inputs else None,
            "marks": (
                [m.model_dump(exclude_none=True, mode="json") for m in spec.marks]
                if spec.marks
                else None
            ),
        },
        "warnings": warnings,
        "summary": summary,
        "result": result,
        "marks": resolved_marks,
    }
