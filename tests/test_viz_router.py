"""Tests for the viz router's field-inventory endpoint.

Route handlers in tracesignal.api.routers.viz are plain async functions
(same pattern as tests/test_events_router.py), so `list_viz_fields` is
called directly with its collaborators monkeypatched — no FastAPI
TestClient needed.
"""

from __future__ import annotations

import pytest

from tracesignal.api.routers import viz


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
    # filtering: the constant-valued display_name is still listed.
    assert result == {
        "fields": [
            {"token": "artifact", "distinct": 5, "coverage": 1.0},
            {"token": "attr:status_code", "distinct": 6, "coverage": 1.0},
            {"token": "display_name", "distinct": 1, "coverage": 0.9},
        ]
    }
    assert calls == [("c1", ["s1", "s2"])]


@pytest.mark.asyncio
async def test_list_viz_fields_empty_timeline(monkeypatch):
    _fake_inventory(monkeypatch, [], total=0)
    monkeypatch.setattr(viz, "_get_stat_anomaly_service", lambda: _FakeStatService())
    monkeypatch.setattr(viz, "_resolve_timeline_source_ids", _fake_source_ids)

    result = await viz.list_viz_fields("c1", "t1", case=None)
    assert result == {"fields": []}


# ── POST .../viz/compare ────────────────────────────────────────────────────


class _FakeCompareService:
    """Captures the (primary, comparison) EventQuery pair per compare kind."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object, object]] = []

    def compare_time_histogram(self, primary, comparison, buckets):
        self.calls.append(("time", primary, comparison))
        return {"kind": "time"}

    def compare_field_terms(self, primary, comparison, field, limit):
        self.calls.append(("terms", primary, comparison))
        return {"kind": "terms"}

    def compare_field_numeric(self, primary, comparison, field, bins):
        self.calls.append(("numeric", primary, comparison))
        return {"kind": "numeric"}


async def _fake_id_filters(case_id, source_ids, **_kwargs):
    return None, None, None


def _patch_compare(monkeypatch) -> _FakeCompareService:
    svc = _FakeCompareService()
    monkeypatch.setattr(viz, "_get_query_service", lambda: svc)
    monkeypatch.setattr(viz, "_resolve_timeline_scope", _fake_scope)
    monkeypatch.setattr(viz, "_resolve_event_id_filters", _fake_id_filters)
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

    kind, primary, comparison = svc.calls[0]
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

    kind, _primary, comparison = svc.calls[0]
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


# ── GET .../viz/field-terms cache branch (M24a) ─────────────────────────────


class _FakeTermsService:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def field_terms(self, query, field, limit):
        self.calls.append((query, field, limit))
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
    result = await viz.get_field_terms(
        "c1", "t1", field="artifact", limit=50, case=None, **kwargs
    )
    assert result == {"kind": "live"}
    assert len(svc.calls) == 1


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
