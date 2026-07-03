"""Tests for the viz router's field-inventory endpoint.

Route handlers in tracevector.api.routers.viz are plain async functions
(same pattern as tests/test_events_router.py), so `list_viz_fields` is
called directly with its collaborators monkeypatched — no FastAPI
TestClient needed.
"""

from __future__ import annotations

import pytest

from tracevector.api.routers import viz


class _FakeStatService:
    def __init__(self, inventory: list[tuple[str, int, int]], total: int) -> None:
        self._inventory = inventory
        self._total = total
        self.calls: list[tuple[str, list[str]]] = []

    def field_inventory(
        self, case_id: str, source_ids: list[str]
    ) -> tuple[list[tuple[str, int, int]], int]:
        self.calls.append((case_id, source_ids))
        return self._inventory, self._total


async def _fake_source_ids(case_id: str, timeline_id: str) -> list[str]:
    return ["s1", "s2"]


@pytest.mark.asyncio
async def test_list_viz_fields_sorts_by_coverage_then_token(monkeypatch):
    svc = _FakeStatService(
        [
            ("artifact", 5, 1000),
            ("display_name", 1, 900),
            ("attr:status_code", 6, 1000),
        ],
        total=1000,
    )
    monkeypatch.setattr(viz, "_get_stat_anomaly_service", lambda: svc)
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
    assert svc.calls == [("c1", ["s1", "s2"])]


@pytest.mark.asyncio
async def test_list_viz_fields_empty_timeline(monkeypatch):
    svc = _FakeStatService([], total=0)
    monkeypatch.setattr(viz, "_get_stat_anomaly_service", lambda: svc)
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
    monkeypatch.setattr(viz, "_resolve_timeline_source_ids", _fake_source_ids)
    monkeypatch.setattr(viz, "_resolve_event_id_filters", _fake_id_filters)
    return svc


@pytest.mark.asyncio
async def test_compare_terms_without_field_is_422(monkeypatch):
    from fastapi import HTTPException

    _patch_compare(monkeypatch)
    body = viz.CompareRequest(
        kind="terms", comparison=viz.ComparisonSpec(mode="baseline")
    )
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
    assert primary.field_filters == {"attr:src_ip": "203.0.113.7"}
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
