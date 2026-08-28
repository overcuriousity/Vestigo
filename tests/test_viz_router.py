"""Tests for the viz router's field-inventory endpoint.

Route handlers in vestigo.api.routers.viz are plain async functions
(same pattern as tests/test_events_router.py), so `list_viz_fields` is
called directly with its collaborators monkeypatched — no FastAPI
TestClient needed.
"""

from __future__ import annotations

import pytest

from vestigo.api.routers import viz
from vestigo.db._time_fields import TIME_FIELD_PREFIX, TIME_FIELD_SPECS


class _FakeStatService:
    """The endpoint only touches ``.ch`` (handed to the stats cache)."""

    ch = None


def _fake_inventory(monkeypatch, inventory: list[tuple[str, int, int]], total: int) -> list:
    """Stub the M15 stats-cache pair the endpoint reads its inventory from."""
    calls: list[tuple[str, list[str]]] = []

    async def fake_ensure(store, clickhouse, case_id, source_ids):
        calls.append((case_id, source_ids))
        return {}

    monkeypatch.setattr(viz, "ensure_source_field_stats", fake_ensure)
    monkeypatch.setattr(viz, "merged_inventory", lambda stats: (inventory, total))
    return calls


async def _fake_source_ids(case_id: str, timeline_id: str) -> list[str]:
    return ["s1", "s2"]


async def _fake_scope(
    case_id: str, timeline_id: str
) -> tuple[list[str], dict[str, list[str]] | None, dict[str, int] | None]:
    return ["s1", "s2"], None, None


@pytest.mark.asyncio
async def test_list_viz_fields_sorts_by_coverage_then_token(monkeypatch):
    calls = _fake_inventory(
        monkeypatch,
        [
            ("artifact", 5, 1000),
            ("display_name", 1, 900),
            ("attr:status_code", 6, 1000),
        ],
        total=1000,
    )
    monkeypatch.setattr(viz, "_get_stat_anomaly_service", lambda: _FakeStatService())
    monkeypatch.setattr(viz, "_resolve_timeline_source_ids", _fake_source_ids)

    result = await viz.list_viz_fields("c1", "t1", case=None)

    # Coverage descending, token ascending as the tiebreak — and no novelty
    # filtering: the constant-valued display_name is still listed. The virtual
    # `time:` fields follow the real ones (asserted separately below).
    real = [f for f in result["fields"] if not f["token"].startswith(TIME_FIELD_PREFIX)]
    assert real == [
        {"token": "artifact", "distinct": 5, "coverage": 1.0},
        {"token": "attr:status_code", "distinct": 6, "coverage": 1.0},
        {"token": "display_name", "distinct": 1, "coverage": 0.9},
    ]
    assert calls == [("c1", ["s1", "s2"])]


@pytest.mark.asyncio
async def test_list_viz_fields_appends_virtual_time_fields_last(monkeypatch):
    """The analyst's picker must offer everything the agent can chart.

    They are appended rather than merged into the coverage sort: they are
    defined for every dated event, so a coverage-ranked merge would rank them
    above every real field and hand the picker's default pick — documented as
    "the first entry" — to an hour-of-day axis.
    """
    _fake_inventory(monkeypatch, [("artifact", 5, 1000)], total=1000)
    monkeypatch.setattr(viz, "_get_stat_anomaly_service", lambda: _FakeStatService())
    monkeypatch.setattr(viz, "_resolve_timeline_source_ids", _fake_source_ids)

    fields = (await viz.list_viz_fields("c1", "t1", case=None))["fields"]

    assert fields[0]["token"] == "artifact"
    trailing = fields[1:]
    assert [f["token"] for f in trailing] == list(TIME_FIELD_SPECS)
    # A virtual field's stats are null, never fabricated: a time part is
    # undefined for an undated (sentinel-timestamp) event, so claiming
    # coverage 1.0 would assert something this endpoint never measured.
    # `distinct` is the domain size only where the domain is bounded.
    hour = next(f for f in trailing if f["token"] == "time:hour_of_day")
    assert hour == {
        "token": "time:hour_of_day",
        "distinct": 24,
        "coverage": None,
        "label": "Hour of day (UTC)",
    }
    date = next(f for f in trailing if f["token"] == "time:date")
    assert date == {
        "token": "time:date",
        "distinct": None,
        "coverage": None,
        "label": "Date (UTC)",
    }


@pytest.mark.asyncio
async def test_list_viz_fields_empty_timeline(monkeypatch):
    _fake_inventory(monkeypatch, [], total=0)
    monkeypatch.setattr(viz, "_get_stat_anomaly_service", lambda: _FakeStatService())
    monkeypatch.setattr(viz, "_resolve_timeline_source_ids", _fake_source_ids)

    result = await viz.list_viz_fields("c1", "t1", case=None)
    assert result == {"fields": []}


# ── POST .../viz/compare ────────────────────────────────────────────────────


class _FakeCompareService:
    """Captures the (primary, comparison, cache token) per compare kind."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def compare_time_histogram(self, primary, comparison, buckets, baseline_cache_token=None):
        self.calls.append(("time", primary, comparison, baseline_cache_token))
        return {"kind": "time"}

    def compare_field_terms(self, primary, comparison, field, limit, baseline_cache_token=None):
        self.calls.append(("terms", primary, comparison, baseline_cache_token))
        return {"kind": "terms"}

    def compare_field_numeric(self, primary, comparison, field, bins, baseline_cache_token=None):
        self.calls.append(("numeric", primary, comparison, baseline_cache_token))
        return {"kind": "numeric"}


async def _fake_id_filters(case_id, source_ids, **_kwargs):
    return None, None, None


class _FakeStatsRow:
    def __init__(self, source_id: str) -> None:
        import datetime as _dt

        self.source_id = source_id
        self.computed_at = _dt.datetime(2026, 1, 1, tzinfo=_dt.UTC)
        self.events_total = 10


class _FakePgStore:
    """Only what the baseline branch touches: the freshness-fingerprint read."""

    def __init__(self, rows: list[_FakeStatsRow] | None = None) -> None:
        self.rows = rows if rows is not None else []

    async def get_source_field_stats(self, source_ids):
        return [r for r in self.rows if r.source_id in source_ids]


def _patch_compare(monkeypatch, pg_store: _FakePgStore | None = None) -> _FakeCompareService:
    svc = _FakeCompareService()
    monkeypatch.setattr(viz, "_get_query_service", lambda: svc)
    monkeypatch.setattr(viz, "_resolve_timeline_scope", _fake_scope)
    monkeypatch.setattr(viz, "_resolve_event_id_filters", _fake_id_filters)
    monkeypatch.setattr(viz, "get_store", lambda: pg_store or _FakePgStore())
    return svc


@pytest.mark.asyncio
async def test_compare_terms_without_field_is_422(monkeypatch):
    from fastapi import HTTPException

    _patch_compare(monkeypatch)
    body = viz.CompareRequest(kind="terms", comparison=viz.ComparisonSpec(mode="baseline"))
    with pytest.raises(HTTPException) as exc:
        await viz.compare_layers("c1", "t1", body, case=None)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_compare_custom_without_filters_is_422(monkeypatch):
    from fastapi import HTTPException

    _patch_compare(monkeypatch)
    body = viz.CompareRequest(kind="time", comparison=viz.ComparisonSpec(mode="custom"))
    with pytest.raises(HTTPException) as exc:
        await viz.compare_layers("c1", "t1", body, case=None)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_compare_baseline_clears_filters_keeps_scope_and_window(monkeypatch):
    from datetime import UTC, datetime

    svc = _patch_compare(monkeypatch)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)
    body = viz.CompareRequest(
        kind="time",
        primary=viz.CompareFilters(
            q="dos",
            artifacts="apache,nginx",
            filters='{"attr:src_ip": "203.0.113.7"}',
            start=start,
            end=end,
        ),
        comparison=viz.ComparisonSpec(mode="baseline"),
    )
    await viz.compare_layers("c1", "t1", body, case=None)

    kind, primary, comparison, _token = svc.calls[0]
    assert kind == "time"
    assert primary.q == "dos"
    assert primary.field_filters == {"attr:src_ip": ["203.0.113.7"]}
    # Baseline = "everything in this timeline and window": filters dropped,
    # timeline scope and time window kept.
    assert comparison.q is None
    assert comparison.artifacts is None
    assert comparison.field_filters == {}
    assert comparison.source_ids == primary.source_ids == ["s1", "s2"]
    assert comparison.start == start
    assert comparison.end == end


@pytest.mark.asyncio
async def test_compare_custom_inherits_primary_time_window(monkeypatch):
    from datetime import UTC, datetime

    svc = _patch_compare(monkeypatch)
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, tzinfo=UTC)
    body = viz.CompareRequest(
        kind="terms",
        field="attr:method",
        primary=viz.CompareFilters(q="dos", start=start, end=end),
        comparison=viz.ComparisonSpec(
            mode="custom",
            filters=viz.CompareFilters(
                q="error",
                start=datetime(2020, 1, 1, tzinfo=UTC),  # must be overridden
            ),
        ),
    )
    await viz.compare_layers("c1", "t1", body, case=None)

    kind, _primary, comparison, _token = svc.calls[0]
    assert kind == "terms"
    assert comparison.q == "error"
    # Comparability invariant: custom layer shares the primary's window.
    assert comparison.start == start
    assert comparison.end == end


# ── GET .../viz/time-punchcard / field-pivot / field-scatter ────────────────


class _FakeAggService:
    """Captures calls to the new aggregation methods."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def time_punchcard(self, query):
        self.calls.append(("punchcard", query))
        return {"kind": "punchcard"}

    def field_pivot(self, query, field_x, field_y, limit_x, limit_y):
        self.calls.append(("pivot", query, field_x, field_y, limit_x, limit_y))
        return {"kind": "pivot"}

    def field_scatter(self, query, field_x, field_y, limit):
        self.calls.append(("scatter", query, field_x, field_y, limit))
        return {"kind": "scatter"}

    def field_numeric_stats(self, query, field, bins, points, points_limit):
        self.calls.append(("numeric", query, field, bins, points, points_limit))
        return {"kind": "numeric"}

    def field_correlation(self, query, fields):
        self.calls.append(("corr", query, tuple(fields)))
        return {"kind": "corr"}

    def field_numeric_grouped(self, query, field, group_field, groups, bins, points, points_limit):
        self.calls.append(
            ("grouped", query, field, group_field, groups, bins, points, points_limit)
        )
        return {"kind": "numeric_grouped"}


def _patch_agg(monkeypatch) -> _FakeAggService:
    svc = _FakeAggService()
    monkeypatch.setattr(viz, "_get_query_service", lambda: svc)
    monkeypatch.setattr(viz, "_resolve_timeline_scope", _fake_scope)
    monkeypatch.setattr(viz, "_resolve_event_id_filters", _fake_id_filters)
    return svc


# The GET handlers declare every shared filter param with a FastAPI Query
# default — calling the plain function directly would otherwise pass the
# Query marker objects as values.
_FILTER_KWARGS = {
    "q": None,
    "q_regex": False,
    "artifact": None,
    "artifacts": None,
    "source_id": None,
    "tag": None,
    "exclude_tag": None,
    "tags_include": None,
    "tags_exclude": None,
    "ids": None,
    "start": None,
    "end": None,
    "filters": None,
    "exclusions": None,
    "filter_modes": None,
    "exclusion_modes": None,
    "annotated": None,
    "annotation_tag_value": None,
    "run_id": None,
    "collapse_routine": False,
}


@pytest.mark.asyncio
async def test_time_punchcard_resolves_scope_and_calls_service(monkeypatch):
    svc = _patch_agg(monkeypatch)
    result = await viz.get_time_punchcard("c1", "t1", case=None, **_FILTER_KWARGS)
    assert result == {"kind": "punchcard"}
    kind, query = svc.calls[0]
    assert kind == "punchcard"
    assert query.case_id == "c1"
    assert query.source_ids == ["s1", "s2"]


@pytest.mark.asyncio
async def test_field_pivot_passes_fields_and_limits(monkeypatch):
    svc = _patch_agg(monkeypatch)
    result = await viz.get_field_pivot(
        "c1",
        "t1",
        field_x="attr:username",
        field_y="attr:workstation",
        limit_x=7,
        limit_y=9,
        case=None,
        **_FILTER_KWARGS,
    )
    assert result == {"kind": "pivot"}
    kind, query, field_x, field_y, limit_x, limit_y = svc.calls[0]
    assert (field_x, field_y, limit_x, limit_y) == ("attr:username", "attr:workstation", 7, 9)
    assert query.source_ids == ["s1", "s2"]


@pytest.mark.asyncio
async def test_field_pivot_same_field_is_422(monkeypatch):
    from fastapi import HTTPException

    _patch_agg(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await viz.get_field_pivot(
            "c1",
            "t1",
            field_x="artifact",
            field_y="artifact",
            limit_x=10,
            limit_y=10,
            case=None,
            **_FILTER_KWARGS,
        )
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_field_scatter_passes_fields_and_limit(monkeypatch):
    svc = _patch_agg(monkeypatch)
    result = await viz.get_field_scatter(
        "c1",
        "t1",
        field_x="attr:bytes",
        field_y="attr:latency",
        limit=1000,
        case=None,
        **_FILTER_KWARGS,
    )
    assert result == {"kind": "scatter"}
    kind, query, field_x, field_y, limit = svc.calls[0]
    assert (field_x, field_y, limit) == ("attr:bytes", "attr:latency", 1000)


@pytest.mark.asyncio
async def test_field_scatter_same_field_is_422(monkeypatch):
    from fastapi import HTTPException

    _patch_agg(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await viz.get_field_scatter(
            "c1",
            "t1",
            field_x="attr:bytes",
            field_y="attr:bytes",
            limit=1000,
            case=None,
            **_FILTER_KWARGS,
        )
    assert exc.value.status_code == 422


# ── GET .../viz/field-numeric-stats / field-correlation / field-numeric-grouped ──


@pytest.mark.asyncio
async def test_field_numeric_stats_omitted_bins_reach_the_service_as_none(monkeypatch):
    """`bins` unset is the request for automatic (Freedman–Diaconis) binning.

    Substituting a default here would make "auto" unreachable over HTTP.
    """
    svc = _patch_agg(monkeypatch)
    result = await viz.get_field_numeric_stats(
        "c1",
        "t1",
        field="attr:bytes",
        bins=None,
        points=True,
        points_limit=250,
        case=None,
        **_FILTER_KWARGS,
    )
    assert result == {"kind": "numeric"}
    _, query, field, bins, points, points_limit = svc.calls[0]
    assert (field, bins, points, points_limit) == ("attr:bytes", None, True, 250)
    assert query.source_ids == ["s1", "s2"]


@pytest.mark.asyncio
async def test_field_correlation_passes_distinct_fields(monkeypatch):
    svc = _patch_agg(monkeypatch)
    result = await viz.get_field_correlation(
        "c1",
        "t1",
        fields=["attr:bytes", "attr:latency", "attr:retries"],
        case=None,
        **_FILTER_KWARGS,
    )
    assert result == {"kind": "corr"}
    _, query, fields = svc.calls[0]
    assert fields == ("attr:bytes", "attr:latency", "attr:retries")
    assert query.source_ids == ["s1", "s2"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(["attr:bytes", "attr:bytes"], id="duplicate"),
        pytest.param(["attr:bytes"], id="too-few"),
        pytest.param([f"attr:f{i}" for i in range(9)], id="too-many"),
    ],
)
async def test_field_correlation_rejects_bad_field_lists(monkeypatch, fields):
    from fastapi import HTTPException

    _patch_agg(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await viz.get_field_correlation("c1", "t1", fields=fields, case=None, **_FILTER_KWARGS)
    assert exc.value.status_code == 422


@pytest.mark.asyncio
async def test_field_numeric_grouped_passes_every_knob(monkeypatch):
    svc = _patch_agg(monkeypatch)
    result = await viz.get_field_numeric_grouped(
        "c1",
        "t1",
        field="attr:bytes",
        group_field="attr:user",
        groups=4,
        bins=25,
        points=True,
        points_limit=500,
        case=None,
        **_FILTER_KWARGS,
    )
    assert result == {"kind": "numeric_grouped"}
    _, query, field, group_field, groups, bins, points, points_limit = svc.calls[0]
    assert (field, group_field, groups, bins, points, points_limit) == (
        "attr:bytes",
        "attr:user",
        4,
        25,
        True,
        500,
    )
    assert query.source_ids == ["s1", "s2"]


@pytest.mark.asyncio
async def test_field_numeric_grouped_same_field_is_422(monkeypatch):
    """Grouping a field by itself yields one box per value — not a distribution."""
    from fastapi import HTTPException

    _patch_agg(monkeypatch)
    with pytest.raises(HTTPException) as exc:
        await viz.get_field_numeric_grouped(
            "c1",
            "t1",
            field="attr:bytes",
            group_field="attr:bytes",
            groups=8,
            bins=30,
            points=False,
            points_limit=1000,
            case=None,
            **_FILTER_KWARGS,
        )
    assert exc.value.status_code == 422


# ── GET .../viz/field-terms cache branch (M24a) ─────────────────────────────


class _FakeTermsService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def field_terms(self, query, field, limit, *, totals=True):
        self.calls.append((query, field, limit, totals))
        return {"kind": "live"}


def _patch_terms(monkeypatch, cached_result) -> _FakeTermsService:
    svc = _FakeTermsService()
    monkeypatch.setattr(viz, "_get_query_service", lambda: svc)
    monkeypatch.setattr(viz, "_get_stat_anomaly_service", lambda: _FakeStatService())
    monkeypatch.setattr(viz, "get_store", lambda: None)
    monkeypatch.setattr(viz, "_resolve_timeline_scope", _fake_scope)
    monkeypatch.setattr(viz, "_resolve_event_id_filters", _fake_id_filters)

    async def fake_ensure(store, clickhouse, case_id, source_ids):
        return {}

    monkeypatch.setattr(viz, "ensure_source_field_stats", fake_ensure)
    monkeypatch.setattr(viz, "merged_field_terms", lambda stats, field, limit: cached_result)
    return svc


@pytest.mark.asyncio
async def test_field_terms_unfiltered_served_from_cache(monkeypatch):
    cached = {"field": "artifact", "total": 5, "distinct": 2, "values": [], "other_count": 0}
    svc = _patch_terms(monkeypatch, cached)
    result = await viz.get_field_terms(
        "c1", "t1", field="artifact", limit=50, case=None, **_FILTER_KWARGS
    )
    assert result == {**cached, "cached": True}
    assert svc.calls == []  # no ClickHouse scan


@pytest.mark.asyncio
async def test_field_terms_cache_gap_falls_back_live(monkeypatch):
    svc = _patch_terms(monkeypatch, None)
    result = await viz.get_field_terms(
        "c1", "t1", field="artifact", limit=50, case=None, **_FILTER_KWARGS
    )
    assert result == {"kind": "live"}
    assert len(svc.calls) == 1


@pytest.mark.asyncio
async def test_field_terms_any_filter_forces_live_path(monkeypatch):
    cached = {"field": "artifact", "total": 5, "distinct": 2, "values": [], "other_count": 0}
    svc = _patch_terms(monkeypatch, cached)
    kwargs = {**_FILTER_KWARGS, "q": "dos"}
    result = await viz.get_field_terms("c1", "t1", field="artifact", limit=50, case=None, **kwargs)
    assert result == {"kind": "live"}
    assert len(svc.calls) == 1


@pytest.mark.asyncio
async def test_field_terms_totals_flag_reaches_the_service(monkeypatch):
    """`totals=false` is the caller saying it reads only the values, so the
    second whole-corpus grouping is skipped (PR #306 review). Defaults to
    True: every chart that renders an "Other" slice needs the tail."""
    svc = _patch_terms(monkeypatch, None)
    # Passed explicitly: calling the endpoint function directly bypasses
    # FastAPI's parameter resolution, so an omitted arg is the `Query` object
    # rather than its default.
    await viz.get_field_terms(
        "c1", "t1", field="artifact", limit=50, totals=True, case=None, **_FILTER_KWARGS
    )
    assert svc.calls[-1][3] is True
    await viz.get_field_terms(
        "c1", "t1", field="artifact", limit=50, totals=False, case=None, **_FILTER_KWARGS
    )
    assert svc.calls[-1][3] is False


@pytest.mark.asyncio
async def test_field_terms_mapped_token_forces_live_path(monkeypatch):
    cached = {"field": "proto_c", "total": 5, "distinct": 2, "values": [], "other_count": 0}
    svc = _patch_terms(monkeypatch, cached)

    async def scope_with_mappings(case_id, timeline_id):
        return ["s1"], {"proto_c": ["proto", "protocol"]}, None

    monkeypatch.setattr(viz, "_resolve_timeline_scope", scope_with_mappings)
    result = await viz.get_field_terms(
        "c1", "t1", field="proto_c", limit=50, case=None, **_FILTER_KWARGS
    )
    assert result == {"kind": "live"}
    assert len(svc.calls) == 1


# ── POST .../viz/compare baseline-cache token (M24c) ─────────────────────────


@pytest.mark.asyncio
async def test_compare_baseline_passes_freshness_token(monkeypatch):
    svc = _patch_compare(monkeypatch, _FakePgStore([_FakeStatsRow("s1"), _FakeStatsRow("s2")]))
    body = viz.CompareRequest(kind="time", comparison=viz.ComparisonSpec(mode="baseline"))
    await viz.compare_layers("c1", "t1", body, case=None)
    token = svc.calls[0][3]
    assert token is not None
    assert token[0] == "c1"
    assert [sid for sid, _, _ in token[1]] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_compare_baseline_missing_stats_row_disables_cache(monkeypatch):
    svc = _patch_compare(monkeypatch, _FakePgStore([_FakeStatsRow("s1")]))  # s2 missing
    body = viz.CompareRequest(kind="time", comparison=viz.ComparisonSpec(mode="baseline"))
    await viz.compare_layers("c1", "t1", body, case=None)
    assert svc.calls[0][3] is None


@pytest.mark.asyncio
async def test_compare_custom_mode_never_gets_token(monkeypatch):
    svc = _patch_compare(monkeypatch, _FakePgStore([_FakeStatsRow("s1"), _FakeStatsRow("s2")]))
    body = viz.CompareRequest(
        kind="time",
        comparison=viz.ComparisonSpec(mode="custom", filters=viz.CompareFilters(q="x")),
    )
    await viz.compare_layers("c1", "t1", body, case=None)
    assert svc.calls[0][3] is None


# ---------------------------------------------------------------------------
# Routine collapse (#147 follow-up) — every filter-driven viz endpoint must
# resolve the routine scope, same as list_events/get_histogram/export/bulk.
# ---------------------------------------------------------------------------


class _CaptureTermsService:
    """Captures the EventQuery handed to field_terms."""

    def __init__(self) -> None:
        self.last_query = None

    def field_terms(self, query, field_token, limit=50, *, totals=True):
        self.last_query = query
        return {"values": [], "other": 0, "total": 0}


def _patch_routine_scope(monkeypatch):
    """Stub _resolve_routine_collapse, recording the flag it was handed.

    The resolver itself is covered by tests/test_events_router.py — what this
    file guards is the *wiring*: the flag must travel from the route param
    into the resolver, and the resolved scope into the EventQuery. That
    wiring being absent is exactly the silent-drop bug (#147's sibling).
    """
    from vestigo.api.routers.events import RoutineCollapseScope

    calls: list[bool] = []

    async def fake_resolve(case_id, timeline_id, source_ids, collapse_routine):
        calls.append(collapse_routine)
        if collapse_routine:
            return RoutineCollapseScope(["m1"], [4736])
        return RoutineCollapseScope(None, None)

    monkeypatch.setattr(viz, "_resolve_routine_collapse", fake_resolve, raising=False)
    return calls


@pytest.mark.asyncio
async def test_field_terms_honors_routine_collapse(monkeypatch):
    """collapse_routine=True must reach the EventQuery — both scope halves."""
    calls = _patch_routine_scope(monkeypatch)
    svc = _CaptureTermsService()
    monkeypatch.setattr(viz, "_get_query_service", lambda: svc)
    monkeypatch.setattr(viz, "_resolve_timeline_scope", _fake_scope)
    monkeypatch.setattr(viz, "_resolve_event_id_filters", _fake_id_filters)

    async def no_cache(*args, **kwargs):
        raise AssertionError("collapsed query must not be served from the unfiltered cache")

    monkeypatch.setattr(viz, "ensure_source_field_stats", no_cache)

    await viz.get_field_terms(
        "c1",
        "t1",
        field="artifact",
        case=None,
        **{**_FILTER_KWARGS, "collapse_routine": True},
    )

    assert calls == [True]
    assert svc.last_query.exclude_routine_disposition_ids == ["m1"]
    assert svc.last_query.exclude_template_hashes == [4736]


@pytest.mark.asyncio
async def test_field_terms_without_flag_keeps_full_scope(monkeypatch):
    calls = _patch_routine_scope(monkeypatch)
    svc = _CaptureTermsService()
    monkeypatch.setattr(viz, "_get_query_service", lambda: svc)
    monkeypatch.setattr(viz, "_resolve_timeline_scope", _fake_scope)
    monkeypatch.setattr(viz, "_resolve_event_id_filters", _fake_id_filters)
    monkeypatch.setattr(viz, "_get_stat_anomaly_service", lambda: _FakeStatService())

    async def fake_ensure(store, clickhouse, case_id, source_ids):
        return {}

    monkeypatch.setattr(viz, "ensure_source_field_stats", fake_ensure)
    # Force the live path (cache gap) so the EventQuery is observable.
    monkeypatch.setattr(viz, "merged_field_terms", lambda stats, field, limit: None)

    await viz.get_field_terms("c1", "t1", field="artifact", limit=50, case=None, **_FILTER_KWARGS)

    assert calls == [False]
    assert svc.last_query.exclude_routine_disposition_ids is None
    assert svc.last_query.exclude_template_hashes is None


@pytest.mark.asyncio
async def test_compare_layers_honor_routine_collapse_per_layer(monkeypatch):
    """Each compare layer carries its own flag; the baseline layer stays the
    deliberately-unfiltered whole (all filters dropped, collapse included)."""
    _patch_routine_scope(monkeypatch)
    svc = _patch_compare(monkeypatch)

    body = viz.CompareRequest(
        kind="time",
        primary=viz.CompareFilters(collapse_routine=True),
        comparison=viz.ComparisonSpec(mode="baseline"),
    )
    await viz.compare_layers("c1", "t1", body, case=None)

    _, primary, comparison, _ = svc.calls[0]
    assert primary.exclude_template_hashes == [4736]
    assert primary.exclude_routine_disposition_ids == ["m1"]
    assert comparison.exclude_template_hashes is None
    assert comparison.exclude_routine_disposition_ids is None


@pytest.mark.asyncio
async def test_compare_custom_layer_honors_its_own_flag(monkeypatch):
    _patch_routine_scope(monkeypatch)
    svc = _patch_compare(monkeypatch)

    body = viz.CompareRequest(
        kind="time",
        primary=viz.CompareFilters(),
        comparison=viz.ComparisonSpec(
            mode="custom", filters=viz.CompareFilters(collapse_routine=True)
        ),
    )
    await viz.compare_layers("c1", "t1", body, case=None)

    _, primary, comparison, _ = svc.calls[0]
    assert primary.exclude_template_hashes is None
    assert comparison.exclude_template_hashes == [4736]
    assert comparison.exclude_routine_disposition_ids == ["m1"]
