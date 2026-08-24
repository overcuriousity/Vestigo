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

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from fastapi.concurrency import run_in_threadpool

from vestigo.agent.chart_meta import (
    CHART_META,
    METRIC_INFO,
    chart_types_for,
    compare_capable,
    metric_available,
    requires_field,
)
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
    scatter_sample=(5000, 20000),
    corr_max_fields=8,
    points_overlay_max=1000,
    group_cardinality_caution=50,
)


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
                listed = await run_in_threadpool(
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

    applied: dict[str, Any] = {}
    #: Options this chart type nominally reads but that this *particular*
    #: spec made inert (a bounded time axis ignores its limit). Kept out
    #: of the `resolved` echo below, which otherwise re-adds them.
    inert_options: set[str] = set()

    # ── execute, dispatching on the aggregation the mark needs ───────────
    if data_kind == "terms":
        applied["top_n"] = _capped(opts.top_n, limits.terms_top_n, "top_n")
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
        applied["groups"] = _capped(opts.groups, limits.groups, "groups", floor=2)
        applied["bins"] = _capped(opts.bins, limits.bins, "bins")
        result = await run_in_threadpool(
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
            bins_arg = _capped(opts.bins, limits.bins, "bins") if opts.bins is not None else None
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
        applied["buckets"] = _capped(opts.buckets, limits.series_buckets, "buckets", floor=4)
        applied["top_n"] = _capped(opts.top_n, limits.series_top_n, "top_n")
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
        applied["buckets"] = _capped(opts.buckets, limits.time_buckets, "buckets", floor=4)
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
            result = await run_in_threadpool(service.histogram, primary_query, applied["buckets"])
            summary = {
                "buckets": len(result["buckets"]),
                "interval_seconds": result["interval_seconds"],
            }
    elif data_kind == "punchcard":
        result = await run_in_threadpool(service.time_punchcard, primary_query)
        summary = {"total": result["total"], "max_count": result["max_count"]}
    elif data_kind == "pivot":
        applied["limit_x"] = _capped(opts.limit_x, limits.pivot_limit, "limit_x")
        applied["limit_y"] = _capped(opts.limit_y, limits.pivot_limit, "limit_y")
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
        # `fields` is already validated (2–limits.corr_max_fields, distinct)
        # by the multi_field guard above, so no capping happens here.
        fields = spec.fields or []
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
        applied["sample_limit"] = _capped(opts.sample_limit, limits.scatter_sample, "sample_limit")
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
        },
        "warnings": warnings,
        "summary": summary,
        "result": result,
    }
