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
