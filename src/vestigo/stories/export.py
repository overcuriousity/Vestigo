"""Server-authoritative Story export resolution (W7).

``resolve_story_snapshot`` freezes a story into the versioned ``"v": 1``
snapshot bundle: every block resolved against live data at export time,
each individually wrapped so a dangling reference or query failure becomes
an explicit ``resolution.error`` — visible in the artifact, never a
silently dropped block. Truncation is always flagged (a report that shows
200 of 14203 rows says so).

Query execution reuses the existing paths — ``EventQueryService.query``
for view/event blocks (via the agent's ``_build_query``, so field
mappings, tag filters and clock-skew offsets apply exactly as in the
Explorer) and ``execute_chart_spec`` for chart blocks. The keyword hooks
(``run_event_query``, ``run_chart``, ``resolve_scope``, ``now``) exist for
tests; production callers pass none of them.
"""

from __future__ import annotations

import math
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from fastapi.concurrency import run_in_threadpool

from vestigo.db._scan import unbounded_foreground_wait
from vestigo.stories.schemas import canonical_json

if TYPE_CHECKING:
    from vestigo.db.postgres import Story, StoryBlock, User, View


class SnapshotTooLargeError(Exception):
    """The resolved snapshot crossed its byte ceiling mid-resolution.

    Raised by ``resolve_story_snapshot`` as soon as the running total passes
    the cap, rather than after the whole bundle exists. Checking only at the
    end bounds what gets *stored* while still materializing an arbitrarily
    large bundle first — 500 blocks of 1000 frozen rows (or 20000 scatter
    points) live in memory, plus a second copy as the serialized string,
    before anything rejects them. The block that crossed the line is named so
    the analyst knows which embed to shrink.
    """

    def __init__(self, resolved_bytes: int, cap: int, block_id: str) -> None:
        super().__init__(
            f"resolved snapshot exceeds {cap} bytes at block {block_id!r} "
            f"({resolved_bytes} bytes so far)"
        )
        self.resolved_bytes = resolved_bytes
        self.cap = cap
        self.block_id = block_id


def _json_safe(value: Any) -> Any:
    """Coerce a resolved payload into JSON-round-trippable types.

    Query results are not JSON to begin with: ClickHouse hands back
    ``FixedString`` columns (``content_hash``, ``file_hash``,
    ``embedding_config_hash``) as raw ``bytes``, and aggregations can carry
    ``datetime``/``Decimal``. A snapshot is stored *and hashed* as JSON, so
    anything that cannot round-trip has to be coerced here rather than
    blowing up the export — or, worse, hashing a payload that later reads
    back differently.

    Two coercions are less obvious than they look:

    * ``NaN``/``Infinity`` are not JSON. Python emits them as bare literals
      that no conforming parser accepts, Postgres rejects them on insert, and
      the hash would cover bytes nobody can re-parse. They become ``None`` —
      "this aggregate had no defined value", which is what they mean.
    * A ``set`` iterates in an order that varies between processes, so
      freezing one as a list would make the snapshot (and its hash)
      irreproducible. Sorted by string form to pin it down.
    """
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, set | frozenset):
        return sorted((_json_safe(v) for v in value), key=repr)
    if isinstance(value, list | tuple):
        return [_json_safe(v) for v in value]
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, str | int | float):
        return value
    return str(value)


def _validate_filters(fspec):
    """Run the Explorer's regex checks over a FilterSpec, returning it.

    Mirrors the ``validated`` helper ``execute_chart_spec`` builds; kept here
    so the view-block path can't drift away from the chart path.
    """
    from vestigo.api.routers.events import _validate_field_modes, _validate_regex

    _validate_regex(fspec.q, fspec.q_regex)
    _validate_field_modes(fspec.filters, fspec.filter_modes)
    _validate_field_modes(fspec.exclusions, fspec.exclusion_modes)
    return fspec


def _view_filter_to_spec(view: View):
    """Map a stored ``View.view_filter`` payload onto the agent's FilterSpec."""
    return _filter_payload_to_spec(view.view_filter or {})


def _filter_payload_to_spec(payload: dict[str, Any]):
    """Map a stored Explorer filter payload onto the agent's FilterSpec.

    The payload is ``filtersToViewPayload``'s shape
    (``frontend/src/lib/queryParams.ts``) — used both by a saved View and by a
    saved chart's custom comparison layer. FilterSpec is the same Explorer
    filter vocabulary, so this is a key rename plus dropping the empties
    (FilterSpec rejects explicit empty selections as select-nothing traps).
    """
    from vestigo.agent.tools import FilterSpec

    p = payload or {}

    def _list(key: str) -> list[str] | None:
        values = p.get(key) or None
        return list(values) if values else None

    artifacts = _list("artifacts") or []
    if p.get("artifact") and p["artifact"] not in artifacts:
        artifacts.append(p["artifact"])
    tags_include = _list("tagsInclude") or []
    if p.get("tag") and p["tag"] not in tags_include:
        tags_include.append(p["tag"])
    tags_exclude = _list("tagsExclude") or []
    if p.get("excludeTag") and p["excludeTag"] not in tags_exclude:
        tags_exclude.append(p["excludeTag"])

    filters = {k: list(v) for k, v in (p.get("filters") or {}).items() if v}
    exclusions = {k: list(v) for k, v in (p.get("exclusions") or {}).items() if v}

    def _dt(key: str) -> datetime | None:
        raw = p.get(key)
        if not raw:
            return None
        return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))

    return FilterSpec(
        q=p.get("q") or None,
        q_regex=bool(p.get("qRegex")),
        artifacts=artifacts or None,
        source_id=p.get("sourceId") or None,
        start=_dt("start"),
        end=_dt("end"),
        filters=filters,
        exclusions=exclusions,
        filter_modes={k: v for k, v in (p.get("filterModes") or {}).items() if k in filters},
        exclusion_modes={
            k: v for k, v in (p.get("exclusionModes") or {}).items() if k in exclusions
        },
        tags_include=tags_include or None,
        tags_exclude=tags_exclude or None,
        annotated=_list("annotated"),
        annotation_tag_value=p.get("annotationTagValue") or None,
        # Agent-only members of FilterSpec. The Explorer cannot produce them
        # and no saved View carries them, but a chart saved from an agent
        # proposal can — and dropping them here would silently widen the
        # chart's scope. Absent everywhere else, so this is a no-op there.
        collapse_routine=bool(p.get("collapseRoutine")),
        event_ids=_list("eventIds"),
        run_id=p.get("runId") or None,
    )


#: Stored ``ChartConfig`` key → ``ChartSpec`` field. The two describe the same
#: chart field for field; only the casing differs (the frontend serializes its
#: own camelCase config, the agent tool takes snake_case). Explicit rather than
#: a generic camel→snake pass so an added key has to be considered here.
_CHART_CONFIG_KEYS = {
    "chartType": "chart_type",
    "scale": "scale",
    "field": "field",
    "fieldY": "field_y",
    "fields": "fields",
    "metric": "metric",
}

_CHART_OPTION_KEYS = {
    "orientation": "orientation",
    "sort": "sort",
    "logScale": "log_scale",
    "seriesMode": "series_mode",
    "legend": "legend",
    "topN": "top_n",
    "bins": "bins",
    "showDensity": "show_density",
    "buckets": "buckets",
    "limitX": "limit_x",
    "limitY": "limit_y",
    "sampleLimit": "sample_limit",
    "groups": "groups",
    "showPoints": "show_points",
    "tableSortBy": "table_sort_by",
    "tableSortDir": "table_sort_dir",
    "highlight": "highlight",
}


#: Version stamp on a stored ``ChartConfig``. The frontend upgrades a ``v: 1``
#: row on read (``upgradeChartConfig``) and refuses anything else
#: (``parseStoredChartConfig``), so anything writing a SavedChart has to set
#: the current value. v2 added ``derive``, ``inputs`` and ``marks``; a v1 row
#: reads as v2 with all three empty.
CHART_CONFIG_VERSION = 2


#: Stored ``ChartConfig.derive.kind`` (camelCase) ↔ ``DeriveSpec.kind``. Only
#: ``kind`` differs in casing; ``mode``/``count``/``edges``/``part`` are shared.
_DERIVE_KIND_TO_SPEC = {"bins": "bins", "timePart": "time_part"}
_DERIVE_KIND_TO_STORED = {v: k for k, v in _DERIVE_KIND_TO_SPEC.items()}


def _stored_derive_to_spec(raw: Any) -> dict[str, Any] | None:
    """Stored derivation → ``ChartSpec.derive`` payload; an unknown shape is none."""
    if not isinstance(raw, dict) or raw.get("kind") not in _DERIVE_KIND_TO_SPEC:
        return None
    out = {k: v for k, v in raw.items() if v is not None}
    out["kind"] = _DERIVE_KIND_TO_SPEC[raw["kind"]]
    return out


def _spec_derive_to_stored(derive: Any) -> dict[str, Any] | None:
    """Inverse of ``_stored_derive_to_spec``."""
    if derive is None:
        return None
    out = derive.model_dump(exclude_none=True)
    out["kind"] = _DERIVE_KIND_TO_STORED[out["kind"]]
    return out


def _stored_chart_to_spec(config: dict[str, Any]):
    """Translate a saved chart's stored config into an executable ChartSpec.

    ``SavedChart.config`` is the frontend's own ``ChartConfig`` round-tripped
    verbatim (the backend never interprets it), so executing one server-side
    means crossing the casing boundary exactly once — here.
    ``spec_to_stored_chart_config`` is the inverse; the two are tested as a
    round trip, because a config written in the wrong shape produces a chart
    that is silently undrawable in every consumer rather than an error.
    """
    from vestigo.agent.tools import ChartSpec

    spec: dict[str, Any] = {}
    for stored_key, spec_key in _CHART_CONFIG_KEYS.items():
        value = config.get(stored_key)
        if value is not None:
            spec[spec_key] = value

    options = config.get("options") or {}
    spec_options = {
        spec_key: options[stored_key]
        for stored_key, spec_key in _CHART_OPTION_KEYS.items()
        if options.get(stored_key) is not None
    }
    if spec_options:
        spec["options"] = spec_options

    derive = _stored_derive_to_spec(config.get("derive"))
    if derive:
        spec["derive"] = derive

    stored_inputs = config.get("inputs")
    if isinstance(stored_inputs, dict) and isinstance(stored_inputs.get("columns"), list):
        spec["inputs"] = {"columns": list(stored_inputs["columns"])}

    # The primary filter layer — the Explorer filters the chart was saved
    # under, stored beside the ChartConfig keys (the frontend's
    # ``chartConfigToStored``). Absent on charts saved before filters were
    # captured, which resolve over the whole timeline as they always did.
    stored_filters = config.get("filters")
    if stored_filters:
        spec["filters"] = _filter_payload_to_spec(stored_filters)

    compare = config.get("compare") or {}
    mode = compare.get("mode", "off")
    if mode != "off":
        spec["compare"] = {"mode": mode}
        if mode == "custom":
            spec["compare"]["filters"] = _filter_payload_to_spec(compare.get("filters") or {})

    return ChartSpec.model_validate(spec)


def _spec_filters_to_payload(fspec: Any) -> dict[str, Any]:
    """Inverse of ``_filter_payload_to_spec``: FilterSpec → stored filter payload."""
    payload: dict[str, Any] = {}
    if fspec is None:
        return payload
    simple = {
        "q": fspec.q,
        "qRegex": fspec.q_regex or None,
        "artifacts": list(fspec.artifacts) if fspec.artifacts else None,
        "sourceId": fspec.source_id,
        "start": fspec.start.isoformat() if fspec.start else None,
        "end": fspec.end.isoformat() if fspec.end else None,
        "filters": dict(fspec.filters) if fspec.filters else None,
        "exclusions": dict(fspec.exclusions) if fspec.exclusions else None,
        "filterModes": dict(fspec.filter_modes) if fspec.filter_modes else None,
        "exclusionModes": dict(fspec.exclusion_modes) if fspec.exclusion_modes else None,
        "tagsInclude": list(fspec.tags_include) if fspec.tags_include else None,
        "tagsExclude": list(fspec.tags_exclude) if fspec.tags_exclude else None,
        "annotated": list(fspec.annotated) if fspec.annotated else None,
        "annotationTagValue": fspec.annotation_tag_value,
        "collapseRoutine": fspec.collapse_routine or None,
        "eventIds": list(fspec.event_ids) if fspec.event_ids else None,
        "runId": fspec.run_id,
    }
    return {**payload, **{k: v for k, v in simple.items() if v is not None}}


def spec_to_stored_chart_config(spec: Any) -> dict[str, Any]:
    """Translate an agent ``ChartSpec`` into a stored ``ChartConfig``.

    The exact inverse of ``_stored_chart_to_spec``. Needed because the agent
    speaks ``ChartSpec`` (snake_case, unversioned) while a persisted
    ``SavedChart.config`` is the frontend's ``ChartConfig`` (camelCase,
    ``v: 1``) — storing the spec's dump directly produces a row that the
    export resolver, the story editor card and the Visualize rail all refuse
    to draw, with no error at write time.

    ``spec.filters`` — the primary filter layer — is stored beside the chart
    keys rather than inside them, matching the frontend: on the live page the
    URL owns those filters, and storage is the one place the chart and the
    slice it describes travel together.
    """
    config: dict[str, Any] = {"v": CHART_CONFIG_VERSION}
    # An all-defaults FilterSpec narrows nothing, so it is stored as no key at
    # all — the same bytes an unfiltered chart from the Visualize page writes.
    stored_filters = _spec_filters_to_payload(spec.filters)
    if stored_filters:
        config["filters"] = stored_filters
    for stored_key, spec_key in _CHART_CONFIG_KEYS.items():
        value = getattr(spec, spec_key, None)
        if value is not None:
            config[stored_key] = value

    dumped_options = spec.options.model_dump() if spec.options is not None else {}
    options = {
        stored_key: dumped_options[spec_key]
        for stored_key, spec_key in _CHART_OPTION_KEYS.items()
        if dumped_options.get(spec_key) is not None
    }
    config["options"] = options

    stored_derive = _spec_derive_to_stored(getattr(spec, "derive", None))
    if stored_derive:
        config["derive"] = stored_derive

    inputs = getattr(spec, "inputs", None)
    if inputs is not None and inputs.columns:
        config["inputs"] = {"columns": list(inputs.columns)}

    mode = getattr(spec.compare, "mode", "off") if spec.compare is not None else "off"
    if mode != "off":
        compare: dict[str, Any] = {"mode": mode}
        if mode == "custom":
            compare["filters"] = _spec_filters_to_payload(spec.compare.filters)
        config["compare"] = compare
    return config


async def resolve_story_snapshot(
    story: Story,
    blocks: list[StoryBlock],
    *,
    user: User,
    store: Any = None,
    run_event_query: Callable[..., Any] | None = None,
    run_chart: Any = None,
    resolve_scope: Any = None,
    now: Callable[[], datetime] | None = None,
    max_bytes: int | None = None,
) -> dict[str, Any]:
    """Resolve every block of *story* into the frozen ``"v": 1`` snapshot.

    Never raises for a bad block — failures freeze as ``resolution.error``.

    ``max_bytes`` caps the resolved bundle. It is enforced *during*
    resolution, block by block, so a story that would blow past it stops
    costing memory and ClickHouse queries at the point it crosses the line
    rather than after the whole thing is built (``SnapshotTooLargeError``).
    ``None`` or ``0`` disables the ceiling.
    """
    from vestigo.agent.tools import AgentScope, _build_query

    if store is None:
        from vestigo.api.deps import get_store

        store = get_store()
    if now is None:
        now = lambda: datetime.now(UTC)  # noqa: E731

    if resolve_scope is None:

        async def _resolve_scope(case_id: str, timeline_id: str):
            from vestigo.api.routers.events import _resolve_timeline_scope

            return await _resolve_timeline_scope(case_id, timeline_id)

    else:
        _sync_resolve = resolve_scope

        async def _resolve_scope(case_id: str, timeline_id: str):
            return _sync_resolve(case_id, timeline_id)

    if run_event_query is None:
        from vestigo.api.routers.events import _get_query_service

        service = _get_query_service()

        async def _run_event_query(query):
            return await run_in_threadpool(service.query, query)

    else:
        _sync_query = run_event_query

        async def _run_event_query(query):
            return _sync_query(query)

    if run_chart is None:
        from functools import partial

        from vestigo.agent.chart_exec import ANALYST_CHART_LIMITS, execute_chart_spec

        # An export freezes the *analyst's* chart, so it runs under the same
        # defaults and ceilings as the interactive card. Executing it under
        # the agent's context-budget caps would quietly show less than the
        # analyst saw — a top-50 bar chart frozen as top-30 — and leak the
        # agent-facing clamp wording into the report.
        run_chart = partial(execute_chart_spec, limits=ANALYST_CHART_LIMITS)

    scope_cache: dict[str, AgentScope] = {}

    async def _scope_for(timeline_id: str) -> AgentScope:
        if timeline_id not in scope_cache:
            source_ids, field_mappings, source_offsets = await _resolve_scope(
                story.case_id, timeline_id
            )
            scope_cache[timeline_id] = AgentScope(
                case_id=story.case_id,
                timeline_id=timeline_id,
                user=user,
                source_ids=source_ids,
                field_mappings=field_mappings,
                source_offsets=source_offsets,
            )
        return scope_cache[timeline_id]

    async def _resolve_markdown(content: dict) -> tuple[dict, dict]:
        return {"text": content.get("text", "")}, {}

    async def _resolve_view(content: dict) -> tuple[dict | None, dict]:
        view = await store.get_view(story.case_id, content["view_id"])
        if view is None:
            raise LookupError(f"view {content['view_id']!r} not found (deleted before export)")
        timeline_id = content["timeline_id"]
        scope = await _scope_for(timeline_id)
        display = content.get("display") or {}
        limit = int(display.get("limit") or 200)
        # Same regex validation every interactive path applies before building
        # a query — a stored View can carry a pattern that was never checked
        # by this code path, and the asymmetry becomes a hole the moment the
        # validator grows a rule that matters.
        query = await _build_query(scope, _validate_filters(_view_filter_to_spec(view)))
        # _build_query clamps limit to the agent's per-search context cap;
        # export blocks carry their own (validated ≤ VIEW_BLOCK_ROW_CAP).
        query.limit = limit
        page = await _run_event_query(query)
        rows = page.events
        total = page.total if page.total is not None else len(rows)
        data = {
            "rows": rows,
            "row_count_total": total,
            "rows_included": len(rows),
            "truncated": total > len(rows),
            "columns": display.get("columns"),
        }
        ref_extra = {
            "name": view.name,
            "query": view.query,
            "filter": view.view_filter or {},
        }
        return data, {"timeline_id": timeline_id, "ref_extra": ref_extra}

    async def _resolve_chart(content: dict) -> tuple[dict | None, dict]:
        chart = await store.get_saved_chart(
            story.case_id, content["timeline_id"], content["chart_id"]
        )
        if chart is None:
            raise LookupError(
                f"saved chart {content['chart_id']!r} not found (deleted before export)"
            )
        try:
            spec = _stored_chart_to_spec(chart.config or {})
        except Exception as exc:
            raise ValueError(
                f"saved chart config (v={((chart.config or {}).get('v'))}) "
                f"did not parse as a chart spec: {exc}"
            ) from exc
        scope = await _scope_for(content["timeline_id"])
        # An export is a job: no spinner to answer to, no request to 503 and
        # no retry. It queues for a foreground slot rather than failing a
        # block because analysts happened to be rendering charts (#305).
        with unbounded_foreground_wait():
            envelope = await run_chart(scope, spec)
        data = {
            "name": chart.name,
            "config": chart.config or {},
            "resolved": envelope.get("resolved"),
            "warnings": envelope.get("warnings", []),
            "chart": envelope.get("result"),
        }
        return data, {"timeline_id": content["timeline_id"], "ref_extra": {"name": chart.name}}

    async def _resolve_event(content: dict) -> tuple[dict | None, dict]:
        from vestigo.db.queries import EventQuery

        query = EventQuery(
            case_id=story.case_id,
            source_ids=[content["source_id"]],
            event_ids=[content["event_id"]],
            limit=1,
        )
        page = await _run_event_query(query)
        if not page.events:
            raise LookupError(
                f"event {content['event_id']!r} not found (source deleted or re-scoped)"
            )
        return {"event": page.events[0], "caption": content.get("caption")}, {}

    resolvers = {
        "markdown": _resolve_markdown,
        "view_ref": _resolve_view,
        "chart_ref": _resolve_chart,
        "event_ref": _resolve_event,
    }

    frozen_blocks: list[dict[str, Any]] = []
    # Running size of the blocks resolved so far. Each block is coerced and
    # measured as it lands, because the point of the ceiling is to stop the
    # work, not just to refuse the result.
    resolved_bytes = 0
    for block in blocks:
        resolution: dict[str, Any] = {"executed_at": now().isoformat(), "error": None}
        ref = dict(block.content or {})
        data: dict | None = None
        try:
            resolver = resolvers.get(block.kind)
            if resolver is None:
                raise ValueError(f"unknown block kind {block.kind!r}")
            data, extra = await resolver(block.content or {})
            if extra.get("timeline_id"):
                resolution["timeline_id"] = extra["timeline_id"]
            ref.update(extra.get("ref_extra") or {})
        except Exception as exc:  # noqa: BLE001 — every failure freezes as an error block
            data = None
            resolution["error"] = str(exc)
        # Coerce per block rather than once over the finished bundle: hash and
        # storage both happen over JSON, and measuring a block means
        # serializing it, which only works on JSON-native types.
        frozen = _json_safe(
            {
                "id": block.id,
                "kind": block.kind,
                "origin": block.origin,
                "ref": ref,
                "data": data,
                "resolution": resolution,
            }
        )
        if max_bytes:
            resolved_bytes += len(canonical_json(frozen).encode("utf-8"))
            if resolved_bytes > max_bytes:
                raise SnapshotTooLargeError(resolved_bytes, max_bytes, block.id)
        frozen_blocks.append(frozen)

    return {
        "v": 1,
        "story": _json_safe(
            {
                "id": story.id,
                "title": story.title,
                "case_id": story.case_id,
                "exported_at": now().isoformat(),
                "exported_by": user.username,
            }
        ),
        "blocks": frozen_blocks,
    }
