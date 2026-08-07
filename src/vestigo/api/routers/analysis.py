"""The Investigate surface's read endpoints: the analysis plan, and its findings.

Both are thin. The plan endpoint assembles a :class:`PlanInputs` snapshot from
data already cached elsewhere — the per-source ``field_stats`` payload and one
timestamp-range probe — and hands it to ``db/analysis_plan.py``'s pure
predicates, so it answers without scanning a single event.

Neither endpoint ever restricts what can run. A method the plan reports as
``not_applicable`` executes exactly as it would have without a gate; the plan
is advice plus an audit record of what was considered, never a lock. That
property is what lets the UI hide a method from the default sweep without
hiding it from the analyst.

Both endpoints are ``require_case_read``.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from starlette.concurrency import run_in_threadpool

from vestigo.api.deps import get_store, require_case_read
from vestigo.api.routers.events import (
    _get_stat_anomaly_service,
    _resolve_timeline_scope,
    _run_stat_detector,
    _serialize_stat_result,
)
from vestigo.core.config import get_settings
from vestigo.db._buckets import query_timestamp_range
from vestigo.db._dt import ensure_utc
from vestigo.db.analysis_cache import cache_get, cache_put, enrichment_generation, fingerprint
from vestigo.db.analysis_plan import (
    PlanInputs,
    build_plan,
    numeric_tokens_from_stats,
    series_distinct_from_stats,
)
from vestigo.db.field_stats import ensure_source_field_stats, merged_inventory
from vestigo.db.postgres import Case, dispositions_hash

router = APIRouter(prefix="/api/cases", tags=["analysis"])

#: The field the sequence and interval methods group by, matching the
#: ``series_field`` default on ``GET /anomalies``.
DEFAULT_SERIES_FIELD = "artifact"


async def _collect_plan_inputs(
    case_id: str,
    timeline_id: str,
    source_ids: list[str],
    frame: str,
    baseline_id: str | None,
) -> PlanInputs:
    """Assemble the gate's snapshot from already-cached data plus one probe.

    The ``field_stats`` read is the same self-healing path ``viz.py`` and the
    field wizards use, so a cache miss here costs exactly what those already
    cost and warms the cache for them too. The timestamp probe is a
    ``min``/``max`` over a sorted column — cheap enough not to need its own
    cache layer.
    """
    cfg = get_settings()
    store = get_store()
    svc = _get_stat_anomaly_service()

    if not source_ids:
        return PlanInputs(
            inventory=[],
            numeric_tokens=[],
            series_distinct=0,
            events_total=0,
            span_seconds=0.0,
            frame=frame,
            has_active_baseline=baseline_id is not None,
        )

    stats = await ensure_source_field_stats(store, svc.ch, case_id, source_ids)
    inventory, events_total = merged_inventory(stats)

    min_ts, max_ts = query_timestamp_range(
        svc.ch.client,
        svc.ch.database,
        "case_id = {case_id:String} AND source_id IN {source_ids:Array(String)}",
        {"case_id": case_id, "source_ids": source_ids},
    )
    span_seconds = (max_ts - min_ts).total_seconds() if min_ts and max_ts else 0.0

    return PlanInputs(
        inventory=inventory,
        numeric_tokens=numeric_tokens_from_stats(stats, cfg.analysis_gate_min_numeric_ratio),
        series_distinct=series_distinct_from_stats(
            stats,
            DEFAULT_SERIES_FIELD,
            next((d for token, d, _c in inventory if token == DEFAULT_SERIES_FIELD), 0),
        ),
        events_total=events_total,
        span_seconds=span_seconds,
        frame=frame,
        has_active_baseline=baseline_id is not None,
    )


async def _scope_object(
    case_id: str, timeline_id: str, frame: str, baseline_id: str | None
) -> dict[str, Any]:
    """The scope every response is stamped with.

    A finding is meaningless without the comparison that produced it, so this
    object travels with every plan and every findings response, and is what a
    disposition records when the analyst reaches a verdict.

    An unresolvable ``baseline_id`` is a 404 rather than a silent fall back to
    the self frame: quietly answering a different question than the one asked
    is the failure mode this whole object exists to prevent.
    """
    baseline_name = None
    if baseline_id is not None:
        definition = await get_store().get_baseline_definition(case_id, timeline_id, baseline_id)
        if definition is None:
            raise HTTPException(status_code=404, detail="Baseline definition not found")
        baseline_name = definition.name
    return {"frame": frame, "baseline_id": baseline_id, "baseline_name": baseline_name}


@router.get("/{case_id}/timelines/{timeline_id}/analysis/plan")
async def get_analysis_plan(
    case_id: str,
    timeline_id: str,
    frame: str = Query(default="self", pattern="^(self|baseline)$"),
    baseline_id: str | None = Query(default=None),
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Return one gate verdict per method, without scanning any events.

    ``status`` is one of ``applicable``, ``not_applicable`` (the method cannot
    produce a finding on this data) or ``needs_setup`` (an analyst action makes
    it applicable). ``reason_facts`` carries the arithmetic behind the verdict
    so a client can state the numbers rather than a canned sentence.

    Nothing here is enforcement: every method remains runnable through the
    findings endpoint regardless of its verdict.
    """
    source_ids, _field_mappings, _source_offsets = await _resolve_timeline_scope(
        case_id, timeline_id
    )
    scope = await _scope_object(case_id, timeline_id, frame, baseline_id)
    inputs = await _collect_plan_inputs(case_id, timeline_id, source_ids, frame, baseline_id)
    plans = build_plan(inputs, get_settings())
    return {
        "methods": [
            {
                "method": p.method,
                "status": p.status,
                "reason": p.reason,
                "reason_facts": p.reason_facts,
                "cost_class": p.cost_class,
            }
            for p in plans
        ],
        "scope": scope,
        "events_total": inputs.events_total,
    }


#: Per-method parameter schemas: the params-object field a client sends, mapped
#: to the keyword the underlying runner takes. Declaring this table is what lets
#: the cache key be ``hash(params)`` with no per-method allowlist — a method
#: cannot get a wrong key by omission, because a key it does not declare is
#: rejected outright rather than silently dropped.
METHOD_PARAMS: dict[str, dict[str, str]] = {
    "value_novelty": {"fields": "fields"},
    "value_combo": {"fields": "fields"},
    "numeric_range": {"fields": "fields"},
    "charset": {"fields": "fields", "group_field": "group_field"},
    "entropy": {"fields": "fields"},
    "frequency": {"series_field": "series_field", "z_threshold": "z_threshold"},
    "proportion_shift": {"fields": "fields", "fdr_q": "fdr_q", "min_ratio": "min_ratio"},
    "value_distribution_drift": {"fields": "fields", "fdr_q": "fdr_q"},
    "interval_periodicity": {
        "series_field": "series_field",
        "fdr_q": "fdr_q",
        "min_ratio": "min_ratio",
    },
    "timestamp_order": {"min_skew_seconds": "min_skew_seconds"},
    "sequence_novelty": {
        "series_field": "series_field",
        "ngram_size": "ngram_size",
        "max_gap_seconds": "max_gap_seconds",
    },
    #: Not a `_run_stat_detector` detector — log templating is a browser with
    #: its own service call (see :func:`_run_log_templates`). Routing it through
    #: the detector dispatch would run a different analysis under this label.
    "log_template": {"field": "field", "order": "order", "only_new": "only_new"},
}


def _adapt_params(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Map a params object onto runner keywords, rejecting unknown keys.

    Rejecting rather than ignoring matters for the cache: a typo'd knob that was
    silently dropped would compute the *default* answer and store it under a key
    claiming the typo'd parameters, so a later analyst would be served a cached
    answer to a question nobody asked.
    """
    schema = METHOD_PARAMS[method]
    unknown = sorted(set(params) - set(schema))
    if unknown:
        raise HTTPException(
            status_code=422,
            detail=(
                f"Unknown parameter(s) for {method}: {', '.join(unknown)}. "
                f"Accepted: {', '.join(sorted(schema))}."
            ),
        )
    return {schema[k]: v for k, v in params.items()}


async def _run_log_templates(
    case_id: str,
    timeline_id: str,
    source_ids: list[str],
    params: dict[str, Any],
    limit: int,
    baseline_id: str | None = None,
) -> dict[str, Any]:
    """Run the log-template browser and shape its result like a findings response.

    Templating is a browser rather than a scored detector, so it has no
    ``baseline_size`` or ``z_threshold``; the fields it cannot supply are
    reported as their empty values rather than invented.

    ``only_new`` splits "seen before" from "new" at the active baseline's end,
    so the baseline comes from the request's *scope* rather than from a params
    key. Carrying it in params too would let a client ask for templates new
    against one baseline while the response claimed the scope of another.
    """
    store = get_store()
    svc = _get_stat_anomaly_service()
    _source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(
        case_id, timeline_id
    )

    baseline_end = None
    only_new = bool(params.get("only_new"))
    if only_new:
        definition = (
            await store.get_baseline_definition(case_id, timeline_id, baseline_id)
            if baseline_id
            else None
        )
        if definition is None:
            raise HTTPException(
                status_code=422,
                detail="only_new requires the request scope to name a baseline definition",
            )
        baseline_end = ensure_utc(definition.baseline_end)

    result = await run_in_threadpool(
        svc.list_log_templates,
        case_id=case_id,
        source_ids=source_ids,
        field=params.get("field", "message"),
        limit=limit,
        order=params.get("order", "count"),
        baseline_end=baseline_end,
        only_new=only_new,
        field_mappings=field_mappings,
        source_offsets=source_offsets,
    )
    payload = asdict(result)
    templates = payload.get("templates", [])
    return {
        "status": payload.get("status", "ok"),
        "results": templates,
        "total_findings": len(templates),
        "warnings": payload.get("warnings", []),
    }


async def _normal_dispositions_hash(case_id: str, timeline_id: str, source_ids: list[str]) -> str:
    """Hash the timeline's detection-affecting verdicts, across every detector.

    Deliberately method-agnostic, unlike the per-detector hash
    ``_run_stat_detector`` computes for its own forensic snapshot. A
    method-specific hash would need the same query issued before the run just to
    build a cache key; taking the timeline-wide superset instead means a verdict
    recorded for one method also invalidates the others' cached answers. That is
    over-invalidation, never under-invalidation — the only safe direction, and
    it costs a rescan rather than a wrong answer.
    """
    rows = await get_store().list_dispositions(
        case_id, timeline_id=timeline_id, source_ids=source_ids, kinds=["normal"]
    )
    return dispositions_hash(rows)


@router.get("/{case_id}/timelines/{timeline_id}/analysis/findings")
async def get_analysis_findings(
    case_id: str,
    timeline_id: str,
    method: str = Query(..., description="Method id, as reported by /analysis/plan."),
    params: str | None = Query(default=None, description="JSON object of per-method parameters."),
    frame: str = Query(default="self", pattern="^(self|baseline)$"),
    baseline_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    include_dismissed: bool = Query(
        default=False,
        description=(
            "Keep dismissed-disposition findings in `results`, flagged rather "
            "than filtered. Bypasses the cache in both directions — it is a "
            "presentation-only reveal, and caching both variants would double "
            "every key for a mode used rarely."
        ),
    ),
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Run one method under one scope, serving a cache hit when the data is unchanged.

    The gate never restricts this endpoint: a method the plan reports as
    ``not_applicable`` runs exactly as it would have without a gate, and returns
    exactly what an unconditional sweep would have returned.
    """
    if method not in METHOD_PARAMS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown method '{method}'. Known: {', '.join(sorted(METHOD_PARAMS))}.",
        )
    try:
        parsed = json.loads(params) if params else {}
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=422, detail=f"params is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="params must be a JSON object.")

    cfg = get_settings()
    store = get_store()
    source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(case_id, timeline_id)
    scope = await _scope_object(case_id, timeline_id, frame, baseline_id)
    kwargs = _adapt_params(method, parsed)

    sources = await store.list_timeline_sources(case_id, timeline_id)
    key = fingerprint(
        timeline_id=timeline_id,
        source_hashes=[s.file_hash for s in sources if s.id in set(source_ids)],
        enrichment_generation=await enrichment_generation(store, source_ids),
        frame=frame,
        baseline_id=baseline_id,
        method=method,
        params=parsed,
        dispositions_hash=await _normal_dispositions_hash(case_id, timeline_id, source_ids),
    )
    if not include_dismissed:
        cached = await cache_get(store, case_id, key)
        if cached is not None:
            return {**cached, "cache": "hit"}

    if method == "log_template":
        body = await _run_log_templates(
            case_id, timeline_id, source_ids, kwargs, limit, baseline_id=baseline_id
        )
    else:
        result, resolution = await _run_stat_detector(
            case_id,
            timeline_id,
            source_ids,
            detector=method,
            fields=kwargs.get("fields"),
            series_field=kwargs.get("series_field", DEFAULT_SERIES_FIELD),
            z_threshold=kwargs.get("z_threshold"),
            baseline_id=baseline_id,
            limit=limit,
            min_skew_seconds=kwargs.get("min_skew_seconds"),
            fdr_q=kwargs.get("fdr_q"),
            min_ratio=kwargs.get("min_ratio"),
            ngram_size=kwargs.get("ngram_size"),
            group_field=kwargs.get("group_field"),
            max_gap_seconds=kwargs.get("max_gap_seconds"),
            # Both come from _resolve_timeline_scope and are not optional
            # niceties: without field_mappings a canonical field alias is
            # ignored, and without source_offsets a declared per-source
            # clock-skew correction is silently dropped.
            field_mappings=field_mappings,
            source_offsets=source_offsets,
        )
        body = _serialize_stat_result(result)
        scope = {**scope, "dispositions_hash": resolution.get("dispositions_hash")}

    payload = {
        **body,
        "method": method,
        "scope": scope,
        "computed_at": datetime.now(UTC).isoformat(),
    }
    if not include_dismissed:
        await cache_put(store, case_id, key, payload, cfg.analysis_cache_max_rows_per_case)
    return {**payload, "cache": "miss"}
