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
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from starlette.concurrency import run_in_threadpool

from vestigo.api.deps import get_store, require_case_read
from vestigo.api.routers.events import (
    _apply_confirmations,
    _apply_dismissals,
    _get_stat_anomaly_service,
    _resolve_timeline_scope,
    _run_stat_detector,
    _serialize_stat_result,
)
from vestigo.core.config import get_settings
from vestigo.db._buckets import query_timestamp_range
from vestigo.db._dt import ensure_utc
from vestigo.db.analysis_cache import cache_get, cache_put, enrichment_generation, fingerprint
from vestigo.db.analysis_cache import detector_settings as detector_settings_material
from vestigo.db.analysis_plan import (
    PlanInputs,
    build_plan,
    numeric_tokens_from_stats,
    series_distinct_from_stats,
)
from vestigo.db.field_stats import (
    approximate_canonical_inventory,
    ensure_source_field_stats,
    merged_inventory,
)
from vestigo.db.postgres import Case, _windows_config_hash, dispositions_hash

router = APIRouter(prefix="/api/cases", tags=["analysis"])

#: The field the sequence and interval methods group by, matching the
#: ``series_field`` default on ``GET /anomalies``.
DEFAULT_SERIES_FIELD = "artifact"


def _validate_scope_args(frame: str, baseline_id: str | None) -> None:
    """Reject a request whose frame and baseline id describe different questions.

    The runners key off ``baseline_id`` alone while ``frame`` is what the
    response — and therefore every verdict's recorded provenance — is stamped
    with. ``frame=self`` plus an id would run the two-window comparison and
    label the result "all events scanned"; ``frame=baseline`` without one would
    do the reverse, and ``build_plan`` would disagree with the runner about the
    same request. Neither is recoverable after the fact, so neither is guessed.
    """
    if frame == "baseline" and baseline_id is None:
        raise HTTPException(
            status_code=422,
            detail="frame=baseline requires baseline_id — name the definition to compare against.",
        )
    if frame == "self" and baseline_id is not None:
        raise HTTPException(
            status_code=422,
            detail="frame=self takes no baseline_id — pass frame=baseline to compare against one.",
        )


async def _collect_plan_inputs(
    case_id: str,
    timeline_id: str,
    source_ids: list[str],
    frame: str,
    baseline_id: str | None,
    field_mappings: dict[str, list[str]] | None,
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
    # With the timeline's mappings, exactly as every detector path resolves
    # them. Without them the gate counts each mapped raw key as its own field
    # and never sees the canonical token, so `reason_facts` — which the Tools
    # sheet renders verbatim as arithmetic the analyst is invited to check —
    # would describe a field set no detector uses. The canonical entries are
    # approximated from the same cache rather than aggregated live, because
    # this endpoint's contract is that it answers without a scan.
    inventory, events_total = merged_inventory(stats, field_mappings)
    inventory = inventory + approximate_canonical_inventory(stats, field_mappings)

    # Offloaded: this is a blocking ClickHouse round-trip in an async handler,
    # and the rail fires a dozen findings requests immediately after the plan
    # resolves — holding the event loop here stalls all of them.
    min_ts, max_ts = await run_in_threadpool(
        query_timestamp_range,
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
) -> tuple[dict[str, Any], str | None]:
    """The scope every response is stamped with, plus the definition's content hash.

    A finding is meaningless without the comparison that produced it, so this
    object travels with every plan and every findings response, and is what a
    disposition records when the analyst reaches a verdict.

    An unresolvable ``baseline_id`` is a 404 rather than a silent fall back to
    the self frame: quietly answering a different question than the one asked
    is the failure mode this whole object exists to prevent.

    The content hash is returned *beside* the scope rather than inside it. It is
    cache-key material, and the scope object is recorded verbatim as a verdict's
    provenance — putting it there would make two verdicts on the same comparison
    fail to dedupe the moment the definition was edited.
    """
    baseline_name = None
    config_hash = None
    if baseline_id is not None:
        definition = await get_store().get_baseline_definition(case_id, timeline_id, baseline_id)
        if definition is None:
            raise HTTPException(status_code=404, detail="Baseline definition not found")
        baseline_name = definition.name
        config_hash = _windows_config_hash(definition.windows_payload())
    return {"frame": frame, "baseline_id": baseline_id, "baseline_name": baseline_name}, config_hash


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
    _validate_scope_args(frame, baseline_id)
    source_ids, field_mappings, _source_offsets = await _resolve_timeline_scope(
        case_id, timeline_id
    )
    scope, _config_hash = await _scope_object(case_id, timeline_id, frame, baseline_id)
    inputs = await _collect_plan_inputs(
        case_id, timeline_id, source_ids, frame, baseline_id, field_mappings
    )
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


class _Params(BaseModel):
    """Base for every method's parameter model.

    One model per method is the single declaration of that method's knobs: the
    accepted keys, their types and their bounds. Rejecting an unknown key
    matters for the cache — a typo'd knob that was silently dropped would
    compute the *default* answer and store it under a key claiming the typo'd
    parameters, so a later analyst would be served a cached answer to a question
    nobody asked. Rejecting a wrong *type* matters for the same reason plus one
    more: the runners take these values unchecked, so a list where a string is
    expected is a 500 rather than a 422.

    Bounds mirror the ``Query(...)`` constraints ``GET /anomalies`` declares for
    the same knobs. The model's field names are the runner keywords; where a
    client-facing name ever needs to differ, declare it as a Pydantic ``alias``
    rather than reintroducing a second mapping table.
    """

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    def runner_kwargs(self) -> dict[str, Any]:
        """Validated params as runner keywords, defaults included.

        Defaults are included deliberately: this dict is also the cache key
        material, and "omitted" and "sent at its default value" are the same
        question and must hash the same.
        """
        return self.model_dump()


class _FieldsParams(_Params):
    """Methods that take a field selection and nothing else."""

    fields: str | None = None

    @field_validator("fields", mode="before")
    @classmethod
    def _join_fields(cls, v: Any) -> Any:
        """Accept a JSON list as well as the comma-joined string.

        A list is the natural JSON encoding of a multi-field knob, and it is
        what a client writes first. ``_parse_novelty_fields`` splits a string,
        so normalize here rather than letting ``.split`` fail downstream. The
        ``__none__`` sentinel (explicitly "no fields") passes through as-is.
        """
        if isinstance(v, list | tuple):
            return ",".join(str(x) for x in v)
        return v


class _ValueNoveltyParams(_FieldsParams):
    pass


class _ValueComboParams(_FieldsParams):
    pass


class _NumericRangeParams(_FieldsParams):
    pass


class _EntropyParams(_FieldsParams):
    pass


class _CharsetParams(_FieldsParams):
    group_field: str | None = None


class _FrequencyParams(_Params):
    series_field: str = DEFAULT_SERIES_FIELD
    z_threshold: float | None = Field(default=None, gt=0)


class _ProportionShiftParams(_FieldsParams):
    fdr_q: float | None = Field(default=None, gt=0, le=1)
    min_ratio: float | None = Field(default=None, gt=1)


class _ValueDistributionDriftParams(_FieldsParams):
    fdr_q: float | None = Field(default=None, gt=0, le=1)


class _IntervalPeriodicityParams(_Params):
    series_field: str = DEFAULT_SERIES_FIELD
    fdr_q: float | None = Field(default=None, gt=0, le=1)
    min_ratio: float | None = Field(default=None, gt=1)


class _TimestampOrderParams(_Params):
    min_skew_seconds: float | None = Field(default=None, ge=0)


class _SequenceNoveltyParams(_Params):
    series_field: str = DEFAULT_SERIES_FIELD
    ngram_size: int | None = Field(default=None, ge=2, le=5)
    max_gap_seconds: int | None = Field(default=None, ge=1)


class _LogTemplateParams(_Params):
    #: Not a `_run_stat_detector` detector — log templating is a browser with
    #: its own service call (see :func:`_run_log_templates`). Routing it through
    #: the detector dispatch would run a different analysis under this label.
    field: str = "message"
    order: Literal["count", "first_seen", "last_seen"] = "count"
    only_new: bool = False


METHOD_MODELS: dict[str, type[_Params]] = {
    "value_novelty": _ValueNoveltyParams,
    "value_combo": _ValueComboParams,
    "numeric_range": _NumericRangeParams,
    "charset": _CharsetParams,
    "entropy": _EntropyParams,
    "frequency": _FrequencyParams,
    "proportion_shift": _ProportionShiftParams,
    "value_distribution_drift": _ValueDistributionDriftParams,
    "interval_periodicity": _IntervalPeriodicityParams,
    "timestamp_order": _TimestampOrderParams,
    "sequence_novelty": _SequenceNoveltyParams,
    "log_template": _LogTemplateParams,
}

#: Accepted params-object keys per method, derived from the models so the two
#: cannot drift. Read by the frontend's method-registry test, which asserts
#: every knob it declares is a key this endpoint accepts.
METHOD_PARAMS: dict[str, set[str]] = {
    method: set(model.model_fields) for method, model in METHOD_MODELS.items()
}


def _adapt_params(method: str, params: dict[str, Any]) -> dict[str, Any]:
    """Validate a params object against its method's model, returning runner keywords."""
    try:
        model = METHOD_MODELS[method].model_validate(params)
    except ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in err['loc']) or 'params'}: {err['msg']}"
            for err in exc.errors()
        )
        raise HTTPException(
            status_code=422,
            detail=(
                f"Invalid parameters for {method}: {problems}. "
                f"Accepted: {', '.join(sorted(METHOD_PARAMS[method]))}."
            ),
        ) from exc
    return model.runner_kwargs()


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


async def _apply_dispositions(
    case_id: str,
    timeline_id: str,
    source_ids: list[str],
    method: str,
    payload: dict[str, Any],
    include_dismissed: bool,
) -> dict[str, Any]:
    """Filter dismissed findings and badge confirmed ones, as ``/anomalies`` does.

    The same two functions the older endpoint uses, applied at the same point in
    the response's life — presentation, after the answer exists. Without this a
    dismissal is undone by the next refetch and a confirmed finding never
    carries the flag its badge reads.

    ``log_template`` rows carry neither ``event_id`` nor
    ``details.allowlist_*``, so both appliers are no-ops there. That is the
    correct outcome rather than an oversight: a template is a browsing row, and
    nothing records a verdict against one.
    """
    if not payload.get("results"):
        payload["dismissed_count"] = 0
        return payload
    # Both appliers flag findings in place, and `results` may still be the list
    # the cache row holds. Copy the rows before touching them.
    payload["results"] = [dict(f) for f in payload["results"]]
    rows = await get_store().list_dispositions(
        case_id,
        timeline_id=timeline_id,
        source_ids=source_ids,
        kinds=["dismissed", "confirmed"],
        detector=method,
    )
    payload = _apply_dismissals(
        payload, [d for d in rows if d.kind == "dismissed"], include_dismissed
    )
    return _apply_confirmations(payload, [d for d in rows if d.kind == "confirmed"])


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
            "than filtered. Presentation-only: dismissals are applied to the "
            "response after the cache, so this changes what is shown and never "
            "what is computed or stored."
        ),
    ),
    case: Case = Depends(require_case_read),
) -> dict[str, Any]:
    """Run one method under one scope, serving a cache hit when the data is unchanged.

    The gate never restricts this endpoint: a method the plan reports as
    ``not_applicable`` runs exactly as it would have without a gate, and returns
    exactly what an unconditional sweep would have returned.

    Dismissals and confirmations are applied *after* the cache, never before.
    The fingerprint covers ``kind="normal"`` verdicts only — those change what a
    detector computes — so a cached payload that had already been
    dismissal-filtered would keep a later dismissal invisible, and a later
    confirmation unbadged, until something unrelated invalidated the key.
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

    _validate_scope_args(frame, baseline_id)
    cfg = get_settings()
    store = get_store()
    source_ids, field_mappings, source_offsets = await _resolve_timeline_scope(case_id, timeline_id)
    scope, baseline_config_hash = await _scope_object(case_id, timeline_id, frame, baseline_id)
    kwargs = _adapt_params(method, parsed)

    scoped = set(source_ids)
    sources = await store.list_timeline_sources(case_id, timeline_id)
    key = fingerprint(
        timeline_id=timeline_id,
        # `or ""` rather than a filter: a source still without its hash is part
        # of the scope, and dropping it would give it the key of a timeline that
        # never had it. sorted() also refuses to compare None with str.
        source_hashes=[s.file_hash or "" for s in sources if s.id in scoped],
        enrichment_generation=await enrichment_generation(store, source_ids),
        frame=frame,
        baseline_id=baseline_id,
        # The definition's *contents*, not just its id: `PUT .../baselines/{id}`
        # replaces the windows in place, so an edited baseline keeps its id and
        # would otherwise be served the pre-edit answer under the new name.
        baseline_config_hash=baseline_config_hash,
        field_mappings=field_mappings,
        source_offsets=source_offsets,
        detector_settings=detector_settings_material(cfg),
        method=method,
        # The validated params, not the raw object: `2` and `2.0` are the same
        # question and must share a key.
        params=kwargs,
        limit=limit,
        dispositions_hash=await _normal_dispositions_hash(case_id, timeline_id, source_ids),
    )
    cached = await cache_get(store, case_id, key)
    if cached is not None:
        return {
            **await _apply_dispositions(
                case_id, timeline_id, source_ids, method, dict(cached), include_dismissed
            ),
            "cache": "hit",
        }

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
    await cache_put(store, case_id, key, payload, cfg.analysis_cache_max_rows_per_case)
    return {
        **await _apply_dispositions(
            case_id, timeline_id, source_ids, method, dict(payload), include_dismissed
        ),
        "cache": "miss",
    }
