"""Tests for the ClickHouse event query builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from tracevector.db.queries import EventQuery, EventQueryService, TagFilter


@dataclass
class FakeQueryResult:
    """Minimal QueryResult stand-in for clickhouse-connect."""

    result_rows: list[list[Any]] | None = None
    column_names: list[str] | None = None


class FakeClickHouseClient:
    """Records queries and parameters, returns canned results."""

    def __init__(self, event_rows: list[list[Any]] | None = None) -> None:
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.event_rows = event_rows or []
        self.event_columns = [
            "event_id",
            "case_id",
            "source_id",
            "source_file",
            "byte_offset",
            "line_number",
            "content_hash",
            "file_hash",
            "parser_name",
            "parser_version",
            "ingest_time",
            "message",
            "timestamp",
            "timestamp_desc",
            "artifact",
            "artifact_long",
            "display_name",
            "tags",
            "attributes",
            "embedding_model",
            "embedding_config_hash",
            "vector_id",
        ]

    def query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> FakeQueryResult:
        self.queries.append((query, parameters))
        if query.strip().startswith("SELECT count()"):
            return FakeQueryResult(result_rows=[[0]])
        return FakeQueryResult(
            result_rows=self.event_rows,
            column_names=self.event_columns,
        )


class FakeClickHouseStore:
    """Minimal ClickHouseStore stand-in."""

    def __init__(self, event_rows: list[list[Any]] | None = None) -> None:
        self.database = "tracevector"
        self.client = FakeClickHouseClient(event_rows)
        self.schema_initialized = False

    def init_schema(self) -> None:
        self.schema_initialized = True


@pytest.fixture
def service() -> EventQueryService:
    return EventQueryService(store=FakeClickHouseStore())


def _last_query(service: EventQueryService) -> tuple[str, dict[str, Any] | None]:
    return service.store.client.queries[-1]


def _find_query(service: EventQueryService, prefix: str) -> tuple[str, dict[str, Any] | None]:
    for query, params in service.store.client.queries:
        if query.strip().startswith(prefix):
            return query, params
    raise AssertionError(f"Query starting with {prefix!r} not found")


def test_basic_query_parameterizes_case_id(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1"))
    query, params = _last_query(service)
    assert "case_id = {p0:String}" in query
    assert params is not None
    assert params.get("p0") == "case-1"


def test_source_ids_filter_is_parameterized(service: EventQueryService) -> None:
    """Multiple source_ids use has(Array(String), toString(col)) — not IN(...) —
    because ClickHouse 24.x requires the second IN argument to be a constant
    or table expression, and source_id/event_id may be non-String columns
    (event_id is UUID), so the column is cast via toString() for a common type.
    """
    service.query(EventQuery(case_id="case-1", source_ids=["s1", "s2"]))
    query, params = _last_query(service)
    assert "has({p1:Array(String)}, toString(source_id))" in query
    assert params.get("p1") == ["s1", "s2"]


def test_single_source_id_filter_is_parameterized(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1", source_id="s1"))
    query, params = _last_query(service)
    assert "source_id = {p1:String}" in query
    assert params.get("p1") == "s1"


def test_text_search_uses_parameterized_like(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1", q="login"))
    query, params = _last_query(service)
    assert "message ILIKE {p1:String}" in query
    assert params.get("p1") == "%login%"


def test_artifact_and_tag_filters_are_parameterized(
    service: EventQueryService,
) -> None:
    service.query(EventQuery(case_id="case-1", artifact="auth", tag="success"))
    query, params = _last_query(service)
    assert "artifact = {p1:String}" in query
    assert "has(tags, {p2:String})" in query
    assert params.get("p1") == "auth"
    assert params.get("p2") == "success"


def test_artifact_and_artifacts_together_merge_not_and(
    service: EventQueryService,
) -> None:
    """`artifact` (singular) and `artifacts` (plural) are independent
    optional filters on the same column — applying both as separate ANDed
    predicates would require `artifact = 'a' AND artifact IN ('b')`
    simultaneously, which is unsatisfiable and silently returns zero rows
    (U3). They must merge into one effective filter instead."""
    service.query(EventQuery(case_id="case-1", artifact="a", artifacts=["b"]))
    query, params = _last_query(service)
    assert query.count("artifact = ") + query.count("toString(artifact)") == 1
    assert "has({p1:Array(String)}, toString(artifact))" in query
    assert sorted(params["p1"]) == ["a", "b"]


def test_artifact_and_artifacts_dedupe_overlap(service: EventQueryService) -> None:
    """When `artifact` also appears in `artifacts`, the merge must not
    duplicate it into the IN-list."""
    service.query(EventQuery(case_id="case-1", artifact="a", artifacts=["a", "b"]))
    query, params = _last_query(service)
    assert sorted(params["p1"]) == ["a", "b"]


def test_artifacts_plural_alone_single_value_uses_equality(
    service: EventQueryService,
) -> None:
    service.query(EventQuery(case_id="case-1", artifacts=["only"]))
    query, params = _last_query(service)
    assert "artifact = {p1:String}" in query
    assert params["p1"] == "only"


def test_time_range_filter_formats_datetime(service: EventQueryService) -> None:
    start = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    end = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
    service.query(EventQuery(case_id="case-1", start=start, end=end))
    query, params = _last_query(service)
    assert "timestamp >= {p1:String}" in query
    assert "timestamp <= {p2:String}" in query
    assert params.get("p1") == "2024-01-01 12:00:00"
    assert params.get("p2") == "2024-01-02 12:00:00"


def test_top_level_field_filter_uses_column(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"display_name": "auth.log"},
        )
    )
    query, params = _last_query(service)
    assert "display_name = {p1:String}" in query
    assert params.get("p1") == "auth.log"


def test_unknown_field_filter_uses_attributes_map(
    service: EventQueryService,
) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"ip_address_city": "Falkenstein"},
        )
    )
    query, params = _last_query(service)
    assert "attributes[{p1:String}] = {p2:String}" in query
    assert params.get("p1") == "ip_address_city"
    assert params.get("p2") == "Falkenstein"


def test_field_exclusion_uses_not_equals(service: EventQueryService) -> None:
    """A single exclusion value uses != — field_exclusions values are lists
    (multiple excluded values per field use NOT IN {Array(String)} instead)."""
    service.query(
        EventQuery(
            case_id="case-1",
            field_exclusions={"display_name": ["auth.log"]},
        )
    )
    query, params = _last_query(service)
    assert "display_name != {p1:String}" in query
    assert params.get("p1") == "auth.log"


def test_malicious_input_is_not_interpolated(service: EventQueryService) -> None:
    injection = "'; DROP TABLE events; --"
    service.query(EventQuery(case_id="case-1", q=injection))
    query, params = _last_query(service)
    # The dangerous string should appear only as a bound parameter, never in the SQL.
    assert injection not in query
    assert "DROP TABLE" not in query
    assert params.get("p1") == f"%{injection}%"


def test_combined_query_builds_single_where_clause(
    service: EventQueryService,
) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            source_ids=["s1"],
            q="token",
            artifact="auth",
            field_filters={"ip_address_city": "Falkenstein"},
            field_exclusions={"status_code": ["200"]},
        )
    )
    count_query, count_params = _find_query(service, "SELECT count()")
    assert "case_id = {p0:String}" in count_query
    assert "has({p1:Array(String)}, toString(source_id))" in count_query
    assert "message ILIKE {p2:String}" in count_query
    assert "artifact = {p3:String}" in count_query
    assert "attributes[{p4:String}] = {p5:String}" in count_query
    assert "attributes[{p6:String}] != {p7:String}" in count_query
    assert count_params is not None
    assert set(count_params.keys()) == {f"p{i}" for i in range(8)}


def test_event_ids_filter_uses_tostring_for_uuid_column(
    service: EventQueryService,
) -> None:
    """event_id is a native ClickHouse UUID column — has(Array(String), event_id)
    has no common type (ClickHouse error 386 NO_COMMON_TYPE) and fails at query
    time regardless of whether the list is empty or populated. The column must
    be cast via toString() for has() to compare it against a String array.
    """
    service.query(EventQuery(case_id="case-1", event_ids=["e1", "e2"]))
    query, params = _last_query(service)
    assert "has({p1:Array(String)}, toString(event_id))" in query
    assert params.get("p1") == ["e1", "e2"]


def test_empty_event_ids_filter_matches_nothing_not_stale_syntax(
    service: EventQueryService,
) -> None:
    """An empty (but non-None) event_ids list means 'match zero events' (e.g. a
    tag/anomaly filter that currently has no matches) — it must still produce
    valid, type-compatible SQL rather than erroring or being silently ignored.
    """
    service.query(EventQuery(case_id="case-1", event_ids=[]))
    query, params = _last_query(service)
    assert "has({p1:Array(String)}, toString(event_id))" in query
    assert params.get("p1") == []


def test_tags_include_filter_emits_compound_or_predicate(
    service: EventQueryService,
) -> None:
    """C13: the unified tag filter must OR-combine ClickHouse-native
    hasAny(tags, ...) with the pre-resolved Postgres event_id list in a
    single predicate, ANDed with everything else — not a second ClickHouse
    round trip resolved into event_ids.
    """
    service.query(
        EventQuery(
            case_id="case-1",
            tags_include=TagFilter(tag_values=["urgent"], postgres_event_ids=["ann-evt"]),
        )
    )
    query, params = _last_query(service)
    assert (
        "(hasAny(tags, {p1:Array(String)}) OR has({p2:Array(String)}, toString(event_id)))" in query
    )
    assert "NOT (hasAny" not in query
    assert params.get("p1") == ["urgent"]
    assert params.get("p2") == ["ann-evt"]


def test_tags_exclude_filter_negates_whole_predicate(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            tags_exclude=TagFilter(tag_values=["noisy"], postgres_event_ids=["tagged-evt"]),
        )
    )
    query, params = _last_query(service)
    assert (
        "NOT (hasAny(tags, {p1:Array(String)}) OR has({p2:Array(String)}, toString(event_id)))"
        in query
    )
    assert params.get("p1") == ["noisy"]
    assert params.get("p2") == ["tagged-evt"]


def test_tags_include_and_exclude_are_independent_predicates(
    service: EventQueryService,
) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            tags_include=TagFilter(tag_values=["a"], postgres_event_ids=["e1"]),
            tags_exclude=TagFilter(tag_values=["b"], postgres_event_ids=["e2"]),
        )
    )
    query, _params = _last_query(service)
    assert query.count("hasAny(tags,") == 2
    assert "NOT (hasAny(tags, {p3:Array(String)})" in query


def test_event_timestamps_get_explicit_utc_offset(service: EventQueryService) -> None:
    """timestamp/ingest_time come back from clickhouse-connect as naive
    datetimes (the columns have no explicit timezone component). Serializing
    a naive datetime omits the UTC offset, which JS's Date parser then treats
    as local time — silently shifting every displayed/compared timestamp by
    the browser's UTC offset. Both fields must carry an explicit '+00:00'.
    """
    naive_ts = datetime(2026, 6, 25, 7, 30, 1)
    naive_ingest = datetime(2026, 6, 25, 8, 0, 0)
    row = [
        "evt-1",
        "case-1",
        "src-1",
        "file.log",
        0,
        1,
        "hash",
        "hash",
        "parser",
        "1.0",
        naive_ingest,
        "hello",
        naive_ts,
        "desc",
        "artifact",
        "artifact_long",
        "display",
        [],
        {},
        None,
        None,
        None,
    ]
    seeded = EventQueryService(store=FakeClickHouseStore(event_rows=[row]))
    page = seeded.query(EventQuery(case_id="case-1"))
    assert len(page.events) == 1
    event = page.events[0]
    assert event["timestamp"] == "2026-06-25T07:30:01+00:00"
    assert event["ingest_time"] == "2026-06-25T08:00:00+00:00"


def test_event_id_is_stringified_from_native_uuid(service: EventQueryService) -> None:
    """clickhouse-connect returns `event_id` as a `uuid.UUID` (the column is
    natively `UUID`), while Postgres annotations key on `str`. Export's
    annotation lookup (`annotations_by_event.get(row["event_id"])`) silently
    misses every match if `event_id` isn't coerced to `str` here.
    """
    import uuid

    native_id = uuid.uuid4()
    ts = datetime(2026, 6, 25, 7, 30, 1)
    row = [
        native_id,
        "case-1",
        "src-1",
        "file.log",
        0,
        1,
        "hash",
        "hash",
        "parser",
        "1.0",
        ts,
        "hello",
        ts,
        "desc",
        "artifact",
        "artifact_long",
        "display",
        [],
        {},
        None,
        None,
        None,
    ]
    seeded = EventQueryService(store=FakeClickHouseStore(event_rows=[row]))
    page = seeded.query(EventQuery(case_id="case-1"))
    assert page.events[0]["event_id"] == str(native_id)
    assert isinstance(page.events[0]["event_id"], str)


# ── keyset cursor pagination tests ─────────────────────────────────────────────


def _cursor_row(event_id: str, ts: datetime) -> list[Any]:
    return [
        event_id,
        "case-1",
        "src-1",
        "file.log",
        0,
        1,
        "hash",
        "hash",
        "parser",
        "1.0",
        ts,
        "hello",
        ts,
        "desc",
        "artifact",
        "artifact_long",
        "display",
        [],
        {},
        None,
        None,
        None,
    ]


def test_after_cursor_uses_lt_predicate_for_default_desc_order(
    service: EventQueryService,
) -> None:
    ts = datetime(2026, 6, 25, 7, 30, 1)
    service.query(EventQuery(case_id="case-1", after=(ts, "evt-1")))
    query, params = _last_query(service)
    assert (
        "(coalesce(timestamp, {p3:DateTime64(3)}), event_id) < "
        "({p1:DateTime64(3)}, {p2:UUID})" in query
    )
    assert params["p1"] == "2026-06-25 07:30:01.000"
    assert params["p2"] == "evt-1"
    assert params["p3"] == "2299-12-31 23:59:59.999"
    assert "OFFSET" not in query


def test_before_cursor_uses_gt_predicate_and_reversed_fetch_direction(
    service: EventQueryService,
) -> None:
    ts = datetime(2026, 6, 25, 7, 30, 1)
    service.query(EventQuery(case_id="case-1", before=(ts, "evt-1")))
    query, _ = _last_query(service)
    assert (
        "(coalesce(timestamp, {p3:DateTime64(3)}), event_id) > "
        "({p1:DateTime64(3)}, {p2:UUID})" in query
    )
    assert "ORDER BY timestamp ASC, event_id ASC" in query


def test_after_and_before_together_rejected(service: EventQueryService) -> None:
    ts = datetime(2026, 6, 25, 7, 30, 1)
    with pytest.raises(ValueError):
        service.query(EventQuery(case_id="case-1", after=(ts, "a"), before=(ts, "b")))


def test_cursor_mode_skips_count_query(service: EventQueryService) -> None:
    ts = datetime(2026, 6, 25, 7, 30, 1)
    page = service.query(EventQuery(case_id="case-1", after=(ts, "evt-1")))
    assert not any(q.strip().startswith("SELECT count()") for q, _ in service.store.client.queries)
    assert page.total is None


def test_after_cursor_has_more_after_when_extra_row_returned() -> None:
    rows = [_cursor_row(f"evt-{i}", datetime(2026, 6, 25, 7, 30, i)) for i in range(3)]
    svc = EventQueryService(store=FakeClickHouseStore(event_rows=rows))
    page = svc.query(
        EventQuery(case_id="case-1", limit=2, after=(datetime(2026, 6, 25, 7, 29, 0), "evt-0"))
    )
    assert len(page.events) == 2
    assert page.has_more_after is True
    assert page.has_more_before is False
    assert page.total is None


def test_before_cursor_reverses_rows_back_to_display_order() -> None:
    rows = [
        _cursor_row("evt-1", datetime(2026, 6, 25, 7, 30, 1)),
        _cursor_row("evt-2", datetime(2026, 6, 25, 7, 30, 2)),
    ]
    svc = EventQueryService(store=FakeClickHouseStore(event_rows=rows))
    page = svc.query(
        EventQuery(
            case_id="case-1",
            limit=2,
            before=(datetime(2026, 6, 25, 7, 30, 5), "evt-5"),
        )
    )
    assert [e["event_id"] for e in page.events] == ["evt-2", "evt-1"]


def test_cursor_page_echoes_next_and_prev_cursor() -> None:
    rows = [
        _cursor_row("evt-1", datetime(2026, 6, 25, 7, 30, 1)),
        _cursor_row("evt-2", datetime(2026, 6, 25, 7, 30, 2)),
    ]
    svc = EventQueryService(store=FakeClickHouseStore(event_rows=rows))
    page = svc.query(
        EventQuery(case_id="case-1", limit=2, after=(datetime(2026, 6, 25, 7, 29, 0), "evt-0"))
    )
    assert page.prev_cursor == ("2026-06-25T07:30:01+00:00", "evt-1")
    assert page.next_cursor == ("2026-06-25T07:30:02+00:00", "evt-2")


# ── NULL-timestamp cursor tests (F3) ───────────────────────────────────────────


def test_cursor_substitutes_sentinel_for_null_timestamp() -> None:
    """A NULL-timestamp row at a page boundary must never produce a `None`
    cursor component — `[null, id]` serializes to JSON and is not a
    parseable "<iso-ts>,<event_id>" string on the way back in (400)."""
    row = _cursor_row("evt-null", None)
    svc = EventQueryService(store=FakeClickHouseStore(event_rows=[row]))
    page = svc.query(EventQuery(case_id="case-1"))
    assert page.events[0]["timestamp"] is None
    assert page.prev_cursor == ("2299-12-31T23:59:59.999000+00:00", "evt-null")
    assert page.next_cursor == ("2299-12-31T23:59:59.999000+00:00", "evt-null")


def test_cursor_predicate_coalesces_null_timestamp_to_sentinel(
    service: EventQueryService,
) -> None:
    """The keyset predicate must coalesce the `timestamp` column to the same
    sentinel used in cursor construction, or a NULL-timestamp row's tuple
    comparison evaluates to NULL (not true/false) and the row is silently
    unreachable via pagination regardless of direction."""
    ts = datetime(2026, 6, 25, 7, 30, 1)
    service.query(EventQuery(case_id="case-1", after=(ts, "evt-1")))
    query, params = _last_query(service)
    assert "coalesce(timestamp, {p3:DateTime64(3)})" in query
    assert params["p3"] == "2299-12-31 23:59:59.999"


def test_cursor_predicate_maps_empty_event_id_to_min_uuid(
    service: EventQueryService,
) -> None:
    """The jump-to-time synthetic lower bound (empty event_id, meaning "any
    event at this timestamp") must map to the minimum UUID, not an empty
    string — the predicate now compares native UUIDs, and an empty string
    is not a valid UUID literal."""
    ts = datetime(2026, 6, 25, 7, 30, 1)
    service.query(EventQuery(case_id="case-1", before=(ts, "")))
    _, params = _last_query(service)
    assert params["p2"] == "00000000-0000-0000-0000-000000000000"


# ── iter_events tests ──────────────────────────────────────────────────────────


class _BatchedFakeClient:
    """FakeClickHouseClient that returns pages of rows to simulate iter_events paging.

    The first *full_batch_count* non-count queries return *batch_size* rows each;
    the next query returns *remainder* rows, then all further queries return empty.
    """

    def __init__(
        self,
        columns: list[str],
        batch_size: int,
        full_batch_count: int = 1,
        remainder: int = 0,
    ) -> None:
        self.columns = columns
        self.batch_size = batch_size
        self.full_batch_count = full_batch_count
        self.remainder = remainder
        self._select_call = 0
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def _make_rows(self, n: int) -> list[list[Any]]:
        return [
            [f"evt-{self._select_call}-{i}"] + ["x"] * (len(self.columns) - 1) for i in range(n)
        ]

    def query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> FakeQueryResult:
        self.queries.append((query, parameters))
        if query.strip().startswith("SELECT count()"):
            return FakeQueryResult(result_rows=[[0]])
        call = self._select_call
        self._select_call += 1
        if call < self.full_batch_count:
            rows = self._make_rows(self.batch_size)
        elif call == self.full_batch_count:
            rows = self._make_rows(self.remainder)
        else:
            rows = []
        return FakeQueryResult(result_rows=rows, column_names=self.columns)


_EVENT_COLUMNS = [
    "event_id",
    "case_id",
    "source_id",
    "source_file",
    "byte_offset",
    "line_number",
    "content_hash",
    "parser_name",
    "parser_version",
    "ingest_time",
    "message",
    "timestamp",
    "timestamp_desc",
    "artifact",
    "artifact_long",
    "display_name",
    "tags",
    "attributes",
    "embedding_model",
    "embedding_config_hash",
    "vector_id",
]


def _batched_service(
    batch_size: int, full_batches: int = 1, remainder: int = 0
) -> EventQueryService:
    client = _BatchedFakeClient(
        columns=_EVENT_COLUMNS,
        batch_size=batch_size,
        full_batch_count=full_batches,
        remainder=remainder,
    )
    store = FakeClickHouseStore()
    store.client = client  # type: ignore[assignment]
    return EventQueryService(store=store)


def test_iter_events_yields_all_rows_single_batch() -> None:
    svc = _batched_service(batch_size=3, full_batches=0, remainder=2)
    rows = list(svc.iter_events(EventQuery(case_id="c1"), batch_size=3))
    assert len(rows) == 2


def test_iter_events_paginates_multiple_full_batches() -> None:
    """Two full batches of 5 + a remainder of 3 = 13 total rows."""
    svc = _batched_service(batch_size=5, full_batches=2, remainder=3)
    rows = list(svc.iter_events(EventQuery(case_id="c1"), batch_size=5))
    assert len(rows) == 13


def test_iter_events_exact_multiple_terminates() -> None:
    """Batch size exactly divides total; client returns empty on third call."""
    svc = _batched_service(batch_size=4, full_batches=2, remainder=0)
    rows = list(svc.iter_events(EventQuery(case_id="c1"), batch_size=4))
    assert len(rows) == 8


def test_iter_events_yields_dicts_with_expected_keys() -> None:
    svc = _batched_service(batch_size=10, full_batches=0, remainder=1)
    rows = list(svc.iter_events(EventQuery(case_id="c1"), batch_size=10))
    assert len(rows) == 1
    assert "event_id" in rows[0]
    assert "message" in rows[0]
    assert "source_id" in rows[0]


def test_iter_events_where_clause_is_parameterized() -> None:
    """Filter values must never appear as raw SQL — injection is impossible."""
    injection = "'; DROP TABLE events; --"
    svc = _batched_service(batch_size=5, full_batches=0, remainder=0)
    list(svc.iter_events(EventQuery(case_id=injection), batch_size=5))
    select_queries = [
        q
        for q, _ in svc.store.client.queries  # type: ignore[union-attr]
        if not q.strip().startswith("SELECT count()")
    ]
    assert select_queries, "Expected at least one SELECT query"
    for sql in select_queries:
        assert injection not in sql
        assert "DROP TABLE" not in sql


def test_iter_events_applies_filters_in_where_clause() -> None:
    svc = _batched_service(batch_size=5, full_batches=0, remainder=0)
    list(
        svc.iter_events(
            EventQuery(case_id="c1", artifact="auth", tag="malware"),
            batch_size=5,
        )
    )
    select_queries = [
        (q, p)
        for q, p in svc.store.client.queries  # type: ignore[union-attr]
        if not q.strip().startswith("SELECT count()")
    ]
    assert select_queries
    sql, params = select_queries[0]
    assert "artifact = {p1:String}" in sql
    assert "has(tags, {p2:String})" in sql
    assert params is not None
    assert params.get("p1") == "auth"
    assert params.get("p2") == "malware"


# ── order (sort direction) tests ───────────────────────────────────────────────


def _find_order_query(service: EventQueryService) -> str:
    """Return the first query that contains ORDER BY (the data query, not count)."""
    for query, _ in service.store.client.queries:
        if "ORDER BY" in query:
            return query
    raise AssertionError("No ORDER BY query found")


def test_query_default_order_is_desc(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1"))
    query = _find_order_query(service)
    assert "ORDER BY timestamp DESC" in query


def test_query_order_asc(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1", order="asc"))
    query = _find_order_query(service)
    assert "ORDER BY timestamp ASC" in query


def test_iter_events_order_asc() -> None:
    svc = _batched_service(batch_size=5, full_batches=0, remainder=3)
    list(svc.iter_events(EventQuery(case_id="c1", order="asc"), batch_size=5))
    select_queries = [
        q
        for q, _ in svc.store.client.queries  # type: ignore[union-attr]
        if not q.strip().startswith("SELECT count()")
    ]
    assert select_queries
    assert "ORDER BY timestamp ASC" in select_queries[0]


# ── list_fields tests ──────────────────────────────────────────────────────────


class _FieldsFakeClient:
    """Returns a canned groupUniqArrayArray result for list_fields queries."""

    def __init__(self, keys: list[str]) -> None:
        self._keys = keys
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> FakeQueryResult:
        self.queries.append((query, parameters))
        return FakeQueryResult(result_rows=[[self._keys]], column_names=["keys"])


def _fields_service(keys: list[str]) -> EventQueryService:
    store = FakeClickHouseStore()
    store.client = _FieldsFakeClient(keys)  # type: ignore[assignment]
    return EventQueryService(store=store)


def test_list_fields_returns_sorted_attribute_keys() -> None:
    svc = _fields_service(["zebra", "alpha", "middle"])
    result = svc.list_fields("c1", ["s1"])
    assert result["attributes"] == ["alpha", "middle", "zebra"]


def test_list_fields_returns_top_level_columns() -> None:
    from tracevector.db.queries import TOP_LEVEL_DISPLAY_COLUMNS

    svc = _fields_service([])
    result = svc.list_fields("c1", ["s1"])
    assert result["top_level"] == TOP_LEVEL_DISPLAY_COLUMNS


def test_list_fields_empty_dataset() -> None:
    svc = _fields_service([])
    result = svc.list_fields("c1", ["s1"])
    assert result["attributes"] == []


# ── histogram tests ────────────────────────────────────────────────────────────

from datetime import timedelta  # noqa: E402 (already imported at top, repeated for clarity)


class _HistogramFakeClient:
    """Returns canned range + bucket results for histogram queries."""

    def __init__(self, min_ts: datetime, max_ts: datetime, bucket_rows: list[Any]) -> None:
        self._min = min_ts
        self._max = max_ts
        self._buckets = bucket_rows
        self._call = 0
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> FakeQueryResult:
        self.queries.append((query, parameters))
        stripped = query.strip()
        if "min(timestamp)" in stripped:
            return FakeQueryResult(
                result_rows=[[self._min, self._max]], column_names=["min", "max"]
            )
        if "toStartOfInterval" in stripped:
            return FakeQueryResult(result_rows=self._buckets, column_names=["bucket", "c"])
        # count() fallback
        return FakeQueryResult(result_rows=[[len(self._buckets)]])


def _histogram_service(
    min_ts: datetime, max_ts: datetime, bucket_rows: list[Any]
) -> EventQueryService:
    store = FakeClickHouseStore()
    store.client = _HistogramFakeClient(min_ts, max_ts, bucket_rows)  # type: ignore[assignment]
    return EventQueryService(store=store)


def test_histogram_returns_bucket_count() -> None:
    min_ts = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, 0, 0, 0, tzinfo=UTC)
    # 3 fake buckets with datetime objects as bucket start
    bucket_rows = [
        [min_ts, 10],
        [min_ts + timedelta(hours=8), 20],
        [min_ts + timedelta(hours=16), 5],
    ]
    svc = _histogram_service(min_ts, max_ts, bucket_rows)
    result = svc.histogram(EventQuery(case_id="c1", source_ids=["s1"]), buckets=60)
    assert result["interval_seconds"] > 0
    assert len(result["buckets"]) == 3
    assert result["buckets"][1]["count"] == 20


def test_histogram_respects_explicit_time_range() -> None:
    """When start/end are provided, no range-query should be issued."""
    min_ts = datetime(2024, 3, 1, tzinfo=UTC)
    max_ts = datetime(2024, 3, 2, tzinfo=UTC)
    svc = _histogram_service(min_ts, max_ts, [[min_ts, 7]])
    eq = EventQuery(case_id="c1", source_ids=["s1"], start=min_ts, end=max_ts)
    result = svc.histogram(eq, buckets=60)
    # No min/max range query should have been issued
    range_queries = [q for q, _ in svc.store.client.queries if "min(timestamp)" in q]  # type: ignore[union-attr]
    assert range_queries == []
    assert result["buckets"][0]["count"] == 7


def test_histogram_empty_dataset_returns_empty_buckets() -> None:
    """Client returns None timestamps → empty bucket list."""
    store = FakeClickHouseStore()

    class _EmptyClient:
        queries: list = []

        def query(self, query: str, parameters: Any = None, **_: Any) -> FakeQueryResult:
            self.queries.append(query)
            if "min(timestamp)" in query:
                return FakeQueryResult(result_rows=[[None, None]])
            return FakeQueryResult(result_rows=[], column_names=["bucket", "c"])

    store.client = _EmptyClient()  # type: ignore[assignment]
    svc = EventQueryService(store=store)
    result = svc.histogram(EventQuery(case_id="c1", source_ids=["s1"]))
    assert result["buckets"] == []
    assert result["min"] is None


# ── viz aggregation tests (field_terms / field_numeric_stats / field_value_timeseries) ─────


class _VizFakeClient:
    """Dispatches canned results by matching a substring in the query text.

    *responses* is tried in order — put more specific substrings first so a
    query matching several markers gets the intended canned result.
    """

    def __init__(self, responses: list[tuple[str, FakeQueryResult]]) -> None:
        self._responses = responses
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def query(
        self, query: str, parameters: dict[str, Any] | None = None, **_kwargs: Any
    ) -> FakeQueryResult:
        self.queries.append((query, parameters))
        for substr, result in self._responses:
            if substr in query:
                return result
        raise AssertionError(f"No fake response configured for query:\n{query}")


def _viz_service(responses: list[tuple[str, FakeQueryResult]]) -> EventQueryService:
    store = FakeClickHouseStore()
    store.client = _VizFakeClient(responses)  # type: ignore[assignment]
    return EventQueryService(store=store)


def test_field_terms_returns_top_values_and_other_count() -> None:
    svc = _viz_service(
        [
            (
                "GROUP BY val",
                FakeQueryResult(result_rows=[["GET", 60, 100, 5], ["POST", 30, 100, 5]]),
            ),
        ]
    )
    result = svc.field_terms(EventQuery(case_id="c1", source_ids=["s1"]), "artifact")
    assert result["total"] == 100
    assert result["distinct"] == 5
    assert result["values"] == [{"value": "GET", "count": 60}, {"value": "POST", "count": 30}]
    assert result["other_count"] == 10


def test_field_terms_on_timestamp_column_casts_to_string() -> None:
    """`timestamp` is a `DateTime64` top-level column, not `String` — the
    generated SQL must cast it before comparing/grouping, or ClickHouse
    raises a type error on `col != ''`."""
    svc = _viz_service(
        [
            ("GROUP BY val", FakeQueryResult(result_rows=[["2024-01-01 00:00:00.000", 1, 10, 10]])),
        ]
    )
    result = svc.field_terms(EventQuery(case_id="c1", source_ids=["s1"]), "timestamp")
    assert result["total"] == 10
    queries = [q for q, _ in svc.store.client.queries]  # type: ignore[union-attr]
    assert any("toString(timestamp)" in q for q in queries)
    assert not any("AND timestamp != ''" in q for q in queries)


def test_field_terms_empty_dataset_returns_zero_totals() -> None:
    svc = _viz_service([("GROUP BY val", FakeQueryResult(result_rows=[]))])
    result = svc.field_terms(EventQuery(case_id="c1", source_ids=["s1"]), "artifact")
    assert result == {
        "field": "artifact",
        "total": 0,
        "distinct": 0,
        "values": [],
        "other_count": 0,
    }
    # Fused single-scan design: an empty dataset still costs exactly one query.
    assert len(svc.store.client.queries) == 1  # type: ignore[union-attr]


def test_field_terms_top_level_column_uses_bare_column() -> None:
    svc = _viz_service(
        [
            ("GROUP BY val", FakeQueryResult(result_rows=[["auth", 1, 1, 1]])),
        ]
    )
    svc.field_terms(EventQuery(case_id="c1", source_ids=["s1"]), "artifact")
    query, _ = svc.store.client.queries[0]  # type: ignore[union-attr]
    assert "artifact AS val" in query
    assert "attributes[" not in query


def test_field_terms_attribute_field_uses_map_lookup() -> None:
    svc = _viz_service(
        [
            ("GROUP BY val", FakeQueryResult(result_rows=[["200", 1, 1, 1]])),
        ]
    )
    svc.field_terms(EventQuery(case_id="c1", source_ids=["s1"]), "attr:status_code")
    query, params = svc.store.client.queries[0]  # type: ignore[union-attr]
    assert "attributes[{field_key:String}]" in query
    assert params is not None
    assert params.get("field_key") == "status_code"


def test_field_terms_honors_field_filters() -> None:
    """field_terms must reuse _build_where so it respects the same filters as the grid."""
    svc = _viz_service(
        [
            ("GROUP BY val", FakeQueryResult(result_rows=[["ok", 1, 1, 1]])),
        ]
    )
    svc.field_terms(
        EventQuery(case_id="c1", source_ids=["s1"], field_filters={"artifact": "auth"}),
        "artifact",
    )
    query, params = svc.store.client.queries[0]  # type: ignore[union-attr]
    assert "artifact = {p2:String}" in query
    assert params is not None
    assert params.get("p2") == "auth"


def test_field_numeric_stats_returns_stats_and_fixed_width_bins() -> None:
    svc = _viz_service(
        [
            (
                "stddevPop(v)",
                FakeQueryResult(
                    result_rows=[
                        [10, 0.0, 100.0, 50.0, 10.0, 1.0, 5.0, 25.0, 50.0, 75.0, 95.0, 99.0]
                    ]
                ),
            ),
            (
                "toUInt32(floor(",
                FakeQueryResult(result_rows=[[0, 5], [1, 5]]),
            ),
        ]
    )
    result = svc.field_numeric_stats(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:bytes_sent", bins=2
    )
    assert result["count"] == 10
    assert result["min"] == 0.0
    assert result["max"] == 100.0
    assert result["mean"] == 50.0
    assert result["stddev"] == 10.0
    assert result["quantiles"]["0.5"] == 50.0
    assert result["quantiles"]["0.99"] == 99.0
    assert len(result["bins"]) == 2
    assert result["bins"][0] == {"x0": 0.0, "x1": 50.0, "count": 5}
    assert result["bins"][1] == {"x0": 50.0, "x1": 100.0, "count": 5}


def test_field_numeric_stats_fills_empty_bins_with_zero() -> None:
    svc = _viz_service(
        [
            (
                "stddevPop(v)",
                FakeQueryResult(
                    result_rows=[[4, 0.0, 40.0, 20.0, 5.0, 0.0, 0.0, 10.0, 20.0, 30.0, 38.0, 39.0]]
                ),
            ),
            # Only bin 0 has data — bins 1-3 must still appear, count 0.
            ("toUInt32(floor(", FakeQueryResult(result_rows=[[0, 4]])),
        ]
    )
    result = svc.field_numeric_stats(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:latency_ms", bins=4
    )
    assert [b["count"] for b in result["bins"]] == [4, 0, 0, 0]


def test_field_numeric_stats_non_numeric_field_returns_zero_count() -> None:
    """count == 0 signals the caller to fall back to categorical treatment."""
    svc = _viz_service(
        [("stddevPop(v)", FakeQueryResult(result_rows=[[0, None, None, None, None]]))]
    )
    result = svc.field_numeric_stats(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:user_agent", bins=10
    )
    assert result == {
        "field": "attr:user_agent",
        "count": 0,
        "min": None,
        "max": None,
        "mean": None,
        "stddev": None,
        "quantiles": {},
        "bins": [],
    }
    # No histogram bin query should have been attempted for a non-numeric field.
    assert not any("toUInt32(floor(" in q for q, _ in svc.store.client.queries)  # type: ignore[union-attr]


def test_field_value_timeseries_pivots_series_with_zero_fill() -> None:
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    bucket1 = min_ts
    bucket2 = min_ts + timedelta(hours=12)
    svc = _viz_service(
        [
            ("min(timestamp)", FakeQueryResult(result_rows=[[min_ts, max_ts]])),
            ("GROUP BY val", FakeQueryResult(result_rows=[["a", 2, 3, 2], ["b", 1, 3, 2]])),
            (
                "toStartOfInterval",
                FakeQueryResult(
                    result_rows=[
                        [bucket1, "a", 2],
                        [bucket1, "b", 1],
                        [bucket2, "a", 1],
                    ]
                ),
            ),
        ]
    )
    result = svc.field_value_timeseries(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:status_code", buckets=2, series_limit=12
    )
    assert result["interval_seconds"] > 0
    series_by_value = {s["value"]: s for s in result["series"]}
    assert set(series_by_value) == {"a", "b"}
    # "b" has no row for bucket2 — must be zero-filled, not omitted.
    b_counts = {b["start"]: b["count"] for b in series_by_value["b"]["buckets"]}
    assert len(b_counts) == 2
    assert sum(b_counts.values()) == 1


def test_field_value_timeseries_zero_fills_buckets_with_no_top_value_events() -> None:
    """A bucket where *none* of the top-N values fired must still appear,
    zero-filled — not be silently dropped from every series."""
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    bucket1 = min_ts
    bucket4 = min_ts + timedelta(hours=18)
    svc = _viz_service(
        [
            ("min(timestamp)", FakeQueryResult(result_rows=[[min_ts, max_ts]])),
            ("GROUP BY val", FakeQueryResult(result_rows=[["a", 2, 2, 1]])),
            (
                "toStartOfInterval",
                # Only bucket1 and bucket4 have rows — bucket2 (06:00) and
                # bucket3 (12:00) had zero matching events entirely and are
                # absent from the GROUP BY result.
                FakeQueryResult(result_rows=[[bucket1, "a", 1], [bucket4, "a", 1]]),
            ),
        ]
    )
    result = svc.field_value_timeseries(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:status_code", buckets=4, series_limit=12
    )
    series_a = next(s for s in result["series"] if s["value"] == "a")
    starts = [b["start"] for b in series_a["buckets"]]
    assert len(starts) == 4
    counts = {b["start"]: b["count"] for b in series_a["buckets"]}
    assert sum(counts.values()) == 2
    assert sorted(counts.values()) == [0, 0, 1, 1]


def test_field_value_timeseries_empty_range_returns_empty_series() -> None:
    svc = _viz_service([("min(timestamp)", FakeQueryResult(result_rows=[[None, None]]))])
    result = svc.field_value_timeseries(
        EventQuery(case_id="c1", source_ids=["s1"]), "artifact", buckets=60
    )
    assert result["series"] == []
    assert result["min"] is None


def test_field_value_timeseries_no_values_returns_empty_series_without_bucket_query() -> None:
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    svc = _viz_service(
        [
            ("min(timestamp)", FakeQueryResult(result_rows=[[min_ts, max_ts]])),
            ("GROUP BY val", FakeQueryResult(result_rows=[])),
        ]
    )
    result = svc.field_value_timeseries(
        EventQuery(case_id="c1", source_ids=["s1"]), "artifact", buckets=60
    )
    assert result["series"] == []
    assert result["min"] == min_ts.isoformat()


# ── compare aggregation tests (compare_time_histogram / compare_field_terms / compare_field_numeric) ─


class _SeqFakeClient:
    """Like _VizFakeClient, but each marker maps to a FIFO of results.

    The compare_* methods run the *same query shape once per layer* (primary
    then comparison), so a single canned result per marker cannot tell the
    layers apart — a FIFO can.
    """

    def __init__(self, responses: list[tuple[str, list[FakeQueryResult]]]) -> None:
        self._responses = responses
        self.queries: list[tuple[str, dict[str, Any] | None]] = []

    def query(
        self, query: str, parameters: dict[str, Any] | None = None, **_kwargs: Any
    ) -> FakeQueryResult:
        self.queries.append((query, parameters))
        for substr, queue in self._responses:
            if substr in query and queue:
                return queue.pop(0)
        raise AssertionError(f"No fake response left for query:\n{query}")


def _seq_service(responses: list[tuple[str, list[FakeQueryResult]]]) -> EventQueryService:
    store = FakeClickHouseStore()
    store.client = _SeqFakeClient(responses)  # type: ignore[assignment]
    return EventQueryService(store=store)


def test_compare_time_histogram_shared_grid_zero_fill_and_no_range_query() -> None:
    """Explicit start/end → no range query; both layers land on identical
    bucket starts with zero-fill, comparison layer's missing buckets = 0."""
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 1, 2, tzinfo=UTC)
    b0 = min_ts
    b1 = min_ts + timedelta(hours=1)
    svc = _seq_service(
        [
            (
                "toStartOfInterval",
                [
                    FakeQueryResult(result_rows=[[b0, 5]]),  # primary
                    FakeQueryResult(result_rows=[[b0, 50], [b1, 40]]),  # comparison
                ],
            ),
        ]
    )
    primary = EventQuery(case_id="c1", source_ids=["s1"], q="dos", start=min_ts, end=max_ts)
    comparison = EventQuery(case_id="c1", source_ids=["s1"], start=min_ts, end=max_ts)
    result = svc.compare_time_histogram(primary, comparison, buckets=2)

    assert not any("min(timestamp)" in q for q, _ in svc.store.client.queries)  # type: ignore[union-attr]
    assert result["interval_seconds"] == 3600
    assert result["primary_total"] == 5
    assert result["comparison_total"] == 90
    assert len(result["buckets"]) == 2
    # Primary has no row for b1 — zero-filled on the shared grid, not dropped.
    assert result["buckets"][0]["primary"] == 5
    assert result["buckets"][0]["comparison"] == 50
    assert result["buckets"][1]["primary"] == 0
    assert result["buckets"][1]["comparison"] == 40


def test_compare_time_histogram_uses_union_of_layer_ranges() -> None:
    """Without an explicit window, the grid spans the union of both layers'
    data ranges — neither layer's buckets get truncated to the other's."""
    p_min = datetime(2024, 1, 1, 6, tzinfo=UTC)
    p_max = datetime(2024, 1, 1, 12, tzinfo=UTC)
    c_min = datetime(2024, 1, 1, 0, tzinfo=UTC)
    c_max = datetime(2024, 1, 1, 18, tzinfo=UTC)
    svc = _seq_service(
        [
            (
                "min(timestamp)",
                [
                    FakeQueryResult(result_rows=[[p_min, p_max]]),
                    FakeQueryResult(result_rows=[[c_min, c_max]]),
                ],
            ),
            (
                "toStartOfInterval",
                [FakeQueryResult(result_rows=[]), FakeQueryResult(result_rows=[])],
            ),
        ]
    )
    result = svc.compare_time_histogram(
        EventQuery(case_id="c1", source_ids=["s1"], q="dos"),
        EventQuery(case_id="c1", source_ids=["s1"]),
        buckets=18,
    )
    assert result["min"] == c_min.isoformat()
    assert result["max"] == c_max.isoformat()
    range_queries = [q for q, _ in svc.store.client.queries if "min(timestamp)" in q]  # type: ignore[union-attr]
    assert len(range_queries) == 2


def test_compare_time_histogram_empty_dataset() -> None:
    svc = _seq_service(
        [
            (
                "min(timestamp)",
                [
                    FakeQueryResult(result_rows=[[None, None]]),
                    FakeQueryResult(result_rows=[[None, None]]),
                ],
            ),
        ]
    )
    result = svc.compare_time_histogram(
        EventQuery(case_id="c1", source_ids=["s1"]),
        EventQuery(case_id="c1", source_ids=["s1"]),
    )
    assert result["buckets"] == []
    assert result["primary_total"] == 0
    assert result["comparison_total"] == 0


def test_compare_field_terms_shares_primary_categories() -> None:
    """Primary's top-N fixes the category list; comparison is counted against
    those same values, its tail folding into comparison_other."""
    svc = _seq_service(
        [
            # Primary field_terms (window-agg shape: val, c, total, n_groups).
            (
                "OVER ()",
                [FakeQueryResult(result_rows=[["GET", 60, 100, 5], ["POST", 30, 100, 5]])],
            ),
            # Comparison layer folded onto the shared values: '' = its tail.
            (
                "cmp_values",
                [FakeQueryResult(result_rows=[["GET", 600], ["", 400]])],
            ),
        ]
    )
    result = svc.compare_field_terms(
        EventQuery(case_id="c1", source_ids=["s1"], q="dos"),
        EventQuery(case_id="c1", source_ids=["s1"]),
        "attr:method",
    )
    assert [v["value"] for v in result["values"]] == ["GET", "POST"]
    assert result["values"][0] == {"value": "GET", "primary": 60, "comparison": 600}
    # POST absent from comparison rows — zero, not dropped.
    assert result["values"][1] == {"value": "POST", "primary": 30, "comparison": 0}
    assert result["primary_total"] == 100
    assert result["comparison_total"] == 1000
    assert result["primary_other"] == 10
    assert result["comparison_other"] == 400


def test_compare_field_terms_empty_primary_skips_comparison_query() -> None:
    svc = _seq_service([("OVER ()", [FakeQueryResult(result_rows=[])])])
    result = svc.compare_field_terms(
        EventQuery(case_id="c1", source_ids=["s1"]),
        EventQuery(case_id="c1", source_ids=["s1"]),
        "attr:method",
    )
    assert result["values"] == []
    assert result["comparison_total"] == 0
    assert not any("cmp_values" in q for q, _ in svc.store.client.queries)  # type: ignore[union-attr]


def test_compare_field_numeric_shared_edges_from_union_min_max() -> None:
    """Bin edges come from the union min/max of both layers, and both layers
    are bucketed on those identical edges."""
    svc = _seq_service(
        [
            (
                "count(v), min(v), max(v)",
                [
                    FakeQueryResult(result_rows=[[10, 20.0, 60.0]]),  # primary
                    FakeQueryResult(result_rows=[[100, 0.0, 100.0]]),  # comparison
                ],
            ),
            (
                "greatest(0, least(",
                [
                    FakeQueryResult(result_rows=[[0, 4], [1, 6]]),  # primary
                    FakeQueryResult(result_rows=[[0, 50], [1, 50]]),  # comparison
                ],
            ),
        ]
    )
    result = svc.compare_field_numeric(
        EventQuery(case_id="c1", source_ids=["s1"], q="dos"),
        EventQuery(case_id="c1", source_ids=["s1"]),
        "attr:bytes",
        bins=2,
    )
    assert result["min"] == 0.0
    assert result["max"] == 100.0
    assert result["bins"][0] == {"x0": 0.0, "x1": 50.0, "primary": 4, "comparison": 50}
    assert result["bins"][1] == {"x0": 50.0, "x1": 100.0, "primary": 6, "comparison": 50}
    # Both bin queries were parameterized with the same shared edges.
    bin_params = [p for q, p in svc.store.client.queries if "greatest(0, least(" in q]  # type: ignore[union-attr]
    assert all(p["mn"] == 0.0 and p["bw"] == 50.0 for p in bin_params)


def test_compare_field_numeric_no_numeric_values_returns_empty() -> None:
    svc = _seq_service(
        [
            (
                "count(v), min(v), max(v)",
                [
                    FakeQueryResult(result_rows=[[0, None, None]]),
                    FakeQueryResult(result_rows=[[0, None, None]]),
                ],
            ),
        ]
    )
    result = svc.compare_field_numeric(
        EventQuery(case_id="c1", source_ids=["s1"]),
        EventQuery(case_id="c1", source_ids=["s1"]),
        "attr:user_agent",
    )
    assert result["bins"] == []
    assert result["min"] is None
    assert not any("toStartOfInterval" in q for q, _ in svc.store.client.queries)  # type: ignore[union-attr]
