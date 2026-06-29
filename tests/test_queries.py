"""Tests for the ClickHouse event query builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from tracevector.db.queries import EventQuery, EventQueryService


@dataclass
class FakeQueryResult:
    """Minimal QueryResult stand-in for clickhouse-connect."""

    result_rows: list[list[Any]] | None = None
    column_names: list[str] | None = None


class FakeClickHouseClient:
    """Records queries and parameters, returns canned results."""

    def __init__(self) -> None:
        self.queries: list[tuple[str, dict[str, Any] | None]] = []
        self.event_columns = [
            "event_id",
            "case_id",
            "timeline_id",
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
            "source",
            "source_long",
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
            result_rows=[],
            column_names=self.event_columns,
        )


class FakeClickHouseStore:
    """Minimal ClickHouseStore stand-in."""

    def __init__(self) -> None:
        self.database = "tracevector"
        self.client = FakeClickHouseClient()
        self.schema_initialized = False

    def init_schema(self) -> None:
        self.schema_initialized = True


@pytest.fixture
def service() -> EventQueryService:
    return EventQueryService(store=FakeClickHouseStore())


def _last_query(service: EventQueryService) -> tuple[str, dict[str, Any] | None]:
    return service.store.client.queries[-1]


def _find_query(
    service: EventQueryService, prefix: str
) -> tuple[str, dict[str, Any] | None]:
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


def test_timeline_filter_is_parameterized(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1", timeline_id="tl-1"))
    query, params = _last_query(service)
    assert "timeline_id = {p1:String}" in query
    assert params.get("p1") == "tl-1"


def test_text_search_uses_parameterized_like(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1", q="login"))
    query, params = _last_query(service)
    assert "message ILIKE {p1:String}" in query
    assert params.get("p1") == "%login%"


def test_source_and_tag_filters_are_parameterized(
    service: EventQueryService,
) -> None:
    service.query(EventQuery(case_id="case-1", source="auth", tag="success"))
    query, params = _last_query(service)
    assert "source = {p1:String}" in query
    assert "has(tags, {p2:String})" in query
    assert params.get("p1") == "auth"
    assert params.get("p2") == "success"


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
    service.query(
        EventQuery(
            case_id="case-1",
            field_exclusions={"display_name": "auth.log"},
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
            timeline_id="tl-1",
            q="token",
            source="auth",
            field_filters={"ip_address_city": "Falkenstein"},
            field_exclusions={"status_code": "200"},
        )
    )
    count_query, count_params = _find_query(service, "SELECT count()")
    assert "case_id = {p0:String}" in count_query
    assert "timeline_id = {p1:String}" in count_query
    assert "message ILIKE {p2:String}" in count_query
    assert "source = {p3:String}" in count_query
    assert "attributes[{p4:String}] = {p5:String}" in count_query
    assert "attributes[{p6:String}] != {p7:String}" in count_query
    assert count_params is not None
    assert set(count_params.keys()) == {f"p{i}" for i in range(8)}


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
        return [[f"evt-{self._select_call}-{i}"] + ["x"] * (len(self.columns) - 1) for i in range(n)]

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
    "event_id", "case_id", "timeline_id", "source_file", "byte_offset",
    "line_number", "content_hash", "parser_name", "parser_version", "ingest_time",
    "message", "timestamp", "timestamp_desc", "source", "source_long",
    "display_name", "tags", "attributes", "embedding_model", "embedding_config_hash",
    "vector_id",
]


def _batched_service(batch_size: int, full_batches: int = 1, remainder: int = 0) -> EventQueryService:
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


def test_iter_events_where_clause_is_parameterized() -> None:
    """Filter values must never appear as raw SQL — injection is impossible."""
    injection = "'; DROP TABLE events; --"
    svc = _batched_service(batch_size=5, full_batches=0, remainder=0)
    list(svc.iter_events(EventQuery(case_id=injection), batch_size=5))
    select_queries = [
        q for q, _ in svc.store.client.queries  # type: ignore[union-attr]
        if not q.strip().startswith("SELECT count()")
    ]
    assert select_queries, "Expected at least one SELECT query"
    for sql in select_queries:
        assert injection not in sql
        assert "DROP TABLE" not in sql


def test_iter_events_applies_filters_in_where_clause() -> None:
    svc = _batched_service(batch_size=5, full_batches=0, remainder=0)
    list(svc.iter_events(
        EventQuery(case_id="c1", source="auth", tag="malware"),
        batch_size=5,
    ))
    select_queries = [
        (q, p) for q, p in svc.store.client.queries  # type: ignore[union-attr]
        if not q.strip().startswith("SELECT count()")
    ]
    assert select_queries
    sql, params = select_queries[0]
    assert "source = {p1:String}" in sql
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
        q for q, _ in svc.store.client.queries  # type: ignore[union-attr]
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
    result = svc.list_fields("c1", "tl1")
    assert result["attributes"] == ["alpha", "middle", "zebra"]


def test_list_fields_returns_top_level_columns() -> None:
    from tracevector.db.queries import TOP_LEVEL_DISPLAY_COLUMNS
    svc = _fields_service([])
    result = svc.list_fields("c1", "tl1")
    assert result["top_level"] == TOP_LEVEL_DISPLAY_COLUMNS


def test_list_fields_empty_dataset() -> None:
    svc = _fields_service([])
    result = svc.list_fields("c1", "tl1")
    assert result["attributes"] == []


# ── histogram tests ────────────────────────────────────────────────────────────

from datetime import UTC, timedelta  # noqa: E402 (already imported at top, repeated for clarity)


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
            return FakeQueryResult(result_rows=[[self._min, self._max]], column_names=["min", "max"])
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
    result = svc.histogram(EventQuery(case_id="c1", timeline_id="tl1"), buckets=60)
    assert result["interval_seconds"] > 0
    assert len(result["buckets"]) == 3
    assert result["buckets"][1]["count"] == 20


def test_histogram_respects_explicit_time_range() -> None:
    """When start/end are provided, no range-query should be issued."""
    min_ts = datetime(2024, 3, 1, tzinfo=UTC)
    max_ts = datetime(2024, 3, 2, tzinfo=UTC)
    svc = _histogram_service(min_ts, max_ts, [[min_ts, 7]])
    eq = EventQuery(case_id="c1", timeline_id="tl1", start=min_ts, end=max_ts)
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
    result = svc.histogram(EventQuery(case_id="c1", timeline_id="tl1"))
    assert result["buckets"] == []
    assert result["min"] is None
