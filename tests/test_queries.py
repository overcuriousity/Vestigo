"""Tests for the ClickHouse event query builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import pytest

from tracesignal.db._dt import NULL_TS_SENTINEL, TS_NOT_SENTINEL_SQL
from tracesignal.db.queries import (
    EventQuery,
    EventQueryService,
    TagFilter,
    _normalize_event_row,
)


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
        ]

    def query(
        self,
        query: str,
        parameters: dict[str, Any] | None = None,
        **_kwargs: Any,
    ) -> FakeQueryResult:
        self.queries.append((query, parameters))
        stripped = query.strip()
        if stripped.startswith("SELECT count()"):
            return FakeQueryResult(result_rows=[[0]])
        if stripped.startswith("SELECT event_id, timestamp"):
            # Phase 1 of the two-phase page fetch: thin sort-key rows.
            id_idx = self.event_columns.index("event_id")
            ts_idx = self.event_columns.index("timestamp")
            return FakeQueryResult(
                result_rows=[(row[id_idx], row[ts_idx]) for row in self.event_rows],
                column_names=["event_id", "timestamp"],
            )
        return FakeQueryResult(
            result_rows=self.event_rows,
            column_names=self.event_columns,
        )


class FakeClickHouseStore:
    """Minimal ClickHouseStore stand-in."""

    def __init__(self, event_rows: list[list[Any]] | None = None) -> None:
        self.database = "tracesignal"
        self.client = FakeClickHouseClient(event_rows)
        self.schema_initialized = False
        # M22 fast path off by default: existing SQL-shape tests pin the
        # pre-blob search SQL byte-for-byte.
        self._search_blob_ready = False

    def init_schema(self) -> None:
        self.schema_initialized = True

    def search_blob_ready(self) -> bool:
        return self._search_blob_ready


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
    """Multiple source_ids use a typed `IN {p:Array(String)}` — source_id is a
    String column, and the bare-column IN form keeps ClickHouse able to use
    the primary index and partition pruning, which wrapping the column in
    toString() defeats (M22a). Only the UUID event_id column needs the
    has(..., toString(col)) cast form.
    """
    service.query(EventQuery(case_id="case-1", source_ids=["s1", "s2"]))
    query, params = _last_query(service)
    assert "source_id IN {p1:Array(String)}" in query
    assert "toString(source_id)" not in query
    assert params.get("p1") == ["s1", "s2"]


def test_empty_source_ids_filter_matches_nothing(service: EventQueryService) -> None:
    """An explicit empty source_ids list must produce a valid always-false
    predicate (`source_id IN []`), not stale syntax or a dropped filter."""
    service.query(EventQuery(case_id="case-1", source_ids=[]))
    query, params = _last_query(service)
    assert "source_id IN {p1:Array(String)}" in query
    assert params.get("p1") == []


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


def test_regex_search_uses_match_not_ilike(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1", q="^Login (failed|succeeded)$", q_regex=True))
    query, params = _last_query(service)
    assert "match(message, {p1:String})" in query
    assert "arrayExists(v -> match(v, {p1:String}), tags)" in query
    assert "arrayExists(v -> match(v, {p1:String}), mapValues(attributes))" in query
    assert "ILIKE" not in query
    # Pattern is bound raw: no % wrapping, no LIKE-metacharacter escaping.
    assert params.get("p1") == "^Login (failed|succeeded)$"


def test_regex_search_does_not_like_escape_pattern(service: EventQueryService) -> None:
    """Regex metacharacters that overlap LIKE syntax (% _ \\) must reach
    ClickHouse untouched — LIKE-escaping them would corrupt the pattern."""
    service.query(EventQuery(case_id="case-1", q=r"\d+%_", q_regex=True))
    _, params = _last_query(service)
    assert params.get("p1") == r"\d+%_"


def test_keyword_search_unaffected_by_regex_flag_default(service: EventQueryService) -> None:
    service.query(EventQuery(case_id="case-1", q="100%_done"))
    query, params = _last_query(service)
    assert "message ILIKE {p1:String}" in query
    assert "match(" not in query
    assert params.get("p1") == "%100\\%\\_done%"


def test_text_search_fast_path_prepends_blob_prefilter() -> None:
    """M22: with search_blob ready, the blob LIKE pre-filter wraps the OR-chain."""
    store = FakeClickHouseStore()
    store._search_blob_ready = True
    service = EventQueryService(store=store)
    service.query(EventQuery(case_id="case-1", q="login"))
    query, params = _last_query(service)
    assert "(search_blob LIKE lowerUTF8({p1:String}) AND (" in query
    # The full OR-chain survives unchanged as the source of truth.
    assert "message ILIKE {p1:String}" in query
    assert "arrayExists(v -> v ILIKE {p1:String}, mapValues(attributes))" in query
    # One shared bound parameter for both predicates.
    assert params.get("p1") == "%login%"
    assert "p2" not in params or "%login%" not in str(params.get("p2"))


def test_text_search_fast_path_off_sql_unchanged() -> None:
    """Fast path off: search SQL is byte-identical to the pre-blob form."""
    store = FakeClickHouseStore()
    service = EventQueryService(store=store)
    service.query(EventQuery(case_id="case-1", q="login"))
    query, _ = _last_query(service)
    assert "search_blob" not in query
    assert (
        "(message ILIKE {p1:String} OR display_name ILIKE {p1:String} OR "
        "artifact ILIKE {p1:String} OR artifact_long ILIKE {p1:String} OR "
        "timestamp_desc ILIKE {p1:String} OR source_file ILIKE {p1:String} OR "
        "arrayExists(v -> v ILIKE {p1:String}, tags) OR "
        "arrayExists(v -> v ILIKE {p1:String}, mapValues(attributes)))"
    ) in query


def test_regex_search_never_uses_blob_fast_path() -> None:
    """Regex search stays a full scan even when the blob is ready."""
    store = FakeClickHouseStore()
    store._search_blob_ready = True
    service = EventQueryService(store=store)
    service.query(EventQuery(case_id="case-1", q="^root$", q_regex=True))
    query, _ = _last_query(service)
    assert "search_blob" not in query
    assert "match(message, {p1:String})" in query


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
    assert query.count("artifact = ") + query.count("artifact IN ") == 1
    assert "artifact IN {p1:Array(String)}" in query
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


def test_time_range_filter_excludes_sentinel_rows(service: EventQueryService) -> None:
    # A time-range filter must never match sentinel (no-timestamp) rows —
    # mirrors how NULL rows failed any >=/<= comparison on the old Nullable
    # column.
    service.query(EventQuery(case_id="case-1", start=datetime(2024, 1, 1, tzinfo=UTC)))
    query, _ = _last_query(service)
    assert TS_NOT_SENTINEL_SQL in query


def test_no_time_range_no_sentinel_guard(service: EventQueryService) -> None:
    # Without a time filter, sentinel rows are regular grid citizens.
    service.query(EventQuery(case_id="case-1"))
    query, _ = _last_query(service)
    assert TS_NOT_SENTINEL_SQL not in query


def test_normalize_event_row_presents_sentinel_timestamp_as_null() -> None:
    sentinel_naive = NULL_TS_SENTINEL.replace(tzinfo=None)
    row = {
        "event_id": "e1",
        "timestamp": sentinel_naive,
        "ingest_time": datetime(2024, 1, 1, 12, 0, 0),
    }
    normalized = _normalize_event_row(row)
    assert normalized["timestamp"] is None
    # ingest_time is always real — never nulled, just UTC-ISO'd.
    assert normalized["ingest_time"] == "2024-01-01T12:00:00+00:00"


# ---------------------------------------------------------------------------
# W2 — per-source clock-skew correction (query time)
# ---------------------------------------------------------------------------


def test_zero_offset_map_keeps_sql_byte_identical(service: EventQueryService) -> None:
    """An all-zero/empty offset map must not change the generated SQL at all."""
    service.query(EventQuery(case_id="case-1", source_ids=["s1"], source_offsets={"s1": 0}))
    query, params = _last_query(service)
    # Fast path: bare column in ORDER BY, no offset expression or params.
    assert "ORDER BY timestamp DESC" in query
    assert "addSeconds" not in query
    assert "transform(source_id" not in query
    assert not any(k.startswith("clk_off") for k in (params or {}))


def test_active_offset_orders_by_corrected_timestamp(service: EventQueryService) -> None:
    service.query(
        EventQuery(case_id="case-1", source_ids=["s1", "s2"], source_offsets={"s2": 3600})
    )
    query, params = _last_query(service)
    # Ordering (and the cursor, when present) run on the corrected timestamp.
    assert "addSeconds(timestamp, transform(source_id" in query
    assert "ORDER BY if(" in query
    assert params["clk_off_src"] == ["s2"]
    assert params["clk_off_val"] == [3600]
    # The sentinel is never shifted — the correction is gated on it.
    assert TS_NOT_SENTINEL_SQL in query


def test_active_offset_time_filter_widens_raw_bound(service: EventQueryService) -> None:
    """The corrected time filter carries a widened raw-column bound for pruning."""
    start = datetime(2024, 1, 1, 12, 0, 0, tzinfo=UTC)
    end = datetime(2024, 1, 2, 12, 0, 0, tzinfo=UTC)
    service.query(
        EventQuery(
            case_id="case-1",
            source_ids=["s1"],
            source_offsets={"s1": 3600},
            start=start,
            end=end,
        )
    )
    query, params = _last_query(service)
    # Corrected-ts predicate on the effective expression.
    assert "if(" in query and ">= {p" in query
    # Widened raw bounds: lower = start - max_off (1h earlier), upper =
    # end - min_off (min_off = 0 here, so unchanged).
    assert "2024-01-01 11:00:00" in params.values()
    assert "2024-01-02 12:00:00" in params.values()


def test_normalize_event_row_applies_source_offset() -> None:
    row = {
        "event_id": "e1",
        "source_id": "s2",
        "timestamp": datetime(2024, 1, 1, 12, 0, 0),
        "ingest_time": datetime(2024, 1, 1, 12, 0, 0),
    }
    normalized = _normalize_event_row(row, {"s2": 3600})
    # Event time shifts +1h; ingest_time (real wall-clock metadata) does not.
    assert normalized["timestamp"] == "2024-01-01T13:00:00+00:00"
    assert normalized["ingest_time"] == "2024-01-01T12:00:00+00:00"


def test_normalize_event_row_offset_skips_sentinel() -> None:
    sentinel_naive = NULL_TS_SENTINEL.replace(tzinfo=None)
    row = {"event_id": "e1", "source_id": "s2", "timestamp": sentinel_naive}
    normalized = _normalize_event_row(row, {"s2": 3600})
    # A shifted sentinel would leak a fake date — must stay null.
    assert normalized["timestamp"] is None


def test_normalize_event_row_offset_only_its_source() -> None:
    row = {
        "event_id": "e1",
        "source_id": "s1",
        "timestamp": datetime(2024, 1, 1, 12, 0, 0),
    }
    # s1 has no offset in the map — untouched.
    normalized = _normalize_event_row(row, {"s2": 3600})
    assert normalized["timestamp"] == "2024-01-01T12:00:00+00:00"


def test_histogram_buckets_over_corrected_timestamp() -> None:
    svc = EventQueryService(store=FakeClickHouseStore())
    svc.histogram(
        EventQuery(
            case_id="case-1",
            source_ids=["s1"],
            source_offsets={"s1": 3600},
            start=datetime(2024, 1, 1, tzinfo=UTC),
            end=datetime(2024, 1, 2, tzinfo=UTC),
        )
    )
    query, _ = svc.store.client.queries[-1]
    assert "toStartOfInterval(if(" in query
    assert "addSeconds(timestamp, transform(source_id" in query


def test_top_level_field_filter_uses_column(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"display_name": ["auth.log"]},
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
            field_filters={"ip_address_city": ["Falkenstein"]},
        )
    )
    query, params = _last_query(service)
    assert "attributes[{p1:String}] = {p2:String}" in query
    assert params.get("p1") == "ip_address_city"
    assert params.get("p2") == "Falkenstein"


def test_field_filter_multiple_values_ors_with_in(service: EventQueryService) -> None:
    """Two values under one field key OR together via IN (src_port 22 OR 23)."""
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"attr:src_port": ["22", "23"]},
        )
    )
    query, params = _last_query(service)
    assert "attributes[{p1:String}] IN {p2:Array(String)}" in query
    assert params.get("p2") == ["22", "23"]


def test_field_filter_multiple_wildcard_values_or_clauses(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"src_ip": ["10.0.*", "192.168.*"]},
            filter_modes={"src_ip": "wildcard"},
        )
    )
    query, params = _last_query(service)
    assert "ILIKE {p2:String} OR" in query
    assert params.get("p2") == "10.0.%"
    assert params.get("p3") == "192.168.%"


def test_field_filters_across_keys_are_anded(service: EventQueryService) -> None:
    """Multiple values within a key OR; distinct keys AND."""
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"attr:src_port": ["22", "23"], "display_name": ["auth.log"]},
        )
    )
    query, params = _last_query(service)
    assert "IN {p2:Array(String)}" in query
    assert "display_name = {p3:String}" in query
    assert " AND " in query


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


def test_field_filter_wildcard_translates_glob(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"src_ip": ["10.0.*"]},
            filter_modes={"src_ip": "wildcard"},
        )
    )
    query, params = _last_query(service)
    assert "attributes[{p1:String}] ILIKE {p2:String}" in query
    assert params.get("p2") == "10.0.%"
    assert "10.0.*" not in query


def test_field_filter_wildcard_escapes_like_metachars(service: EventQueryService) -> None:
    """Literal % and _ in the value stay literal; * and ? become LIKE wildcards."""
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"msg": ["100%_a?*"]},
            filter_modes={"msg": "wildcard"},
        )
    )
    _, params = _last_query(service)
    assert params.get("p2") == "100\\%\\_a_%"


def test_field_filter_regex_uses_match(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"src_ip": [r"^10\.0\."]},
            filter_modes={"src_ip": "regex"},
        )
    )
    query, params = _last_query(service)
    assert "match(attributes[{p1:String}], {p2:String})" in query
    assert params.get("p2") == r"^10\.0\."


def test_field_filter_wildcard_casts_non_string_column(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"timestamp": ["2024*"]},
            filter_modes={"timestamp": "wildcard"},
        )
    )
    query, _ = _last_query(service)
    assert "toString(timestamp) ILIKE {p1:String}" in query


def test_field_exclusion_wildcard_negates_or_of_patterns(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_exclusions={"src_ip": ["10.0.*", "192.168.*"]},
            exclusion_modes={"src_ip": "wildcard"},
        )
    )
    query, params = _last_query(service)
    assert (
        "NOT (attributes[{p1:String}] ILIKE {p2:String} "
        "OR attributes[{p1:String}] ILIKE {p3:String})" in query
    )
    assert params.get("p2") == "10.0.%"
    assert params.get("p3") == "192.168.%"


def test_field_exclusion_regex_negates_or_of_patterns(service: EventQueryService) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_exclusions={"src_ip": [r"^10\."]},
            exclusion_modes={"src_ip": "regex"},
        )
    )
    query, params = _last_query(service)
    assert "NOT (match(attributes[{p1:String}], {p2:String}))" in query
    assert params.get("p2") == r"^10\."


def test_field_filter_pattern_modes_are_not_interpolated(service: EventQueryService) -> None:
    injection = "'; DROP TABLE events; --"
    for mode in ("wildcard", "regex"):
        service.query(
            EventQuery(
                case_id="case-1",
                field_filters={"msg": [injection]},
                filter_modes={"msg": mode},
            )
        )
        query, _ = _last_query(service)
        assert "DROP TABLE" not in query


def test_invalid_match_mode_raises(service: EventQueryService) -> None:
    with pytest.raises(ValueError, match="invalid match mode"):
        service.query(
            EventQuery(
                case_id="case-1",
                field_filters={"msg": ["x"]},
                filter_modes={"msg": "glob"},
            )
        )
    with pytest.raises(ValueError, match="invalid match mode"):
        service.query(
            EventQuery(
                case_id="case-1",
                field_exclusions={"msg": ["x"]},
                exclusion_modes={"msg": "like"},
            )
        )


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
            field_filters={"ip_address_city": ["Falkenstein"]},
            field_exclusions={"status_code": ["200"]},
        )
    )
    count_query, count_params = _find_query(service, "SELECT count()")
    assert "case_id = {p0:String}" in count_query
    # Single-element source list becomes equality — a fixed sort-key prefix
    # is what lets ClickHouse read in order for ORDER BY ... LIMIT pages.
    assert "source_id = {p1:String}" in count_query
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
    assert "(timestamp, event_id) < ({p1:DateTime64(3)}, {p2:UUID})" in query
    # Redundant scalar bound: only a scalar sort-key comparison prunes
    # primary-index granules; the tuple form alone re-reads the partition.
    assert "AND timestamp <= {p1:DateTime64(3)}" in query
    assert params["p1"] == "2026-06-25 07:30:01.000"
    assert params["p2"] == "evt-1"
    assert "OFFSET" not in query


def test_before_cursor_uses_gt_predicate_and_reversed_fetch_direction(
    service: EventQueryService,
) -> None:
    ts = datetime(2026, 6, 25, 7, 30, 1)
    service.query(EventQuery(case_id="case-1", before=(ts, "evt-1")))
    query, _ = _last_query(service)
    assert "(timestamp, event_id) > ({p1:DateTime64(3)}, {p2:UUID})" in query
    assert "AND timestamp >= {p1:DateTime64(3)}" in query
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


def test_cursor_substitutes_sentinel_for_undated_row() -> None:
    """An undated row (stored as the year-2299 sentinel, presented as null)
    at a page boundary must never produce a `None` cursor component —
    `[null, id]` serializes to JSON and is not a parseable
    "<iso-ts>,<event_id>" string on the way back in (400)."""
    row = _cursor_row("evt-null", NULL_TS_SENTINEL.replace(tzinfo=None))
    svc = EventQueryService(store=FakeClickHouseStore(event_rows=[row]))
    page = svc.query(EventQuery(case_id="case-1"))
    assert page.events[0]["timestamp"] is None
    assert page.prev_cursor == ("2299-12-31T23:59:59.999000+00:00", "evt-null")
    assert page.next_cursor == ("2299-12-31T23:59:59.999000+00:00", "evt-null")


def test_cursor_predicate_is_sargable_plain_tuple(
    service: EventQueryService,
) -> None:
    """No `coalesce()` wrapper on the cursor predicate: the column stores the
    year-2299 sentinel instead of NULL, so the plain tuple comparison is both
    correct for undated rows and usable by the primary index — the coalesce
    form defeated granule pruning entirely."""
    ts = datetime(2026, 6, 25, 7, 30, 1)
    service.query(EventQuery(case_id="case-1", after=(ts, "evt-1")))
    query, params = _last_query(service)
    assert "coalesce(" not in query
    assert "(timestamp, event_id) < ({p1:DateTime64(3)}, {p2:UUID})" in query
    assert "p3" not in (params or {})


def test_two_phase_page_fetch_shapes() -> None:
    """Phase 1 must select only (event_id, timestamp); phase 2 must re-filter
    by the page's timestamp bounds plus an explicit event_id set, and return
    rows in phase-1 order."""
    rows = [
        _cursor_row("evt-1", datetime(2026, 6, 25, 7, 30, 1)),
        _cursor_row("evt-2", datetime(2026, 6, 25, 7, 30, 2)),
    ]
    svc = EventQueryService(store=FakeClickHouseStore(event_rows=rows))
    page = svc.query(
        EventQuery(case_id="case-1", limit=2, after=(datetime(2026, 6, 25, 7, 29, 0), "evt-0"))
    )
    queries = [q for q, _ in svc.store.client.queries]
    phase1 = [q for q in queries if q.strip().startswith("SELECT event_id, timestamp")]
    assert len(phase1) == 1
    assert "ORDER BY timestamp" in phase1[0]
    hydrate = [q for q in queries if "event_id IN (" in q]
    assert len(hydrate) == 1
    assert "timestamp >= {hts_min:DateTime64(3)}" in hydrate[0]
    assert "timestamp <= {hts_max:DateTime64(3)}" in hydrate[0]
    # The fat columns are only ever selected in the bounded hydration query.
    assert "message" not in phase1[0]
    assert "message" in hydrate[0]
    _, hydrate_params = next((q, p) for q, p in svc.store.client.queries if "event_id IN (" in q)
    assert hydrate_params["hid0"] == "evt-1"
    assert hydrate_params["hid1"] == "evt-2"
    assert [e["event_id"] for e in page.events] == ["evt-1", "evt-2"]


def test_hydration_reorders_rows_to_phase1_order() -> None:
    """The hydration SELECT has no ORDER BY — row order must be restored from
    the phase-1 key order."""

    class _ShuffledClient(FakeClickHouseClient):
        def query(self, query: str, parameters=None, **kwargs: Any) -> FakeQueryResult:
            result = super().query(query, parameters, **kwargs)
            if "event_id IN (" in query:
                result.result_rows = list(reversed(result.result_rows))
            return result

    rows = [
        _cursor_row("evt-1", datetime(2026, 6, 25, 7, 30, 2)),
        _cursor_row("evt-2", datetime(2026, 6, 25, 7, 30, 1)),
    ]
    store = FakeClickHouseStore()
    store.client = _ShuffledClient(rows)
    svc = EventQueryService(store=store)
    page = svc.query(
        EventQuery(case_id="case-1", limit=2, after=(datetime(2026, 6, 25, 7, 31, 0), "evt-0"))
    )
    assert [e["event_id"] for e in page.events] == ["evt-1", "evt-2"]


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
    from tracesignal.db.queries import TOP_LEVEL_DISPLAY_COLUMNS

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
        if "intDiv(toUnixTimestamp(timestamp)" in stripped:
            # Combined single-round-trip histogram: bucket rows carry the
            # server-computed interval and range alongside each bucket.
            iv = max(1, int((self._max - self._min).total_seconds() // 60))
            rows = [[b, c, iv, self._min, self._max] for b, c in self._buckets]
            return FakeQueryResult(
                result_rows=rows,
                column_names=["bucket", "c", "interval_seconds", "min_ts", "max_ts"],
            )
        if "toStartOfInterval" in stripped:
            return FakeQueryResult(result_rows=self._buckets, column_names=["bucket", "c"])
        if "min(timestamp)" in stripped:
            return FakeQueryResult(
                result_rows=[[self._min, self._max]], column_names=["min", "max"]
            )
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
    assert result["min"] == min_ts.isoformat()
    assert result["max"] == max_ts.isoformat()
    # M22(c): derived-range histogram must be a single ClickHouse round trip —
    # no separate min/max range query before the bucket scan.
    assert len(svc.store.client.queries) == 1  # type: ignore[union-attr]


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


def test_histogram_queries_exclude_sentinel_rows() -> None:
    # Both histogram branches must exclude no-timestamp sentinel rows — the
    # derived-range CTE especially, where a sentinel would blow up
    # max(timestamp) and stretch every bucket interval to ~275 years.
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    svc = _histogram_service(min_ts, max_ts, [[min_ts, 1]])
    svc.histogram(EventQuery(case_id="c1", source_ids=["s1"]), buckets=60)
    derived_sql = svc.store.client.queries[-1][0]  # type: ignore[union-attr]
    assert derived_sql.count(TS_NOT_SENTINEL_SQL) == 2  # CTE range + bucket scan

    svc2 = _histogram_service(min_ts, max_ts, [[min_ts, 1]])
    svc2.histogram(
        EventQuery(case_id="c1", source_ids=["s1"], start=min_ts, end=max_ts), buckets=60
    )
    explicit_sql = svc2.store.client.queries[-1][0]  # type: ignore[union-attr]
    assert TS_NOT_SENTINEL_SQL in explicit_sql


def test_histogram_empty_dataset_returns_empty_buckets() -> None:
    """Client returns None timestamps → empty bucket list."""
    store = FakeClickHouseStore()

    class _EmptyClient:
        queries: list = []

        def query(self, query: str, parameters: Any = None, **_: Any) -> FakeQueryResult:
            self.queries.append(query)
            if "intDiv(toUnixTimestamp(timestamp)" in query:
                # Combined query: no matching rows → zero result rows.
                return FakeQueryResult(result_rows=[], column_names=["bucket", "c"])
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
        EventQuery(case_id="c1", source_ids=["s1"], field_filters={"artifact": ["auth"]}),
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
                "toInt64(floor(",
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
            ("toInt64(floor(", FakeQueryResult(result_rows=[[0, 4]])),
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
    assert not any("toInt64(floor(" in q for q, _ in svc.store.client.queries)  # type: ignore[union-attr]


def test_field_value_timeseries_pivots_series_with_zero_fill() -> None:
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    bucket1 = min_ts
    bucket2 = min_ts + timedelta(hours=12)
    svc = _viz_service(
        [
            ("min(timestamp)", FakeQueryResult(result_rows=[[min_ts, max_ts]])),
            (
                "groupArrayIf",
                FakeQueryResult(
                    result_rows=[
                        ["a", 3, [(bucket1, 2), (bucket2, 1)]],
                        ["b", 1, [(bucket1, 1)]],
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
    # "b" has no row for bucket2 — must be zero-filled, not omitted. The grid
    # also carries the trailing bucket containing max_ts (Jan 2 00:00).
    b_counts = {b["start"]: b["count"] for b in series_by_value["b"]["buckets"]}
    assert len(b_counts) == 3
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
            (
                "groupArrayIf",
                # Only bucket1 and bucket4 have rows — bucket2 (06:00) and
                # bucket3 (12:00) had zero matching events entirely and are
                # absent from the plotted buckets.
                FakeQueryResult(result_rows=[["a", 2, [(bucket1, 1), (bucket4, 1)]]]),
            ),
        ]
    )
    result = svc.field_value_timeseries(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:status_code", buckets=4, series_limit=12
    )
    series_a = next(s for s in result["series"] if s["value"] == "a")
    starts = [b["start"] for b in series_a["buckets"]]
    # 4 requested buckets plus the trailing one containing max_ts.
    assert len(starts) == 5
    counts = {b["start"]: b["count"] for b in series_a["buckets"]}
    assert sum(counts.values()) == 2
    assert sorted(counts.values()) == [0, 0, 0, 1, 1]


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
    # Three grid buckets: the trailing one contains max_ts itself (02:00) —
    # empty here, but it must exist so events in a partial trailing bucket
    # are never dropped from the chart.
    assert len(result["buckets"]) == 3
    # Primary has no row for b1 — zero-filled on the shared grid, not dropped.
    assert result["buckets"][0]["primary"] == 5
    assert result["buckets"][0]["comparison"] == 50
    assert result["buckets"][1]["primary"] == 0
    assert result["buckets"][1]["comparison"] == 40
    assert result["buckets"][2] == {"start": max_ts.isoformat(), "primary": 0, "comparison": 0}


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


def test_guard_encoder_degrades_on_failure() -> None:
    """A failing encoder is caught, disabled after first error, returns []."""
    from tracesignal.db.queries import _guard_encoder

    calls = {"n": 0}

    def boom(_texts: list[str]) -> list[list[float]]:
        calls["n"] += 1
        raise RuntimeError("401 Unauthorized")

    guarded = _guard_encoder(boom)
    assert guarded is not None
    assert guarded(["a"]) == []  # first call fails, swallowed
    assert guarded(["b"]) == []  # short-circuits, no second remote call
    assert calls["n"] == 1


def test_guard_encoder_passes_through_success_and_none() -> None:
    from tracesignal.db.queries import _guard_encoder

    assert _guard_encoder(None) is None

    def ok(texts: list[str]) -> list[list[float]]:
        return [[1.0, 0.0] for _ in texts]

    guarded = _guard_encoder(ok)
    assert guarded is not None
    assert guarded(["a", "b"]) == [[1.0, 0.0], [1.0, 0.0]]


def test_embedding_wizard_scans_carry_memory_settings(service):
    """Both wizard scans (artifact inventory reading the attributes map, and
    the randomized sample) are whole-corpus queries and must carry the shared
    heavy-scan SETTINGS clause (see db/_scan.py)."""
    from tracesignal.db._scan import HEAVY_SCAN_SETTINGS

    service.list_fields_by_artifact("case", ["s1"], encode=None)
    queries = [q for q, _ in service.store.client.queries]
    scans = [q for q in queries if "GROUP BY artifact" in q or "_rn" in q]
    assert len(scans) == 2
    for q in scans:
        assert HEAVY_SCAN_SETTINGS in q


# ── heavy-scan guardrails + clock-skew on viz aggregations ──────────────────


def test_viz_aggregations_carry_memory_settings() -> None:
    """Every viz aggregation scan must carry the shared heavy-scan SETTINGS
    clause (db/_scan.py) — a chart over a high-cardinality field is a
    whole-corpus GROUP BY exactly like a detector scan, and without the
    per-query memory cap N concurrent charts can stack unbounded scans."""
    from tracesignal.db._scan import HEAVY_SCAN_SETTINGS

    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    q = EventQuery(case_id="c1", source_ids=["s1"])

    terms_row = FakeQueryResult(result_rows=[["a", 1, 1, 1]])
    stats_row = FakeQueryResult(
        result_rows=[[4, 0.0, 40.0, 20.0, 5.0, 0.0, 0.0, 10.0, 20.0, 30.0, 38.0, 39.0]]
    )

    services = {
        "histogram": _viz_service([("intDiv(toUnixTimestamp", FakeQueryResult(result_rows=[]))]),
        "histogram_explicit": _viz_service(
            [("toStartOfInterval", FakeQueryResult(result_rows=[]))]
        ),
        "field_terms": _viz_service([("GROUP BY val", terms_row)]),
        "field_numeric_stats": _viz_service(
            [
                ("stddevPop(v)", stats_row),
                ("toInt64(floor(", FakeQueryResult(result_rows=[[0, 4]])),
            ]
        ),
        "field_value_timeseries": _viz_service(
            [
                ("min(timestamp)", FakeQueryResult(result_rows=[[min_ts, max_ts]])),
                ("groupArrayIf", FakeQueryResult(result_rows=[["a", 1, [(min_ts, 1)]]])),
            ]
        ),
    }
    services["histogram"].histogram(q)
    services["histogram_explicit"].histogram(
        EventQuery(case_id="c1", source_ids=["s1"], start=min_ts, end=max_ts)
    )
    services["field_terms"].field_terms(q, "artifact")
    services["field_numeric_stats"].field_numeric_stats(q, "attr:latency_ms", bins=2)
    services["field_value_timeseries"].field_value_timeseries(q, "attr:status_code", buckets=4)

    for name, svc in services.items():
        queries = [sql for sql, _ in svc.store.client.queries]  # type: ignore[union-attr]
        assert queries, name
        for sql in queries:
            assert HEAVY_SCAN_SETTINGS in sql, f"{name} scan missing settings:\n{sql}"


def test_viz_compare_aggregations_carry_memory_settings() -> None:
    from tracesignal.db._scan import HEAVY_SCAN_SETTINGS

    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 1, 2, tzinfo=UTC)
    p = EventQuery(case_id="c1", source_ids=["s1"], q="dos", start=min_ts, end=max_ts)
    c = EventQuery(case_id="c1", source_ids=["s1"], start=min_ts, end=max_ts)

    svc_time = _seq_service(
        [
            (
                "toStartOfInterval",
                [FakeQueryResult(result_rows=[]), FakeQueryResult(result_rows=[])],
            ),
        ]
    )
    svc_time.compare_time_histogram(p, c, buckets=2)

    svc_terms = _seq_service(
        [
            ("OVER ()", [FakeQueryResult(result_rows=[["GET", 6, 10, 2]])]),
            ("cmp_values", [FakeQueryResult(result_rows=[["GET", 3]])]),
        ]
    )
    svc_terms.compare_field_terms(p, c, "attr:method")

    svc_numeric = _seq_service(
        [
            (
                "count(v), min(v), max(v)",
                [
                    FakeQueryResult(result_rows=[[5, 0.0, 10.0]]),
                    FakeQueryResult(result_rows=[[5, 0.0, 10.0]]),
                ],
            ),
            (
                "toInt64(floor(",
                [FakeQueryResult(result_rows=[[0, 5]]), FakeQueryResult(result_rows=[[0, 5]])],
            ),
        ]
    )
    svc_numeric.compare_field_numeric(p, c, "attr:bytes", bins=2)

    for name, svc in (("time", svc_time), ("terms", svc_terms), ("numeric", svc_numeric)):
        queries = [sql for sql, _ in svc.store.client.queries]  # type: ignore[union-attr]
        assert queries, name
        for sql in queries:
            assert HEAVY_SCAN_SETTINGS in sql, f"compare {name} scan missing settings:\n{sql}"


class _CountingGate:
    """Context-manager stand-in for HEAVY_SCAN_GATE that detects re-entry."""

    def __init__(self) -> None:
        self.acquired = 0
        self.depth = 0
        self.max_depth = 0

    def __enter__(self) -> None:
        self.acquired += 1
        self.depth += 1
        self.max_depth = max(self.max_depth, self.depth)

    def __exit__(self, *exc: Any) -> None:
        self.depth -= 1


def test_viz_public_aggregations_acquire_scan_gate_once(monkeypatch: Any) -> None:
    """Public viz aggregations acquire the admission gate exactly once — the
    nested field_terms call inside field_value_timeseries / compare_field_terms
    must go through the ungated impl, or a BoundedSemaphore(1) deployment
    (TS_STAT_SCAN_CONCURRENCY=1) would deadlock against itself."""
    import tracesignal.db.queries as queries_mod

    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)

    gate = _CountingGate()
    monkeypatch.setattr(queries_mod, "HEAVY_SCAN_GATE", gate)
    svc = _viz_service(
        [
            ("min(timestamp)", FakeQueryResult(result_rows=[[min_ts, max_ts]])),
            ("groupArrayIf", FakeQueryResult(result_rows=[["a", 1, [(min_ts, 1)]]])),
        ]
    )
    svc.field_value_timeseries(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:status_code", buckets=4
    )
    assert gate.acquired == 1
    assert gate.max_depth == 1

    gate2 = _CountingGate()
    monkeypatch.setattr(queries_mod, "HEAVY_SCAN_GATE", gate2)
    svc2 = _seq_service(
        [
            ("OVER ()", [FakeQueryResult(result_rows=[["GET", 6, 10, 2]])]),
            ("cmp_values", [FakeQueryResult(result_rows=[["GET", 3]])]),
        ]
    )
    svc2.compare_field_terms(
        EventQuery(case_id="c1", source_ids=["s1"], q="dos"),
        EventQuery(case_id="c1", source_ids=["s1"]),
        "attr:method",
    )
    assert gate2.acquired == 1
    assert gate2.max_depth == 1


def test_field_value_timeseries_honors_source_offsets() -> None:
    """W2: the value×time chart must range and bucket on the offset-corrected
    timestamp like `histogram` does, or the two charts bucket the same filtered
    view on different timelines whenever a source is skew-corrected."""
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    svc = _viz_service(
        [
            ("min(if(", FakeQueryResult(result_rows=[[min_ts, max_ts]])),
            ("groupArrayIf", FakeQueryResult(result_rows=[])),
        ]
    )
    svc.field_value_timeseries(
        EventQuery(case_id="c1", source_ids=["s1"], source_offsets={"s1": 3600}),
        "attr:status_code",
        buckets=4,
    )
    queries = [sql for sql, _ in svc.store.client.queries]  # type: ignore[union-attr]
    range_q = next(sql for sql in queries if "min(if(" in sql)
    bucket_q = next(sql for sql in queries if "toStartOfInterval" in sql)
    assert "addSeconds(timestamp, transform(source_id" in range_q
    assert "toStartOfInterval(if(" in bucket_q
    assert "addSeconds(timestamp, transform(source_id" in bucket_q


def test_compare_time_histogram_honors_source_offsets() -> None:
    """W2: both compare layers bucket (and, without an explicit window, range)
    on their own offset-corrected timestamps."""
    p_min = datetime(2024, 1, 1, tzinfo=UTC)
    p_max = datetime(2024, 1, 1, 6, tzinfo=UTC)
    svc = _seq_service(
        [
            (
                "min(if(",
                [
                    FakeQueryResult(result_rows=[[p_min, p_max]]),
                    FakeQueryResult(result_rows=[[p_min, p_max]]),
                ],
            ),
            (
                "toStartOfInterval",
                [FakeQueryResult(result_rows=[]), FakeQueryResult(result_rows=[])],
            ),
        ]
    )
    offsets = {"s1": 3600}
    svc.compare_time_histogram(
        EventQuery(case_id="c1", source_ids=["s1"], q="dos", source_offsets=offsets),
        EventQuery(case_id="c1", source_ids=["s1"], source_offsets=offsets),
        buckets=6,
    )
    queries = [sql for sql, _ in svc.store.client.queries]  # type: ignore[union-attr]
    range_qs = [sql for sql in queries if "min(if(" in sql]
    bucket_qs = [sql for sql in queries if "toStartOfInterval" in sql]
    assert len(range_qs) == 2
    assert len(bucket_qs) == 2
    for sql in bucket_qs:
        assert "toStartOfInterval(if(" in sql


# ── time_punchcard / field_pivot / field_scatter ─────────────────────────────


def test_time_punchcard_returns_sparse_cells_and_totals() -> None:
    svc = _viz_service(
        [
            (
                "toDayOfWeek",
                FakeQueryResult(result_rows=[[1, 9, 40], [1, 10, 60], [6, 3, 7]]),
            ),
        ]
    )
    result = svc.time_punchcard(EventQuery(case_id="c1", source_ids=["s1"]))
    assert result["kind"] == "punchcard"
    assert result["total"] == 107
    assert result["max_count"] == 60
    assert result["cells"] == [
        {"dow": 1, "hour": 9, "count": 40},
        {"dow": 1, "hour": 10, "count": 60},
        {"dow": 6, "hour": 3, "count": 7},
    ]
    sql, _ = svc.store.client.queries[0]  # type: ignore[union-attr]
    # Day/hour extraction pinned to UTC — server timezone must never reshape
    # the punch card — and sentinel (undated) rows excluded.
    assert "toDayOfWeek(timestamp, 0, 'UTC')" in sql
    assert "toHour(timestamp, 'UTC')" in sql
    assert TS_NOT_SENTINEL_SQL in sql
    from tracesignal.db._scan import HEAVY_SCAN_SETTINGS

    assert HEAVY_SCAN_SETTINGS in sql


def test_time_punchcard_honors_source_offsets() -> None:
    svc = _viz_service([("toDayOfWeek", FakeQueryResult(result_rows=[]))])
    svc.time_punchcard(EventQuery(case_id="c1", source_ids=["s1"], source_offsets={"s1": 3600}))
    sql, _ = svc.store.client.queries[0]  # type: ignore[union-attr]
    assert "addSeconds(timestamp, transform(source_id" in sql


def test_field_pivot_builds_matrix_with_other_rollup() -> None:
    svc = _viz_service(
        [
            # Terms scan for field_x = artifact (bare column expr).
            ("artifact AS val", FakeQueryResult(result_rows=[["auth", 60, 100, 3]])),
            # Terms scan for field_y = attr:status (map lookup expr).
            (
                "attributes[{field_key:String}] AS val",
                FakeQueryResult(result_rows=[["200", 80, 100, 4]]),
            ),
            # Matrix scan: '' cells are the per-axis Other rollups.
            (
                "GROUP BY xv, yv",
                FakeQueryResult(result_rows=[["auth", "200", 50], ["", "200", 9], ["auth", "", 5]]),
            ),
        ]
    )
    result = svc.field_pivot(
        EventQuery(case_id="c1", source_ids=["s1"]), "artifact", "attr:status", limit_x=5, limit_y=5
    )
    assert result["kind"] == "pivot"
    assert result["x_values"] == ["auth"]
    assert result["y_values"] == ["200"]
    assert result["x_distinct"] == 3
    assert result["y_distinct"] == 4
    assert result["total"] == 64
    assert {"x": "", "y": "200", "count": 9} in result["cells"]

    matrix_sql, matrix_params = next(
        (sql, p)
        for sql, p in svc.store.client.queries  # type: ignore[union-attr]
        if "GROUP BY xv, yv" in sql
    )
    # Both axes bind their own field-key params (never a shared name) and
    # their own top-N value lists.
    assert matrix_params is not None
    assert matrix_params.get("field_key_y") == "status"
    assert matrix_params.get("pivot_x_values") == ["auth"]
    assert matrix_params.get("pivot_y_values") == ["200"]
    from tracesignal.db._scan import HEAVY_SCAN_SETTINGS

    assert HEAVY_SCAN_SETTINGS in matrix_sql


def test_field_pivot_empty_axis_skips_matrix_scan() -> None:
    svc = _viz_service(
        [
            ("artifact AS val", FakeQueryResult(result_rows=[["auth", 60, 100, 3]])),
            ("attributes[{field_key:String}] AS val", FakeQueryResult(result_rows=[])),
        ]
    )
    result = svc.field_pivot(EventQuery(case_id="c1", source_ids=["s1"]), "artifact", "attr:status")
    assert result["cells"] == []
    assert result["total"] == 0
    assert not any(
        "GROUP BY xv, yv" in sql
        for sql, _ in svc.store.client.queries  # type: ignore[union-attr]
    )


def test_field_pivot_acquires_scan_gate_once(monkeypatch: Any) -> None:
    """The two parallel terms scans run under the parent's single gate slot."""
    import tracesignal.db.queries as queries_mod

    gate = _CountingGate()
    monkeypatch.setattr(queries_mod, "HEAVY_SCAN_GATE", gate)
    svc = _viz_service(
        [
            ("artifact AS val", FakeQueryResult(result_rows=[["auth", 60, 100, 3]])),
            (
                "attributes[{field_key:String}] AS val",
                FakeQueryResult(result_rows=[["200", 80, 100, 4]]),
            ),
            ("GROUP BY xv, yv", FakeQueryResult(result_rows=[["auth", "200", 50]])),
        ]
    )
    svc.field_pivot(EventQuery(case_id="c1", source_ids=["s1"]), "artifact", "attr:status")
    assert gate.acquired == 1
    assert gate.max_depth == 1


def test_field_scatter_samples_points_with_true_extents() -> None:
    svc = _viz_service(
        [
            ("min(vx)", FakeQueryResult(result_rows=[[120000, 0.0, 1000.0, -5.0, 99.0]])),
            (
                "ORDER BY rand()",
                FakeQueryResult(result_rows=[[10.0, 1.0], [500.0, 42.0]]),
            ),
        ]
    )
    result = svc.field_scatter(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:bytes", "attr:latency", limit=2
    )
    assert result["kind"] == "scatter"
    assert result["total"] == 120000
    assert result["sampled"] == 2
    # Extents come from the full-data stats scan, not the sample.
    assert (result["x_min"], result["x_max"]) == (0.0, 1000.0)
    assert (result["y_min"], result["y_max"]) == (-5.0, 99.0)
    assert result["points"] == [[10.0, 1.0], [500.0, 42.0]]

    sample_sql = next(
        sql
        for sql, _ in svc.store.client.queries  # type: ignore[union-attr]
        if "ORDER BY rand()" in sql
    )
    assert "LIMIT 2" in sample_sql
    from tracesignal.db._scan import HEAVY_SCAN_SETTINGS

    assert HEAVY_SCAN_SETTINGS in sample_sql
    assert "toFloat64OrNull(toString(" in sample_sql


def test_field_scatter_non_numeric_skips_sample_scan() -> None:
    """total == 0 (no numeric pairs) → no second scan, categorical fallback signal."""
    svc = _viz_service([("min(vx)", FakeQueryResult(result_rows=[[0, None, None, None, None]]))])
    result = svc.field_scatter(
        EventQuery(case_id="c1", source_ids=["s1"]), "attr:user_agent", "attr:bytes"
    )
    assert result["total"] == 0
    assert result["points"] == []
    assert result["x_min"] is None
    assert not any(
        "ORDER BY rand()" in sql
        for sql, _ in svc.store.client.queries  # type: ignore[union-attr]
    )


# ── baseline-compare layer cache (M24c) ──────────────────────────────────────


def _fresh_viz_cache():
    from tracesignal.db import viz_cache

    viz_cache.reset_baseline_cache()
    return viz_cache


_TOKEN = ("c1", (("s1", "2026-01-01T00:00:00+00:00", 10),))


def test_compare_time_histogram_baseline_cache_warm_render_scans_primary_only() -> None:
    _fresh_viz_cache()
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    svc = _seq_service(
        [
            ("min(", [FakeQueryResult(result_rows=[[min_ts, max_ts]])] * 2),
            ("toStartOfInterval", [FakeQueryResult(result_rows=[])] * 6),
        ]
    )
    p = EventQuery(case_id="c1", source_ids=["s1"], q="dos")
    c = EventQuery(case_id="c1", source_ids=["s1"])

    first = svc.compare_time_histogram(p, c, buckets=4, baseline_cache_token=_TOKEN)
    # Cold with token: baseline range + 2 bucket scans — the primary range
    # scan is skipped outright (union == baseline range in baseline mode).
    assert len(svc.store.client.queries) == 3

    second = svc.compare_time_histogram(p, c, buckets=4, baseline_cache_token=_TOKEN)
    # Warm: only the primary's bucket scan runs.
    assert len(svc.store.client.queries) == 4
    assert second == first

    # A changed freshness token re-scans the baseline layer.
    other = ("c1", (("s1", "2026-02-01T00:00:00+00:00", 20),))
    svc.compare_time_histogram(p, c, buckets=4, baseline_cache_token=other)
    assert len(svc.store.client.queries) == 7


def test_compare_time_histogram_without_token_stays_fully_live() -> None:
    _fresh_viz_cache()
    min_ts = datetime(2024, 1, 1, tzinfo=UTC)
    max_ts = datetime(2024, 1, 2, tzinfo=UTC)
    svc = _seq_service(
        [
            ("min(", [FakeQueryResult(result_rows=[[min_ts, max_ts]])] * 4),
            ("toStartOfInterval", [FakeQueryResult(result_rows=[])] * 4),
        ]
    )
    p = EventQuery(case_id="c1", source_ids=["s1"], q="dos")
    c = EventQuery(case_id="c1", source_ids=["s1"])
    svc.compare_time_histogram(p, c, buckets=4)
    svc.compare_time_histogram(p, c, buckets=4)
    # 2 range + 2 bucket scans per render, nothing cached.
    assert len(svc.store.client.queries) == 8


def test_compare_field_terms_baseline_cache_warm_render() -> None:
    _fresh_viz_cache()
    svc = _seq_service(
        [
            ("OVER ()", [FakeQueryResult(result_rows=[["GET", 6, 10, 2]])] * 2),
            # One canned comparison scan only: a warm second render must not
            # consult this FIFO again (it would raise "no fake response left").
            ("cmp_values", [FakeQueryResult(result_rows=[["GET", 3]])]),
        ]
    )
    p = EventQuery(case_id="c1", source_ids=["s1"], q="dos")
    c = EventQuery(case_id="c1", source_ids=["s1"])
    first = svc.compare_field_terms(p, c, "attr:method", baseline_cache_token=_TOKEN)
    second = svc.compare_field_terms(p, c, "attr:method", baseline_cache_token=_TOKEN)
    assert second == first
    assert first["values"] == [{"value": "GET", "primary": 6, "comparison": 3}]


def test_compare_field_numeric_baseline_cache_warm_render() -> None:
    _fresh_viz_cache()
    stats = FakeQueryResult(result_rows=[[5, 0.0, 10.0]])
    bins = FakeQueryResult(result_rows=[[0, 5]])
    svc = _seq_service(
        [
            # Cold render consumes two of each (both layers, identical rows so
            # _run_parallel interleaving can't skew the assertion); warm render
            # consumes one of each (primary only).
            ("count(v), min(v), max(v)", [stats] * 3),
            ("toInt64(floor(", [bins] * 3),
        ]
    )
    p = EventQuery(case_id="c1", source_ids=["s1"], q="dos")
    c = EventQuery(case_id="c1", source_ids=["s1"])
    first = svc.compare_field_numeric(p, c, "attr:bytes", bins=2, baseline_cache_token=_TOKEN)
    assert len(svc.store.client.queries) == 4
    second = svc.compare_field_numeric(p, c, "attr:bytes", bins=2, baseline_cache_token=_TOKEN)
    assert len(svc.store.client.queries) == 6
    assert second == first
