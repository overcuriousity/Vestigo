"""Tests for events router helpers that don't require a full HTTP client.

Route handlers in tracesignal.api.routers.events are plain async functions,
so the pure logic (annotation-filter resolution, live-finding union,
export-annotation indexing) is tested by calling them directly rather than
spinning up a FastAPI TestClient.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import HTTPException

from tests.conftest import _fake_user
from tracesignal.api import deps
from tracesignal.api.routers import events
from tracesignal.db.postgres import Case, PostgresStore


@pytest_asyncio.fixture()
async def store(tmp_path):
    """In-memory SQLite store — same pattern as tests/test_annotations.py."""
    db_path = tmp_path / "test_events_router.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    await s.init_schema()
    yield s
    await s.engine.dispose()


@pytest_asyncio.fixture()
async def patched_store(store, monkeypatch):
    """Point deps.get_store() (shared by every router) at the in-memory test store."""
    monkeypatch.setattr(deps, "_store", store)
    return store


async def _make_run(store, case_id: str, timeline_id: str, event_ids: list[str]) -> str:
    """Seed a DetectorRun row with the given finding event_ids and return its run_id."""
    run = await store.create_detector_run(
        case_id,
        timeline_id,
        "value_novelty",
        params={},
        result={"results": [{"event_id": eid} for eid in event_ids]},
    )
    return run.id


# ---------------------------------------------------------------------------
# _resolve_annotated_event_ids
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_annotated_returns_none_when_no_filter(patched_store):
    result = await events._resolve_annotated_event_ids("c1", ["s1"], None, None)
    assert result is None


@pytest.mark.asyncio
async def test_resolve_annotated_anomaly_matches_persisted_only(patched_store):
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="persisted-evt",
        annotation_id="ann1",
        annotation_type="anomaly",
        content="tagged",
        origin="system",
    )
    result = await events._resolve_annotated_event_ids("c1", ["s1"], "anomaly", None)
    assert result == ["persisted-evt"]


@pytest.mark.asyncio
async def test_resolve_annotated_anomaly_unions_run_event_ids(patched_store):
    """Live (not-yet-tagged) findings never reach the annotations table —
    the frontend references them by a persisted run_id, and the anomaly
    branch must union the run's finding event_ids in rather than requiring
    annotation-persistence first."""
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="persisted-evt",
        annotation_id="ann2",
        annotation_type="anomaly",
        content="tagged",
        origin="system",
    )
    run_id = await _make_run(patched_store, "c1", "t1", ["live-evt-1", "live-evt-2"])
    result = await events._resolve_annotated_event_ids("c1", ["s1"], "anomaly", None, run_id=run_id)
    assert set(result) == {"persisted-evt", "live-evt-1", "live-evt-2"}


@pytest.mark.asyncio
async def test_resolve_annotated_run_id_ignored_without_anomaly_type(
    patched_store,
):
    """run_id should only ever apply to the 'anomaly' branch — passing it
    while filtering on 'tag' alone must not leak it into the result."""
    run_id = await _make_run(patched_store, "c1", "t1", ["live-evt-1"])
    result = await events._resolve_annotated_event_ids("c1", ["s1"], "tag", None, run_id=run_id)
    assert result == []


@pytest.mark.asyncio
async def test_resolve_annotated_dedupes_overlap_between_persisted_and_run(
    patched_store,
):
    """The same event flagged both ways (e.g. persisted after being a live
    finding) must not appear twice in the resolved list."""
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="same-evt",
        annotation_id="ann3",
        annotation_type="anomaly",
        content="tagged",
        origin="system",
    )
    run_id = await _make_run(patched_store, "c1", "t1", ["same-evt"])
    result = await events._resolve_annotated_event_ids("c1", ["s1"], "anomaly", None, run_id=run_id)
    assert result == ["same-evt"]


@pytest.mark.asyncio
async def test_resolve_annotated_unknown_run_id_raises_404(patched_store):
    with pytest.raises(HTTPException) as exc_info:
        await events._resolve_annotated_event_ids(
            "c1", ["s1"], "anomaly", None, run_id="no-such-run"
        )
    assert exc_info.value.status_code == 404


# ---------------------------------------------------------------------------
# _resolve_event_id_filters (C17 — shared by list_events, bulk_annotate_by_filter,
# get_histogram, export_events)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_event_id_filters_no_filters_means_no_restriction(patched_store):
    event_ids, tags_include, tags_exclude = await events._resolve_event_id_filters(
        "c1",
        ["s1"],
        annotated=None,
        annotation_tag_value=None,
        run_id=None,
        tags_include=None,
        tags_exclude=None,
        ids=None,
    )
    assert event_ids is None
    assert tags_include is None
    assert tags_exclude is None


@pytest.mark.asyncio
async def test_resolve_event_id_filters_intersects_annotated_and_ids(patched_store):
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="flagged-evt",
        annotation_id="ann1",
        annotation_type="anomaly",
        content="tagged",
        origin="system",
    )
    event_ids, tags_include, tags_exclude = await events._resolve_event_id_filters(
        "c1",
        ["s1"],
        annotated="anomaly",
        annotation_tag_value=None,
        run_id=None,
        tags_include=None,
        tags_exclude=None,
        ids="flagged-evt,other-evt",
    )
    assert event_ids == ["flagged-evt"]
    assert tags_include is None
    assert tags_exclude is None


@pytest.mark.asyncio
async def test_resolve_event_id_filters_returns_tags_exclude_filter_independently(
    patched_store,
):
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="tagged-evt",
        annotation_id="ann1",
        annotation_type="tag",
        origin="user",
        content="noisy",
    )

    event_ids, tags_include, tags_exclude = await events._resolve_event_id_filters(
        "c1",
        ["s1"],
        annotated=None,
        annotation_tag_value=None,
        run_id=None,
        tags_include=None,
        tags_exclude="noisy",
        ids=None,
    )
    assert event_ids is None
    assert tags_include is None
    assert tags_exclude.tag_values == ["noisy"]
    assert tags_exclude.postgres_event_ids == ["tagged-evt"]


@pytest.mark.asyncio
async def test_resolve_event_id_filters_returns_tags_include_filter_separately(patched_store):
    """tags_include must not be folded into event_ids — it's an OR-between-
    systems predicate applied via EventQuery.tags_include, not an ID
    restriction ANDed via _intersect_optional."""
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="tagged-evt",
        annotation_id="ann1",
        annotation_type="tag",
        origin="user",
        content="urgent",
    )

    event_ids, tags_include, tags_exclude = await events._resolve_event_id_filters(
        "c1",
        ["s1"],
        annotated=None,
        annotation_tag_value=None,
        run_id=None,
        tags_include="urgent",
        tags_exclude=None,
        ids=None,
    )
    assert event_ids is None
    assert tags_include.tag_values == ["urgent"]
    assert tags_include.postgres_event_ids == ["tagged-evt"]
    assert tags_exclude is None


# ---------------------------------------------------------------------------
# _resolve_tags_filter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resolve_tags_filter_returns_none_for_no_values(patched_store):
    assert await events._resolve_tags_filter("c1", ["s1"], None) is None
    assert await events._resolve_tags_filter("c1", ["s1"], []) is None


@pytest.mark.asyncio
async def test_resolve_tags_filter_resolves_only_postgres_side(patched_store):
    """Only the Postgres (user annotation) half is resolved here — the
    parser-tag half is matched natively in ClickHouse via EventQuery.tags_include,
    not fetched into Python (that round trip is exactly what C13 removed)."""
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="ann-evt",
        annotation_id="ann1",
        annotation_type="tag",
        origin="user",
        content="suspicious",
    )
    result = await events._resolve_tags_filter("c1", ["s1"], ["suspicious"])
    assert result.tag_values == ["suspicious"]
    assert result.postgres_event_ids == ["ann-evt"]


# ---------------------------------------------------------------------------
# bulk_annotate_by_filter
# ---------------------------------------------------------------------------


class _FakeQueryService:
    """Captures the EventQuery passed by bulk_annotate_by_filter."""

    def __init__(self, refs: list[tuple[str, str]]) -> None:
        self.refs = refs
        self.last_query = None

    def query_event_refs(self, query, cap: int = 100_000):
        self.last_query = query
        return self.refs


@pytest.mark.asyncio
async def test_bulk_annotate_by_filter_honors_annotated_restriction(patched_store, monkeypatch):
    """The 'apply to all matching filter' bulk action must not silently
    ignore an active `annotated` (e.g. anomaly) filter — regression test for
    a bug where BulkAnnotateByFilterRequest had no `annotated` field at all,
    so bulk-tagging while filtered to flagged events wrote to every event
    matching the other filters instead of just the flagged subset."""
    await patched_store.create_case("c1", "Case One")
    await patched_store.create_source("c1", "s1", "source one", file_hash="h1", size_bytes=10)
    await patched_store.create_timeline("c1", "t1", "Timeline One", source_ids=["s1"])
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="flagged-evt",
        annotation_id="ann1",
        annotation_type="anomaly",
        content="tagged",
        origin="system",
    )

    fake_service = _FakeQueryService(refs=[("flagged-evt", "s1")])
    monkeypatch.setattr(events, "_get_query_service", lambda: fake_service)

    body = events.BulkAnnotateByFilterRequest(
        annotation_type="tag",
        content="reviewed",
        annotated="anomaly",
    )
    result = await events.bulk_annotate_by_filter(
        "c1", "t1", body, case=Case(id="c1"), user=_fake_user()
    )

    assert result == {"tagged": 1}
    assert fake_service.last_query.event_ids == ["flagged-evt"]


# ---------------------------------------------------------------------------
# _index_annotations_by_event (export enrichment)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_index_annotations_by_event_groups_by_event_id(patched_store):
    await patched_store.create_annotation(
        case_id="c2",
        source_id="s2",
        event_id="e1",
        annotation_id="a1",
        annotation_type="tag",
        content="foo",
        origin="user",
    )
    await patched_store.create_annotation(
        case_id="c2",
        source_id="s2",
        event_id="e1",
        annotation_id="a2",
        annotation_type="comment",
        content="bar",
        origin="user",
    )
    await patched_store.create_annotation(
        case_id="c2",
        source_id="s2",
        event_id="e2",
        annotation_id="a3",
        annotation_type="tag",
        content="baz",
        origin="user",
    )
    all_annotations = await patched_store.list_source_annotations("c2", ["s2"])
    indexed = events._index_annotations_by_event(all_annotations)
    assert {a.id for a in indexed["e1"]} == {"a1", "a2"}
    assert {a.id for a in indexed["e2"]} == {"a3"}
    assert "e3" not in indexed


# ---------------------------------------------------------------------------
# _parse_cursor (keyset pagination query param)
# ---------------------------------------------------------------------------


def test_parse_cursor_returns_none_for_empty_value():
    assert events._parse_cursor(None, param_name="after") is None
    assert events._parse_cursor("", param_name="after") is None


def test_parse_cursor_splits_timestamp_and_event_id():
    ts, event_id = events._parse_cursor("2026-06-25T07:30:01+00:00,evt-1", param_name="after")
    assert ts == datetime.fromisoformat("2026-06-25T07:30:01+00:00")
    assert event_id == "evt-1"


def test_parse_cursor_rejects_malformed_value():
    with pytest.raises(HTTPException) as exc_info:
        events._parse_cursor("not-a-cursor", param_name="before")
    assert exc_info.value.status_code == 400


def test_parse_cursor_accepts_empty_event_id_as_synthetic_lower_bound():
    """A jump-to-time target may only have a timestamp (e.g. a Frequency
    finding's window_start with no representative event) — the trailing
    comma with nothing after it is a valid synthetic cursor, not malformed.
    """
    ts, event_id = events._parse_cursor("2026-06-25T07:30:01+00:00,", param_name="before")
    assert ts == datetime.fromisoformat("2026-06-25T07:30:01+00:00")
    assert event_id == ""


def test_parse_cursor_rejects_bad_timestamp():
    with pytest.raises(HTTPException) as exc_info:
        events._parse_cursor("not-a-timestamp,evt-1", param_name="after")
    assert exc_info.value.status_code == 400


# ---------------------------------------------------------------------------
# _validate_regex / _run_regex_guarded (q_regex search)
# ---------------------------------------------------------------------------


def test_validate_regex_accepts_valid_pattern():
    events._validate_regex(r"^Login (failed|succeeded)$", True)


def test_validate_regex_noop_when_flag_off_or_no_query():
    events._validate_regex("([", False)  # invalid pattern, but literal mode
    events._validate_regex(None, True)


def test_validate_regex_rejects_invalid_pattern_with_400():
    with pytest.raises(HTTPException) as exc_info:
        events._validate_regex("([", True)
    assert exc_info.value.status_code == 400
    assert "invalid regular expression" in exc_info.value.detail


def test_parse_modes_object_accepts_valid_modes():
    assert events._parse_modes_object(None) == {}
    assert events._parse_modes_object('{"src_ip": "wildcard", "msg": "regex", "a": "exact"}') == {
        "src_ip": "wildcard",
        "msg": "regex",
        "a": "exact",
    }


def test_parse_modes_object_rejects_unknown_mode_with_400():
    with pytest.raises(HTTPException) as exc_info:
        events._parse_modes_object('{"src_ip": "glob"}')
    assert exc_info.value.status_code == 400
    assert "invalid match mode" in exc_info.value.detail


def test_validate_field_regexes_rejects_invalid_pattern_with_400():
    with pytest.raises(HTTPException) as exc_info:
        events._validate_field_regexes({"msg": "(["}, {"msg": "regex"})
    assert exc_info.value.status_code == 400
    assert "invalid regular expression" in exc_info.value.detail
    # Exclusion-shaped (list) values are checked per value.
    with pytest.raises(HTTPException):
        events._validate_field_regexes({"msg": ["ok", "(["]}, {"msg": "regex"})


def test_validate_field_regexes_ignores_non_regex_modes():
    # "([" is an invalid regex but valid literal/wildcard — must not raise.
    events._validate_field_regexes({"msg": "(["}, {"msg": "wildcard"})
    events._validate_field_regexes({"msg": "(["}, {})


def test_uses_regex_detects_field_modes():
    assert events._uses_regex(False) is False
    assert events._uses_regex(True) is True
    assert events._uses_regex(False, {"a": "wildcard"}) is False
    assert events._uses_regex(False, {"a": "wildcard"}, {"b": "regex"}) is True


@pytest.mark.asyncio
async def test_run_regex_guarded_maps_re2_failure_to_400():
    from clickhouse_connect.driver.exceptions import DatabaseError

    def scan():
        raise DatabaseError("Code: 427. DB::Exception: OK, but cannot compile re2: (?<=x)")

    with pytest.raises(HTTPException) as exc_info:
        await events._run_regex_guarded(True, scan)
    assert exc_info.value.status_code == 400


@pytest.mark.asyncio
async def test_run_regex_guarded_reraises_non_regex_errors():
    from clickhouse_connect.driver.exceptions import DatabaseError

    def scan():
        raise DatabaseError("Code: 241. DB::Exception: Memory limit exceeded")

    with pytest.raises(DatabaseError):
        await events._run_regex_guarded(True, scan)


@pytest.mark.asyncio
async def test_run_regex_guarded_reraises_when_flag_off():
    from clickhouse_connect.driver.exceptions import DatabaseError

    def scan():
        raise DatabaseError("Code: 427. DB::Exception: cannot compile re2")

    with pytest.raises(DatabaseError):
        await events._run_regex_guarded(False, scan)


# ---------------------------------------------------------------------------
# _get_field_encoder (embedding-assisted field pairing)
# ---------------------------------------------------------------------------


def test_get_field_encoder_does_not_eagerly_load_in_remote_mode(monkeypatch):
    """In remote-embedding mode, load() raises RuntimeError (it's a
    local-model-only operation) — calling it unconditionally here silently
    disables the field-pairing recommender for every remote deployment,
    since the bare except swallows the RuntimeError and returns None."""
    monkeypatch.setattr(events, "_embedding_model", None)

    class ExplodingLoadModel:
        def __init__(self) -> None:
            self.is_remote = True

        def load(self):
            raise RuntimeError("load() is not available when using a remote embedding endpoint")

        def encode(self, texts):
            return [[0.0] for _ in texts]

    import tracesignal.models.embeddings as embeddings_module

    monkeypatch.setattr(embeddings_module, "EmbeddingModel", ExplodingLoadModel)

    encode = events._get_field_encoder()
    assert encode is not None
    assert encode(["x"]) == [[0.0]]


# ---------------------------------------------------------------------------
# _run_stat_detector (C16 — shared by list_anomalies and tag_anomalies)
# ---------------------------------------------------------------------------


class _FakeStatAnomalyService:
    """Captures the kwargs passed to each detector method."""

    ch = None  # accessed by the router's field-stats cache resolution

    def __init__(self, midpoint=None, ts_range=None):
        self._midpoint = midpoint
        # (min, max) timeline range used by the legacy split shim. Defaults to
        # a window whose midpoint equals `midpoint` when one is supplied.
        if ts_range is None and midpoint is not None:
            ts_range = (midpoint - timedelta(hours=12), midpoint + timedelta(hours=12))
        self._ts_range = ts_range or (
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 31, tzinfo=UTC),
        )
        self.frequency_calls: list[dict] = []
        self.value_novelty_calls: list[dict] = []
        self.combo_calls: list[dict] = []
        self.order_calls: list[dict] = []
        self.range_calls: list[dict] = []
        self.charset_calls: list[dict] = []
        self.entropy_calls: list[dict] = []
        self.shift_calls: list[dict] = []
        self.interval_calls: list[dict] = []

    def get_timeline_midpoint(self, case_id, source_ids):
        return self._midpoint

    def get_timeline_range(self, case_id, source_ids):
        return self._ts_range

    def find_frequency_anomalies(self, **kwargs):
        self.frequency_calls.append(kwargs)
        return "frequency-result"

    def find_value_novelty(self, **kwargs):
        self.value_novelty_calls.append(kwargs)
        return "value-novelty-result"

    def find_value_combos(self, **kwargs):
        self.combo_calls.append(kwargs)
        return "value-combo-result"

    def find_order_violations(self, **kwargs):
        self.order_calls.append(kwargs)
        return "order-result"

    def find_range_violations(self, **kwargs):
        self.range_calls.append(kwargs)
        return "range-result"

    def find_charset_novelty(self, **kwargs):
        self.charset_calls.append(kwargs)
        return "charset-result"

    def find_entropy_outliers(self, **kwargs):
        self.entropy_calls.append(kwargs)
        return "entropy-result"

    def find_proportion_shifts(self, **kwargs):
        self.shift_calls.append(kwargs)
        return "shift-result"

    def find_interval_periodicity(self, **kwargs):
        self.interval_calls.append(kwargs)
        return "interval-result"


@pytest.fixture()
def stub_field_stats_cache(monkeypatch):
    """Stub the per-source field-stats cache the router resolves for
    auto-field novelty runs (fields=None), so tests don't need a live
    ClickHouse or a real store schema behind ensure_source_field_stats."""

    async def _fake_ensure(store, ch, case_id, source_ids):
        return {}

    monkeypatch.setattr(events, "ensure_source_field_stats", _fake_ensure)
    monkeypatch.setattr(
        events, "merged_inventory", lambda stats, field_mappings=None: ([("artifact", 2, 10)], 10)
    )


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_frequency(patched_store, monkeypatch):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, _resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="frequency",
        fields=None,
        series_field="artifact",
        z_threshold=3.0,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert result == "frequency-result"
    assert len(fake_svc.frequency_calls) == 1
    assert fake_svc.frequency_calls[0]["series_field"] == "artifact"
    assert fake_svc.frequency_calls[0]["z_threshold"] == 3.0
    assert not fake_svc.value_novelty_calls


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_value_novelty(patched_store, monkeypatch):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, _resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="value_novelty",
        fields="artifact,attr:user_agent",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert result == "value-novelty-result"
    assert len(fake_svc.value_novelty_calls) == 1
    assert fake_svc.value_novelty_calls[0]["fields"] == ["artifact", "attr:user_agent"]
    # Explicit fields: the router must not resolve the field-stats cache.
    assert fake_svc.value_novelty_calls[0]["inventory"] is None
    assert fake_svc.value_novelty_calls[0]["inventory_total"] is None
    assert not fake_svc.frequency_calls


@pytest.mark.asyncio
async def test_run_stat_detector_auto_fields_resolves_cache_inventory(
    patched_store, monkeypatch, stub_field_stats_cache
):
    """M22(d): fields=None must resolve the candidate inventory from the
    per-source field-stats cache in the router and pass it to the detector,
    instead of letting the detector run the live field_inventory map scan."""
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="value_novelty",
        fields=None,
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    call = fake_svc.value_novelty_calls[0]
    assert call["fields"] is None
    assert call["inventory"] == [("artifact", 2, 10)]
    assert call["inventory_total"] == 10


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_value_combo(patched_store, monkeypatch):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, _resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="value_combo",
        fields="attr:action,attr:hour",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert result == "value-combo-result"
    assert fake_svc.combo_calls[0]["fields"] == ["attr:action", "attr:hour"]
    assert not fake_svc.value_novelty_calls


@pytest.mark.asyncio
async def test_run_stat_detector_value_combo_rejects_single_field(patched_store, monkeypatch):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    with pytest.raises(HTTPException) as exc:
        await events._run_stat_detector(
            "c1",
            "t1",
            ["s1"],
            detector="value_combo",
            fields="artifact",
            series_field="artifact",
            z_threshold=None,
            baseline_end=None,
            temporal=False,
            limit=50,
        )
    assert exc.value.status_code == 422
    assert not fake_svc.combo_calls


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_numeric_range(patched_store, monkeypatch):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, _resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="numeric_range",
        fields="attr:bytes",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert result == "range-result"
    assert fake_svc.range_calls[0]["fields"] == ["attr:bytes"]
    assert not fake_svc.value_novelty_calls


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_charset(patched_store, monkeypatch):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, _resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="charset",
        fields="attr:user",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert result == "charset-result"
    assert fake_svc.charset_calls[0]["fields"] == ["attr:user"]
    # Explicit fields → the field-stats cache inventory is not resolved.
    assert fake_svc.charset_calls[0]["inventory"] is None
    assert not fake_svc.value_novelty_calls


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_entropy(patched_store, monkeypatch):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, _resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="entropy",
        fields="attr:host",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert result == "entropy-result"
    assert fake_svc.entropy_calls[0]["fields"] == ["attr:host"]
    assert fake_svc.entropy_calls[0]["inventory"] is None
    assert not fake_svc.value_novelty_calls


def test_serialize_finding_entropy_shape():
    from tracesignal.db.anomaly_stats import EntropyFinding

    f = EntropyFinding(
        field="attr:host",
        value="kq3v9xz2m8w1",
        entropy=5.5,
        count=3,
        score=0.25,
        direction="above",
        lower=0.5,
        upper=4.5,
        first_seen="2024-01-01T00:00:00+00:00",
        event_id="evt-1",
        event=None,
        details={"detector": "entropy"},
    )
    out = events._serialize_finding(f)
    assert out["type"] == "entropy"
    assert out["entropy"] == 5.5
    assert out["direction"] == "above"


def test_serialize_finding_charset_shape():
    from tracesignal.db.anomaly_stats import CharsetFinding

    f = CharsetFinding(
        field="attr:user",
        value="ab\x00",
        novel_chars=["\x00"],
        count=2,
        score=4.6052,
        first_seen="2024-01-01T00:00:00+00:00",
        event_id="evt-1",
        event=None,
        details={"detector": "charset"},
    )
    out = events._serialize_finding(f)
    assert out["type"] == "charset"
    assert out["novel_chars"] == ["\x00"]
    assert out["score"] == 4.6052


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_proportion_shift(patched_store, monkeypatch):
    """proportion_shift dispatches with effective (config-default) thresholds."""
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="proportion_shift",
        fields="attr:eventid",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert result == "shift-result"
    call = fake_svc.shift_calls[0]
    assert call["fields"] == ["attr:eventid"]
    assert call["inventory"] is None
    # No baseline_id/baseline_end/temporal → the service sees windows=None and
    # returns insufficient_data itself (temporal-only, no 422).
    assert call["windows"] is None
    # Effective thresholds fall back to server config and are snapshotted for
    # the persisted run.
    cfg = events.get_settings()
    assert call["fdr_q"] == cfg.stat_shift_fdr_q
    assert call["min_ratio"] == cfg.stat_shift_min_ratio
    assert call["max_candidates_per_field"] == cfg.stat_shift_max_candidates_per_field
    assert resolution["shift_fdr_q"] == cfg.stat_shift_fdr_q
    assert resolution["shift_min_ratio"] == cfg.stat_shift_min_ratio
    assert not fake_svc.value_novelty_calls


@pytest.mark.asyncio
async def test_run_stat_detector_proportion_shift_request_overrides(patched_store, monkeypatch):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    _result, resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="proportion_shift",
        fields="attr:eventid",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
        fdr_q=0.01,
        min_ratio=3.0,
    )
    call = fake_svc.shift_calls[0]
    assert call["fdr_q"] == 0.01
    assert call["min_ratio"] == 3.0
    assert resolution["shift_fdr_q"] == 0.01
    assert resolution["shift_min_ratio"] == 3.0


@pytest.mark.asyncio
async def test_run_stat_detector_proportion_shift_passes_windows(patched_store, monkeypatch):
    """A legacy baseline_end split resolves to an AnalysisWindows for the detector."""
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="proportion_shift",
        fields="attr:eventid",
        series_field="artifact",
        z_threshold=None,
        baseline_end=datetime(2024, 1, 15, tzinfo=UTC),
        temporal=True,
        limit=50,
    )
    call = fake_svc.shift_calls[0]
    assert call["windows"] is not None
    assert call["windows"].baseline.end == datetime(2024, 1, 15, tzinfo=UTC)


@pytest.mark.asyncio
async def test_run_stat_detector_proportion_shift_auto_fields_resolves_inventory(
    patched_store, monkeypatch, stub_field_stats_cache
):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="proportion_shift",
        fields=None,
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    call = fake_svc.shift_calls[0]
    assert call["fields"] is None
    assert call["inventory"] == [("artifact", 2, 10)]
    assert call["inventory_total"] == 10


def test_serialize_finding_proportion_shift_shape():
    from tracesignal.db.anomaly_stats import ShiftFinding

    f = ShiftFinding(
        field="attr:eventid",
        value="4625",
        count=80,
        baseline_count=50,
        baseline_rate=0.005,
        window_rate=0.08,
        rate_ratio=16.0,
        direction="up",
        g_statistic=225.2477,
        p_value=0.0,
        q_value=0.0,
        score=225.2477,
        first_seen="2024-01-16T00:00:00+00:00",
        event_id="evt-1",
        event=None,
        details={"detector": "proportion_shift"},
    )
    out = events._serialize_finding(f)
    assert out["type"] == "proportion_shift"
    assert out["direction"] == "up"
    assert out["rate_ratio"] == 16.0
    assert out["q_value"] == 0.0
    assert out["score"] == 225.2477


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_interval_periodicity(patched_store, monkeypatch):
    """interval_periodicity dispatches with effective (config-default) thresholds."""
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="interval_periodicity",
        fields="attr:service",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert result == "interval-result"
    call = fake_svc.interval_calls[0]
    assert call["fields"] == ["attr:service"]
    assert call["windows"] is None
    cfg = events.get_settings()
    assert call["fdr_q"] == cfg.stat_interval_fdr_q
    assert call["min_rate_ratio"] == cfg.stat_interval_min_rate_ratio
    assert call["min_baseline_intervals"] == cfg.stat_interval_min_baseline_intervals
    assert call["beacon_min_intervals"] == cfg.stat_interval_beacon_min_intervals
    assert call["max_candidates_per_field"] == cfg.stat_interval_max_candidates_per_field
    assert resolution["interval_fdr_q"] == cfg.stat_interval_fdr_q
    assert resolution["interval_min_rate_ratio"] == cfg.stat_interval_min_rate_ratio
    assert not fake_svc.shift_calls
    assert not fake_svc.value_novelty_calls


@pytest.mark.asyncio
async def test_run_stat_detector_interval_request_overrides(patched_store, monkeypatch):
    """The generic fdr_q/min_ratio request params map onto the cadence thresholds."""
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    _result, resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="interval_periodicity",
        fields="attr:service",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
        fdr_q=0.01,
        min_ratio=3.0,
    )
    call = fake_svc.interval_calls[0]
    assert call["fdr_q"] == 0.01
    assert call["min_rate_ratio"] == 3.0
    assert resolution["interval_fdr_q"] == 0.01
    assert resolution["interval_min_rate_ratio"] == 3.0


def test_serialize_finding_interval_periodicity_shape():
    from tracesignal.db.anomaly_stats import IntervalFinding

    f = IntervalFinding(
        field="attr:service",
        value="heartbeat",
        direction="missed",
        count=0,
        baseline_count=20160,
        baseline_median_interval=60.0,
        window_median_interval=None,
        baseline_cv=0.017,
        window_cv=None,
        statistic=1234.5,
        p_value=0.0,
        q_value=0.0,
        score=300.0,
        first_seen=None,
        event_id="evt-bl-last",
        event=None,
        details={"detector": "interval_periodicity", "last_seen_baseline": "2024-01-14"},
    )
    out = events._serialize_finding(f)
    assert out["type"] == "interval_periodicity"
    assert out["direction"] == "missed"
    assert out["count"] == 0
    assert out["baseline_median_interval"] == 60.0
    assert out["score"] == 300.0
    assert out["details"]["last_seen_baseline"] == "2024-01-14"


@pytest.mark.asyncio
async def test_run_stat_detector_dispatches_to_timestamp_order(patched_store, monkeypatch):
    """timestamp_order dispatches without resolving a temporal midpoint (mode-less)."""
    fake_svc = _FakeStatAnomalyService(midpoint=datetime(2024, 6, 15, 12, 0, 0))
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    result, _resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="timestamp_order",
        fields=None,
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=True,  # ignored by this detector
        limit=50,
        min_skew_seconds=5.0,
    )
    assert result == "order-result"
    assert len(fake_svc.order_calls) == 1
    assert fake_svc.order_calls[0]["min_skew_seconds"] == 5.0
    # Mode-less: never touches the value/frequency paths.
    assert not fake_svc.frequency_calls
    assert not fake_svc.value_novelty_calls


@pytest.mark.asyncio
async def test_run_stat_detector_resolves_timeline_midpoint_when_temporal_and_no_baseline(
    patched_store, monkeypatch
):
    """temporal=True with no explicit baseline_end falls back to the timeline
    midpoint, converted to a baseline+suspect window pair via the legacy shim."""
    midpoint = datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC)
    fake_svc = _FakeStatAnomalyService(midpoint=midpoint)
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    _, resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="frequency",
        fields=None,
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=True,
        limit=50,
    )
    windows = fake_svc.frequency_calls[0]["windows"]
    # Baseline ends at the midpoint; a single "detect" suspect window follows.
    assert windows.baseline.end == midpoint
    assert len(windows.suspects) == 1
    assert windows.suspects[0].start == midpoint
    assert resolution["windows"]["baseline"]["end"] == midpoint.isoformat()


@pytest.mark.asyncio
async def test_run_stat_detector_explicit_baseline_end_wins_over_midpoint(
    patched_store, monkeypatch
):
    explicit = datetime(2024, 1, 10, 0, 0, 0, tzinfo=UTC)
    fake_svc = _FakeStatAnomalyService(midpoint=datetime(2024, 6, 15, 12, 0, 0, tzinfo=UTC))
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="frequency",
        fields=None,
        series_field="artifact",
        z_threshold=None,
        baseline_end=explicit,
        temporal=True,
        limit=50,
    )
    windows = fake_svc.frequency_calls[0]["windows"]
    assert windows.baseline.end == explicit


@pytest.mark.asyncio
async def test_run_stat_detector_excludes_normal_annotated_events(
    patched_store, monkeypatch, stub_field_stats_cache
):
    await patched_store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="normal-evt",
        annotation_id="ann1",
        annotation_type="normal",
        origin="user",
        content="",
    )
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="value_novelty",
        fields=None,
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert fake_svc.value_novelty_calls[0]["exclude_event_ids"] == {"normal-evt"}


# ---------------------------------------------------------------------------
# list_anomalies / tag_anomalies — C18 DetectorRun persistence
# ---------------------------------------------------------------------------


def _make_stat_result(status="ok", event_id="evt-1"):
    from tracesignal.db.anomaly_stats import StatAnomalyResult, ValueFinding

    finding = ValueFinding(
        field="artifact",
        value="rare-value",
        count=1,
        score=4.2,
        first_seen=None,
        event_id=event_id,
        event={"source_id": "s1"},
        details={},
    )
    return StatAnomalyResult(
        status=status,
        detector="value_novelty",
        method="self-baseline",
        baseline_size=100,
        results=[finding] if status == "ok" else [],
        z_threshold=None,
    )


class _FakeStatAnomalyServiceWithResult:
    """Returns a real StatAnomalyResult, for exercising the persist path."""

    ch = None  # accessed by the router's field-stats cache resolution

    def __init__(self, result):
        self._result = result

    def get_timeline_midpoint(self, case_id, source_ids):
        return None

    def find_value_novelty(self, **kwargs):
        return self._result

    def find_frequency_anomalies(self, **kwargs):
        return self._result


@pytest_asyncio.fixture()
async def timeline_setup(patched_store):
    await patched_store.create_case("c1", "Case One")
    await patched_store.create_source("c1", "s1", "source one", file_hash="h1", size_bytes=10)
    await patched_store.create_timeline("c1", "t1", "Timeline One", source_ids=["s1"])
    return patched_store


def _call_list_anomalies(persist: bool = True):
    return events.list_anomalies(
        "c1",
        "t1",
        detector="value_novelty",
        fields=None,
        series_field="artifact",
        z_threshold=None,
        min_skew_seconds=None,
        baseline_end=None,
        temporal=False,
        baseline_id=None,
        limit=50,
        persist=persist,
        case=Case(id="c1"),
        user=_fake_user(),
    )


@pytest.mark.asyncio
async def test_list_anomalies_persists_run_by_default(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    fake_svc = _FakeStatAnomalyServiceWithResult(_make_stat_result())
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    response = await _call_list_anomalies()

    assert response["run_id"] is not None
    run = await timeline_setup.get_detector_run("c1", response["run_id"])
    assert run is not None
    assert run.result["results"][0]["event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_list_anomalies_persist_false_does_not_write_a_run(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    fake_svc = _FakeStatAnomalyServiceWithResult(_make_stat_result())
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    response = await _call_list_anomalies(persist=False)

    assert response["run_id"] is None


@pytest.mark.asyncio
async def test_list_anomalies_does_not_persist_when_status_not_ok(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    fake_svc = _FakeStatAnomalyServiceWithResult(_make_stat_result(status="no_data"))
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    response = await _call_list_anomalies()

    assert response["run_id"] is None


@pytest.mark.asyncio
async def test_tag_anomalies_always_persists_a_run(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    fake_svc = _FakeStatAnomalyServiceWithResult(_make_stat_result())
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    body = events.TagAnomaliesRequest(detector="value_novelty")
    response = await events.tag_anomalies("c1", "t1", body, case=Case(id="c1"), user=_fake_user())

    assert response["run_id"] is not None
    run = await timeline_setup.get_detector_run("c1", response["run_id"])
    assert run is not None


# ---------------------------------------------------------------------------
# baseline_id resolution + forensic snapshot (Phase 3)
# ---------------------------------------------------------------------------


async def _make_baseline(store) -> str:
    definition = await store.create_baseline_definition(
        "c1",
        "t1",
        "incident",
        baseline_start=datetime(2024, 1, 1, tzinfo=UTC),
        baseline_end=datetime(2024, 1, 15, tzinfo=UTC),
        suspect_windows=[
            {
                "id": "w0",
                "label": "exfil",
                "start": "2024-02-01T00:00:00+00:00",
                "end": "2024-02-05T00:00:00+00:00",
            }
        ],
    )
    return definition.id


@pytest.mark.asyncio
async def test_run_stat_detector_baseline_id_builds_windows(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    """A baseline_id resolves the saved definition into detector windows and
    snapshots them (id + hash) into the resolution for the run params."""
    bid = await _make_baseline(timeline_setup)
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    _, resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="value_novelty",
        fields="artifact",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        baseline_id=bid,
        limit=50,
    )
    windows = fake_svc.value_novelty_calls[0]["windows"]
    assert windows.baseline.start == datetime(2024, 1, 1, tzinfo=UTC)
    assert [w.label for w in windows.suspects] == ["exfil"]
    assert resolution["baseline_id"] == bid
    assert len(resolution["windows_hash"]) == 64


@pytest.mark.asyncio
async def test_run_stat_detector_unknown_baseline_id_404s(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)
    with pytest.raises(HTTPException) as exc:
        await events._run_stat_detector(
            "c1",
            "t1",
            ["s1"],
            detector="value_novelty",
            fields="artifact",
            series_field="artifact",
            z_threshold=None,
            baseline_end=None,
            temporal=False,
            baseline_id="no-such-baseline",
            limit=50,
        )
    assert exc.value.status_code == 404


@pytest.mark.asyncio
async def test_run_stat_detector_applies_allowlist(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    """An allowlist entry for the detector is passed through as a (field,
    value) suppression set, and its hash is snapshotted."""
    await timeline_setup.create_allowlist_entry(
        "c1", "t1", "value_novelty", "artifact", "known_good"
    )
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    _, resolution = await events._run_stat_detector(
        "c1",
        "t1",
        ["s1"],
        detector="value_novelty",
        fields="artifact",
        series_field="artifact",
        z_threshold=None,
        baseline_end=None,
        temporal=False,
        limit=50,
    )
    assert fake_svc.value_novelty_calls[0]["allowlist"] == {("artifact", "known_good")}
    assert resolution["allowlist_count"] == 1
    assert len(resolution["allowlist_hash"]) == 64


@pytest.mark.asyncio
async def test_run_stat_detector_applies_wildcard_allowlist_across_detectors(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    """A detector-agnostic (`"*"`) entry suppresses its (field, value) for every
    value detector, while a detector-scoped entry only affects its own."""
    # Wildcard: normal for all value detectors. Scoped: charset only.
    await timeline_setup.create_allowlist_entry("c1", "t1", "*", "artifact", "wild")
    await timeline_setup.create_allowlist_entry("c1", "t1", "charset", "artifact", "cs_only")
    fake_svc = _FakeStatAnomalyService()
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    async def run(detector):
        return await events._run_stat_detector(
            "c1",
            "t1",
            ["s1"],
            detector=detector,
            fields="artifact",
            series_field="artifact",
            z_threshold=None,
            baseline_end=None,
            temporal=False,
            limit=50,
        )

    await run("value_novelty")
    # value_novelty sees the wildcard but not the charset-scoped entry.
    assert fake_svc.value_novelty_calls[0]["allowlist"] == {("artifact", "wild")}

    _, charset_resolution = await run("charset")
    charset_allowlist = fake_svc.charset_calls[0]["allowlist"]
    assert charset_allowlist == {("artifact", "wild"), ("artifact", "cs_only")}
    assert charset_resolution["allowlist_count"] == 2


@pytest.mark.asyncio
async def test_detector_run_replays_after_baseline_deleted(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    """A persisted run stays self-describing after its baseline definition is
    deleted — the window snapshot rides in the run params."""
    bid = await _make_baseline(timeline_setup)
    fake_svc = _FakeStatAnomalyServiceWithResult(_make_stat_result())
    fake_svc.get_timeline_range = lambda c, s: (  # type: ignore[attr-defined]
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 3, 1, tzinfo=UTC),
    )
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    response = await events.list_anomalies(
        "c1",
        "t1",
        detector="value_novelty",
        fields="artifact",
        series_field="artifact",
        z_threshold=None,
        min_skew_seconds=None,
        baseline_end=None,
        temporal=False,
        baseline_id=bid,
        limit=50,
        persist=True,
        case=Case(id="c1"),
        user=_fake_user(),
    )
    run_id = response["run_id"]
    assert await timeline_setup.delete_baseline_definition("c1", "t1", bid) is True

    fetched = await events.get_detector_run("c1", run_id, case=Case(id="c1"))
    assert fetched["params"]["baseline_id"] == bid
    assert fetched["params"]["windows"]["suspect_windows"][0]["label"] == "exfil"


def test_window_phrase_names_window():
    """_window_phrase renders the finding's attributed suspect window."""
    phrase = events._window_phrase(
        {
            "window_label": "exfil",
            "window_start": "2024-02-01T00:00:00+00:00",
            "window_end": "2024-02-05T00:00:00+00:00",
        }
    )
    assert "exfil" in phrase
    assert "2024-02-01" in phrase
    # Frequency findings use the suspect_window_* keys.
    assert "incident" in events._window_phrase({"suspect_window_label": "incident"})
    assert events._window_phrase({}) == ""


@pytest.mark.asyncio
async def test_get_detector_run_endpoint_returns_persisted_run(
    timeline_setup, monkeypatch, stub_field_stats_cache
):
    fake_svc = _FakeStatAnomalyServiceWithResult(_make_stat_result())
    monkeypatch.setattr(events, "_get_stat_anomaly_service", lambda: fake_svc)

    scan = await _call_list_anomalies()
    fetched = await events.get_detector_run("c1", scan["run_id"], case=Case(id="c1"))

    assert fetched["detector"] == "value_novelty"
    assert fetched["result"]["results"][0]["event_id"] == "evt-1"


@pytest.mark.asyncio
async def test_get_detector_run_endpoint_404s_for_unknown_id(timeline_setup):
    with pytest.raises(HTTPException) as exc_info:
        await events.get_detector_run("c1", "no-such-run", case=Case(id="c1"))
    assert exc_info.value.status_code == 404


def test_field_encoder_caches_load_failure(monkeypatch):
    """A failed model load is cached — a broken/missing local model must not
    re-attempt a multi-second load (or a network download) on every wizard
    open."""
    import tracesignal.models.embeddings as emb_mod

    attempts: list[int] = []

    class _BrokenModel:
        def __init__(self):
            attempts.append(1)
            raise RuntimeError("weights not cached")

    monkeypatch.setattr(emb_mod, "EmbeddingModel", _BrokenModel)
    monkeypatch.setattr(events, "_embedding_model", None)

    assert events._get_field_encoder() is None
    assert events._get_field_encoder() is None
    assert len(attempts) == 1
