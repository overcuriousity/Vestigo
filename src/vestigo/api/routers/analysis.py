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

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query

from vestigo.api.deps import get_store, require_case_read
from vestigo.api.routers.events import _get_stat_anomaly_service, _resolve_timeline_scope
from vestigo.core.config import get_settings
from vestigo.db._buckets import query_timestamp_range
from vestigo.db.analysis_plan import (
    PlanInputs,
    build_plan,
    message_tokens_from_inventory,
    numeric_tokens_from_stats,
)
from vestigo.db.field_stats import ensure_source_field_stats, merged_inventory
from vestigo.db.postgres import Case

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
            message_tokens=[],
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
        message_tokens=message_tokens_from_inventory(inventory),
        series_distinct=next((d for token, d, _c in inventory if token == DEFAULT_SERIES_FIELD), 0),
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
