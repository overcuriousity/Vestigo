"""Tests for StatisticalAnomalyService.

All tests use fakes/mocks for ClickHouse so they run without external services.
"""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from vestigo.db._offsets import OFFSET_SRC_PARAM, OFFSET_VAL_PARAM
from vestigo.db.anomaly_stats import (
    _CHARSET_GROUP_PROBE_LIMIT,
    _HYDRATE_CHUNK,
    _MAX_CHARSET_GROUPED_ROWS,
    _MAX_CHARSET_SIZE,
    _SQL_SUPPRESSION_MAX,
    AnalysisWindows,
    FreqFinding,
    NoveltyFieldInfo,
    SelfSlices,
    StatAnomalyResult,
    StatisticalAnomalyService,
    TimeWindow,
    ValueFinding,
    _bh_qvalues,
    _chi2_sf,
    _chi2_sf_df1,
    _classify_field,
    _col_expr,
    _full_bucket_starts,
    _g_statistic,
    _g_statistic_k,
    _gamma_from_median,
    _gamma_sf,
    _greenwood_p,
    _poisson_rate_g,
    _robust_cv,
    _scalar_total,
    _sidak_p,
    _spend_ks_budget,
    _sql_suppression,
    _tvd,
    _window_preds,
    effective_ts_sql,
)

#: Column shape of the grouped ``_alphabet_sql`` — one row per (group, character).
_ALPHABET_COLUMNS = ["grp", "c", "n_vals_with_c", "n_vals"]


def _alphabet_rows(*groups):
    """Grouped ``_alphabet_sql`` rows from ``(grp, charset, n_vals)`` triples.

    The per-character count is immaterial to a baseline-window reference (it
    scores by membership), so every character gets 1.
    """
    return [(grp, c, 1, n_vals) for grp, charset, n_vals in groups for c in charset]


def _assert_paired(page_sql: str, total_sql: str, *fragments: str) -> None:
    """Assert the page and its companion count carry the same filters.

    ``_page_and_total`` composes both statements from one *core*, and this is
    the property it exists for: a total describing a different filter than the
    page it annotates is worse than no total. The old single-statement shape
    asserted a ``count() OVER ()`` inside the page; there is no window any
    more, so the pairing is what a shape test has to check.
    """
    assert " AS scanned" in total_sql, "no companion count statement ran"
    for frag in fragments:
        assert frag in page_sql, f"missing from page: {frag}"
        assert frag in total_sql, f"missing from total: {frag}"


def _one_suspect(
    baseline_start: datetime,
    baseline_end: datetime,
    suspect_start: datetime,
    suspect_end: datetime,
    label: str = "suspect",
) -> AnalysisWindows:
    """Build an AnalysisWindows with one baseline and one suspect window."""
    return AnalysisWindows(
        baseline=TimeWindow("baseline", baseline_start, baseline_end),
        suspects=(TimeWindow(label, suspect_start, suspect_end),),
    )


# ---------------------------------------------------------------------------
# Fake ClickHouse infrastructure
# ---------------------------------------------------------------------------


@dataclass
class FakeQueryResult:
    result_rows: list[tuple]
    column_names: list[str]


def _is_totals_sql(sql: str) -> bool:
    """True for the companion count statement StatisticalAnomalyService runs.

    ``_page_and_total`` composes both statements from one *core*: the page is
    ``core + page_tail``, the total is ``SELECT ... FROM (core) AS scanned``.
    The alias is what tells them apart, and it has to: the two run in parallel
    threads, so nothing about arrival order is stable.
    """
    return ") AS scanned" in sql


class FakeClient:
    """Minimal ClickHouse client fake driven by pre-seeded results.

    Page and aggregate answers are consumed FIFO from *responses*. The
    companion totals statement (see :func:`_is_totals_sql`) is answered from
    its own *totals* seed — one entry per scan, in scan order — because it
    neither arrives at a fixed position in the FIFO nor wants a row shape. An
    entry is an int for the usual one-cell count, or a FakeQueryResult for a
    grouped total (the batched attribute pass counts per key). A scan with no
    totals seed left reports a total of 0.

    ``_calls``/``_all_parameters``/``full_queries`` record the non-totals
    queries only, so index-based assertions read the same as they did when a
    scan was one statement; the totals statements are recorded separately in
    ``total_queries``/``total_parameters``.
    """

    def __init__(
        self,
        responses: list[FakeQueryResult],
        totals: list[int | FakeQueryResult] | None = None,
    ) -> None:
        # Responses are consumed in order (FIFO) for each query call.
        self._responses: list[FakeQueryResult] = list(responses)
        self._totals: list[int | FakeQueryResult] = list(totals or [])
        self._calls: list[str] = []
        self._all_parameters: list[dict] = []
        self.total_queries: list[str] = []
        self.total_parameters: list[dict] = []
        # The page and its total run on two threads against this one client.
        self._lock = threading.Lock()

    def query(self, sql: str, parameters: dict | None = None) -> FakeQueryResult:
        with self._lock:
            if _is_totals_sql(sql):
                self.total_queries.append(sql)
                self.total_parameters.append(parameters or {})
                if self._totals:
                    seed = self._totals.pop(0)
                    if isinstance(seed, FakeQueryResult):
                        return seed
                    return FakeQueryResult(result_rows=[(seed,)], column_names=["n_total"])
                return FakeQueryResult(result_rows=[], column_names=[])
            self._record(sql)
            self._calls.append(sql.strip().split("\n")[0].strip())
            self._all_parameters.append(parameters or {})
            if self._responses:
                return self._responses.pop(0)
            return FakeQueryResult(result_rows=[], column_names=[])

    def _record(self, sql: str) -> None:
        """Hook for subclasses that keep the full SQL text."""


class RecordingClient(FakeClient):
    """FakeClient that also captures the full SQL text of every query.

    Used by the temporal-mode tests to assert on the baseline/detect clauses.
    ``full_queries`` holds the page statements; the companion totals go to
    ``total_queries``, so a test can assert a filter reached *both*.
    """

    def __init__(
        self,
        responses: list[FakeQueryResult],
        totals: list[int | FakeQueryResult] | None = None,
    ) -> None:
        super().__init__(responses, totals)
        self.full_queries: list[str] = []

    def _record(self, sql: str) -> None:
        self.full_queries.append(sql)


class FakeClickHouseStore:
    """Minimal ClickHouseStore wrapper using FakeClient."""

    def __init__(self, client: FakeClient) -> None:
        self.client = client
        self.database = "vestigo"
        # Seedable hydration source for get_events_by_ids; calls are recorded
        # so tests can assert hydration is one batched fetch.
        self.events_by_id: dict[str, dict] = {}
        self.hydration_calls: list[list[str]] = []

    def init_schema(self) -> None:
        pass

    def get_events_by_ids(
        self, case_id: str, source_ids: list[str], event_ids: list[str]
    ) -> dict[str, dict]:
        self.hydration_calls.append(list(event_ids))
        return {i: self.events_by_id[i] for i in event_ids if i in self.events_by_id}


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _svc(
    responses: list[FakeQueryResult], totals: list[int | FakeQueryResult] | None = None
) -> StatisticalAnomalyService:
    """Build a service backed by a FakeClient with canned responses.

    *totals* seeds the companion count statements in scan order.
    """
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(FakeClient(responses, totals))
    return svc


# ---------------------------------------------------------------------------
# Totals contract: exact counts, SQL-side suppression, chunked hydration
# ---------------------------------------------------------------------------


def test_result_total_is_exact_by_default():
    r = StatAnomalyResult(
        status="ok", detector="value_combo", method="self-baseline", baseline_size=0
    )
    assert r.total_findings_exact is True


def test_page_and_total_compose_both_statements_from_one_core():
    """The page and its exact total are built from the same *core*.

    This is the whole reason `_page_and_total` exists: ten scan sites used to
    need two hand-written statements each whose WHERE/GROUP BY/HAVING had to
    agree forever. Composed from one string they cannot drift, and a total
    over a wider set than its page is the failure this rules out.
    """
    svc = _svc([], totals=[42])
    core = (
        "SELECT val, count() AS cnt FROM db.events"
        " WHERE case_id = {cid:String} AND NOT has({excl:Array(String)}, evt_id)"
        " GROUP BY val HAVING cnt <= {floor:UInt32}"
    )
    params = {"cid": "c1", "excl": ["e1"], "floor": 3}
    page, total = svc._page_and_total(
        core,
        params,
        page_tail="ORDER BY cnt ASC\nLIMIT {lim:UInt32}",
        total_select="count()",
    )
    assert _scalar_total(total) == 42
    assert page == []

    page_sql = svc.ch.client._calls  # first lines only; the full text is below
    assert len(page_sql) == 1, "the page ran exactly once"
    total_sql = svc.ch.client.total_queries[0]
    assert total_sql.startswith("SELECT count() FROM (")
    assert ") AS scanned" in total_sql
    # Every filter of the page is inside the counted subquery, and the paging
    # is not — a LIMIT in the count would make it the page length again.
    assert "HAVING cnt <= {floor:UInt32}" in total_sql
    assert "NOT has({excl:Array(String)}, evt_id)" in total_sql
    assert "LIMIT" not in total_sql
    # Both statements bind the same parameters.
    assert svc.ch.client.total_parameters[0] == params
    assert svc.ch.client._all_parameters[0] == params


def _max_memory(sql: str) -> int:
    """The ``max_memory_usage`` a statement carries, as an int."""
    return int(sql.split("max_memory_usage = ")[1].split(",")[0].strip())


def test_page_and_total_split_the_slot_budget_between_its_two_statements(monkeypatch):
    """Both statements carry half the per-slot cap, not the full one.

    A gate slot's cap is sized per *slot*; the page and its count run
    concurrently under one slot, so each must carry half. `scan_fanout` only
    divides a clause built while it is declared — building the clause first
    and declaring the fan-out afterwards emits the full cap twice, which is
    exactly the over-commit the declaration exists to prevent. This asserts
    on the SQL that leaves the service, so a hoisted clause build fails here
    rather than in a ClickHouse OOM.
    """
    from vestigo.db import _scan
    from vestigo.db._scan import heavy_scan_settings, scan_fanout

    budget = 1 << 30
    monkeypatch.setattr(_scan, "detect_scan_memory_budget", lambda: budget)
    solo_clause = heavy_scan_settings()
    with scan_fanout(2):
        halved_clause = heavy_scan_settings()
    assert _max_memory(solo_clause) == budget, "fixture sanity"
    assert _max_memory(halved_clause) == budget // 2, "fixture sanity"

    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(RecordingClient([], totals=[0]))
    svc._page_and_total(
        "SELECT val, count() AS cnt FROM db.events WHERE case_id = {cid:String} GROUP BY val",
        {"cid": "c1"},
        page_tail="ORDER BY cnt ASC\nLIMIT 10",
        total_select="count()",
    )
    page_sql = svc.ch.client.full_queries[0]
    total_sql = svc.ch.client.total_queries[0]

    for sql in (page_sql, total_sql):
        assert _max_memory(sql) == budget // 2, sql[-160:]
        # The whole clause, spill thresholds included, is the fan-out clause.
        assert sql.rstrip().endswith(halved_clause), sql[-160:]
        assert solo_clause not in sql


def test_page_and_total_split_the_slot_threads_between_its_two_statements(monkeypatch):
    """Both statements carry half the heavy thread width, not the full one.

    The heavy width is `cores // N` so a full gate exactly saturates the box
    (`detect_scan_max_threads`); two statements at that width under one slot
    would make a full gate of paged detectors 2x the cores.
    """
    from vestigo.db import _scan

    monkeypatch.setattr(_scan, "detect_scan_max_threads", lambda: 8)
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(RecordingClient([], totals=[0]))
    svc._page_and_total(
        "SELECT val, count() AS cnt FROM db.events GROUP BY val",
        {},
        page_tail="LIMIT 10",
        total_select="count()",
    )
    for sql in (svc.ch.client.full_queries[0], svc.ch.client.total_queries[0]):
        assert "max_threads = 4," in sql, sql[-160:]


def test_page_and_total_composes_with_an_outer_fan_out(monkeypatch):
    """A caller already fanning out gets the product, not a reset to two.

    `scan_fanout` multiplies, and the clause has to be built in the caller's
    context for that to hold — a clause built anywhere else would lose the
    outer declaration.
    """
    from vestigo.db import _scan
    from vestigo.db._scan import scan_fanout

    budget = 1 << 30
    monkeypatch.setattr(_scan, "detect_scan_memory_budget", lambda: budget)
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(RecordingClient([], totals=[0]))
    with scan_fanout(2):
        svc._page_and_total(
            "SELECT val, count() AS cnt FROM db.events GROUP BY val",
            {},
            page_tail="LIMIT 10",
            total_select="count()",
        )
    for sql in (svc.ch.client.full_queries[0], svc.ch.client.total_queries[0]):
        assert _max_memory(sql) == budget // 4, sql[-160:]


def test_scalar_total_reads_an_empty_or_null_count_as_zero():
    """`count()` over an empty set returns no row; `sum(hits)` returns NULL."""
    assert _scalar_total([]) == 0
    assert _scalar_total([(None,)]) == 0
    assert _scalar_total([(7,)]) == 7


def test_sql_suppression_binds_both_sets_under_the_bound():
    params: dict[str, Any] = {}
    frag, reasons = _sql_suppression(
        params,
        key_expr="val",
        evt_expr="evt_id",
        allow_keys=["a", "b"],
        exclude_event_ids={"e1"},
    )
    assert "NOT has({allow:Array(String)}, val)" in frag
    assert "NOT has({excl:Array(String)}, evt_id)" in frag
    assert params["allow"] == ["a", "b"]
    assert params["excl"] == ["e1"]
    assert reasons == []


def test_sql_suppression_is_empty_without_sets():
    params: dict[str, Any] = {}
    frag, reasons = _sql_suppression(
        params, key_expr="val", evt_expr="evt_id", allow_keys=[], exclude_event_ids=None
    )
    assert frag == ""
    assert params == {}
    assert reasons == []


def test_sql_suppression_falls_back_when_a_set_is_too_large():
    """A set past the bind bound is applied to the page instead — and says so."""
    params: dict[str, Any] = {}
    big = {f"e{i}" for i in range(_SQL_SUPPRESSION_MAX + 1)}
    frag, reasons = _sql_suppression(
        params, key_expr="val", evt_expr="evt_id", allow_keys=["a"], exclude_event_ids=big
    )
    # The allowlist still binds; only the oversized set falls back.
    assert "NOT has({allow:Array(String)}, val)" in frag
    assert "excl" not in frag
    assert "excl" not in params
    assert len(reasons) == 1
    assert "page only" in reasons[0]
    assert f"{_SQL_SUPPRESSION_MAX + 1:,}" in reasons[0]


def test_sql_suppression_without_key_expr_skips_the_allowlist():
    params: dict[str, Any] = {}
    frag, _ = _sql_suppression(
        params, key_expr=None, evt_expr="evt_id", allow_keys=["a"], exclude_event_ids={"e"}
    )
    assert "allow" not in frag and "allow" not in params
    assert "excl" in frag


def test_hydration_is_chunked():
    """Hydration binds one parameter per id, so it must fetch in bounded batches."""
    svc = _svc([])
    n = _HYDRATE_CHUNK * 2 + 200
    svc.ch.events_by_id = {f"e{i}": {"event_id": f"e{i}"} for i in range(n)}
    findings = [
        ValueFinding(
            field="f",
            value=str(i),
            count=1,
            score=1.0,
            first_seen=None,
            event_id=f"e{i}",
            event=None,
            details={},
        )
        for i in range(n)
    ]
    svc._hydrate_finding_events("c", ["s"], findings)
    assert [len(c) for c in svc.ch.hydration_calls] == [_HYDRATE_CHUNK, _HYDRATE_CHUNK, 200]
    assert findings[n - 1].event == {"event_id": f"e{n - 1}"}


# ---------------------------------------------------------------------------
# _col_expr unit tests
# ---------------------------------------------------------------------------


def test_col_expr_top_level_column():
    params: dict[str, Any] = {}
    assert _col_expr("artifact", params) == "artifact"
    assert params == {}


def test_col_expr_top_level_column_shared_with_queries_allowlist():
    """F10: anomaly_stats and queries.py share one top-level-column allowlist
    (via db._columns), so a field like `parser_version` — a real top-level
    column, not previously in anomaly_stats' narrower local list — resolves
    to the column here too instead of silently becoming an always-empty
    attribute lookup."""
    params: dict[str, Any] = {}
    assert _col_expr("parser_version", params) == "parser_version"
    assert params == {}


def test_col_expr_attr_prefix():
    params: dict[str, Any] = {}
    expr = _col_expr("attr:user_agent", params)
    assert expr == "attributes[{fk:String}]"
    assert params == {"fk": "user_agent"}


def test_col_expr_bare_attr_name():
    """Bare names not in the top-level set are treated as attribute keys."""
    params: dict[str, Any] = {}
    expr = _col_expr("ip_address", params)
    assert expr == "attributes[{fk:String}]"
    assert params["fk"] == "ip_address"


# ---------------------------------------------------------------------------
# find_value_novelty — self-baseline
# ---------------------------------------------------------------------------


def test_value_novelty_no_data():
    """Returns no_data when total_events = 0."""
    responses = [
        FakeQueryResult(result_rows=[(0,)], column_names=["count()"]),
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty("c1", ["s1"])
    assert result.status == "no_data"
    assert result.results == []


def test_value_novelty_supplied_inventory_skips_live_field_inventory():
    """M22(d): when the router passes a cache-built inventory, the fields=None
    auto-selection path must feed it to the recommender instead of running the
    live field_inventory map scan (the expensive ARRAY JOIN query family)."""
    responses = [
        # _count_events
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        # per-field novelty scans return nothing — irrelevant here
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=None,
        inventory=[("attr:user", 5, 90)],
        inventory_total=100,
    )
    assert result.status in ("ok", "no_data")
    # The scanned field set came from the supplied inventory — if the live
    # field_inventory scan had run instead, it would have consumed the canned
    # responses, yielded an empty recommendation, and fallen back to
    # _DEFAULT_NOVELTY_FIELDS (which don't include attr:user).
    assert any("user" in str(p.values()) for p in svc.ch.client._all_parameters)
    # Exactly one aggregate ran before the per-field scans: _count_events.
    # A live inventory path would add its batched top-level + ARRAY JOIN scans.
    count_queries = [q for q in svc.ch.client._calls if q.startswith("SELECT count()")]
    assert len(count_queries) == 1


def test_value_novelty_self_baseline_returns_rare_values():
    """Rare values (count ≤ rarity_floor) should be returned, rarest first."""
    import math

    total = 1000
    responses = [
        # Total events count
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        # artifact field: two rare values
        FakeQueryResult(
            result_rows=[
                # Naive datetimes — matches clickhouse-connect's real return type
                # for a DateTime column with no explicit timezone.
                ("suspicious.exe", 1, datetime(2024, 1, 2), "evt-1"),
                ("unusual_tool", 2, datetime(2024, 1, 1), "evt-2"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
        # timestamp_desc field: one rare value
        FakeQueryResult(
            result_rows=[
                ("Malware execution", 1, datetime(2024, 1, 2, 1), "evt-3"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
        # display_name field: no rare values
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact", "timestamp_desc", "display_name"],
        rarity_floor=3,
        limit=50,
    )

    assert result.status == "ok"
    assert result.detector == "value_novelty"
    assert result.method == "self-baseline"
    assert len(result.results) == 3  # 2 + 1

    # All findings are ValueFinding instances.
    assert all(isinstance(r, ValueFinding) for r in result.results)

    # Sorted by score descending (count=1 has higher score than count=2).
    counts = [r.count for r in result.results]
    assert counts[0] <= counts[-1], "Rarest values should rank first"

    # Score = -log(count/total); count=1 → -log(1/1000) ≈ 6.9
    top = result.results[0]
    expected_score = -math.log(1 / total)
    assert abs(top.score - expected_score) < 0.01

    # Details shape.
    for r in result.results:
        d = r.details
        assert d["detector"] == "value_novelty"
        assert d["method"] == "self-baseline"
        assert "field" in d
        assert "value" in d
        assert "count" in d
        assert "surprise" in d


def test_value_novelty_self_baseline_limit_applied():
    """Limit caps the number of returned findings across all fields."""
    total = 500
    # Three fields each returning 3 rare values → 9 total, but limit=4.
    per_field = [
        FakeQueryResult(
            result_rows=[
                (f"val_{i}", 1, datetime(2024, 1, 1), f"evt-{j * 3 + i}") for i in range(3)
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        )
        for j in range(3)
    ]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
            *per_field,
        ],
        totals=[3, 3, 3],
    )
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact", "timestamp_desc", "display_name"],
        limit=4,
    )
    assert result.status == "ok"
    assert len(result.results) <= 4
    # The pre-cap survivor count is reported so the UI can offer "load more"
    # instead of silently truncating.
    assert result.total_findings == 9


def test_value_novelty_event_id_populated():
    """Each finding carries the event_id of its first occurrence."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("backdoor.exe", 1, datetime(2024, 1, 1), "evt-abc")],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
        ]
    )
    # Seed the hydration source: the scan only aggregates event_id, the full
    # event is fetched in one get_events_by_ids batch on the final slice.
    svc.ch.events_by_id["evt-abc"] = {
        "event_id": "evt-abc",
        "message": "bad msg",
        "timestamp": "2024-01-01T00:00:00+00:00",
    }
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact"])
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.event_id == "evt-abc"
    assert r.event is not None
    assert r.event["message"] == "bad msg"
    assert r.value == "backdoor.exe"
    # Hydration ran as exactly one batched fetch.
    assert svc.ch.hydration_calls == [["evt-abc"]]
    # first_seen must carry an explicit UTC offset — a bare "YYYY-MM-DD
    # HH:MM:SS" string is ambiguous to JS's Date parser (browsers treat it as
    # local time), which silently shifted histogram markers and event-grid
    # anomaly matching by the browser's UTC offset.
    assert r.first_seen is not None
    assert r.first_seen.endswith("+00:00") or r.first_seen.endswith("Z")
    assert r.event["timestamp"] == r.first_seen


def test_value_novelty_skips_empty_values():
    """Rows with empty string values must be filtered out."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[
                    ("", 1, datetime(2024, 1, 1), "evt-1"),
                    ("real_value", 2, datetime(2024, 1, 1), "evt-2"),
                ],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
        ]
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact"])
    assert result.status == "ok"
    values = [r.value for r in result.results]
    assert "" not in values
    assert "real_value" in values


def test_value_novelty_total_sums_exact_per_field_counts():
    """Each field's scan counts its own findings; the total is their sum."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[(f"a{i}", 1, datetime(2024, 1, 1), f"e{i}") for i in range(50)],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
            FakeQueryResult(
                result_rows=[(f"b{i}", 1, datetime(2024, 1, 1), f"f{i}") for i in range(50)],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
        ],
        totals=[300, 250],
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact", "timestamp_desc"], limit=50)
    assert len(result.results) == 50
    assert result.total_findings == 550
    assert result.total_findings_exact is True


def test_value_novelty_per_field_budget_is_at_least_the_limit():
    """A global top-50 may be 50 values of one field: the per-field page must allow it."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc.find_value_novelty("c1", ["s1"], fields=["artifact"], limit=80, per_field_limit=25)
    assert svc.ch.client._all_parameters[1]["lim"] == 80


def test_value_novelty_per_field_suppression_is_bound_into_the_sql():
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact"],
        allowlist={("artifact", "known"), ("other", "ignored")},
        exclude_event_ids={"e1"},
    )
    sql = client.full_queries[-1]
    p = client._all_parameters[-1]
    # The suppression has to reach the count too, or the total describes a
    # wider set than the page it annotates.
    _assert_paired(
        sql,
        client.total_queries[-1],
        "NOT has({allow:Array(String)}, val)",
        "NOT has({excl:Array(String)}, evt_id)",
    )
    assert client.total_queries[-1].startswith("SELECT count() FROM (")
    assert p["allow"] == ["known"]
    assert p["excl"] == ["e1"]


def test_value_novelty_batched_binds_per_key_allowlist():
    """The batched pass carries one allowlist per attribute key, and counts per key."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["attr:user", "attr:host"],
        allowlist={("attr:user", "svc"), ("attr:user", "root"), ("attr:host", "web-1")},
        exclude_event_ids={"e1"},
    )
    sql = client.full_queries[-1]
    p = client._all_parameters[-1]
    # The batched pass counts per attribute key, so its companion is a grouped
    # aggregate rather than a one-cell count.
    total_sql = client.total_queries[-1]
    assert total_sql.startswith("SELECT key, count() FROM (")
    assert "GROUP BY key" in total_sql.rsplit(") AS scanned", 1)[1]
    _assert_paired(sql, total_sql, "indexOf({allow_k:Array(String)}, key)")
    assert p["allow_k"] == ["host", "user"]
    assert p["allow_v"] == [["web-1"], ["root", "svc"]]
    assert "indexOf({allow_k:Array(String)}, key)" in sql
    assert "NOT has({excl:Array(String)}, evt_id)" in sql


def test_value_novelty_batched_temporal_counts_hits_per_key():
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(300, 80)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 4, tzinfo=UTC),
    )
    svc.find_value_novelty("c1", ["s1"], fields=["attr:user"], windows=windows)
    sql = client.full_queries[-1]
    # A group that hit two suspect windows is two findings: the total sums
    # hits, per key.
    assert client.total_queries[-1].startswith("SELECT key, sum(hits) FROM (")
    assert "ORDER BY key ASC, best ASC, val ASC" in sql
    assert "ORDER BY" not in client.total_queries[-1].rsplit(") AS scanned", 1)[1]
    assert client._all_parameters[-1]["w0_total"] == 80.0


# ---------------------------------------------------------------------------
# find_value_novelty — temporal baseline
# ---------------------------------------------------------------------------


def test_value_novelty_temporal_baseline_first_seen():
    """Temporal mode flags values absent in baseline but present in a suspect window."""
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        label="exfil-window",
    )
    svc = _svc(
        [
            # Total events
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            # Window totals: baseline_total, w0_total
            FakeQueryResult(result_rows=[(300, 120)], column_names=["bl_total", "w0_total"]),
            # artifact field: val, baseline_cnt, w0_cnt, w0_first, w0_evt
            FakeQueryResult(
                result_rows=[
                    ("first_time_process.exe", 0, 3, datetime(2024, 1, 16), "evt-9", 1, 0.025),
                ],
                column_names=[
                    "val",
                    "baseline_cnt",
                    "w0_cnt",
                    "w0_first",
                    "w0_evt",
                    "hits",
                    "best",
                ],
            ),
        ]
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["artifact"], windows=windows)
    assert result.status == "ok"
    assert result.method == "temporal"
    assert result.baseline_size == 300
    assert len(result.results) == 1
    r = result.results[0]
    assert r.value == "first_time_process.exe"
    assert r.count == 3
    assert r.details["method"] == "temporal"
    # Surprise denominator is the suspect window's own event count, not the corpus.
    assert r.details["window_total_events"] == 120
    assert r.details["total_events"] == 120
    assert r.details["window_label"] == "exfil-window"
    assert result.windows["suspect_windows"][0]["label"] == "exfil-window"


def test_value_novelty_temporal_window_bounds_converted_to_utc_for_sql():
    """Window bounds with a non-UTC offset (e.g. FastAPI-parsed `+02:00`) must
    be converted to the equivalent UTC instant before being spliced into the
    ClickHouse SQL string literals — otherwise the window lands 2h off (F8)."""
    from datetime import timedelta, timezone

    plus_two = timezone(timedelta(hours=2))
    windows = _one_suspect(
        datetime(2024, 1, 1, 2, 0, 0, tzinfo=plus_two),
        datetime(2024, 1, 15, 14, 0, 0, tzinfo=plus_two),
        datetime(2024, 1, 16, 2, 0, 0, tzinfo=plus_two),
        datetime(2024, 1, 20, 2, 0, 0, tzinfo=plus_two),
    )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(300, 0)], column_names=["bl_total", "w0_total"]),
            FakeQueryResult(
                result_rows=[],
                column_names=[
                    "val",
                    "baseline_cnt",
                    "w0_cnt",
                    "w0_first",
                    "w0_evt",
                    "hits",
                    "best",
                ],
            ),
        ]
    )
    svc.find_value_novelty("c1", ["s1"], fields=["artifact"], windows=windows)
    b1_values = [p["b1"] for p in svc.ch.client._all_parameters if "b1" in p]
    assert b1_values
    # 14:00 +02:00 == 12:00 UTC.
    assert all(v == "2024-01-15 12:00:00.000" for v in b1_values)


def test_value_novelty_two_suspect_windows_attributed_separately():
    """A value present in two suspect windows yields one finding per window,
    each scored against its own window's event count."""
    windows = AnalysisWindows(
        baseline=TimeWindow(
            "baseline", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 10, tzinfo=UTC)
        ),
        suspects=(
            TimeWindow("w-a", datetime(2024, 1, 11, tzinfo=UTC), datetime(2024, 1, 12, tzinfo=UTC)),
            TimeWindow("w-b", datetime(2024, 1, 20, tzinfo=UTC), datetime(2024, 1, 21, tzinfo=UTC)),
        ),
    )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            # baseline_total, w0_total, w1_total
            FakeQueryResult(result_rows=[(600, 100, 200)], column_names=["bl", "w0", "w1"]),
            # key, val, baseline_cnt, w0_cnt, w0_first, w0_evt, w1_cnt, w1_first, w1_evt
            # (batched attr scan rows carry the leading map key)
            FakeQueryResult(
                result_rows=[
                    (
                        "user",
                        "svc_x",
                        0,
                        4,
                        datetime(2024, 1, 11),
                        "evt-a",
                        6,
                        datetime(2024, 1, 20),
                        "evt-b",
                        2,
                        0.03,
                    )
                ],
                column_names=[
                    "key",
                    "val",
                    "baseline_cnt",
                    "w0_cnt",
                    "w0_first",
                    "w0_evt",
                    "w1_cnt",
                    "w1_first",
                    "w1_evt",
                    "hits",
                    "best",
                ],
            ),
        ]
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["attr:user"], windows=windows)
    by_label = {r.details["window_label"]: r for r in result.results}
    assert set(by_label) == {"w-a", "w-b"}
    assert by_label["w-a"].count == 4
    assert by_label["w-a"].details["window_total_events"] == 100
    assert by_label["w-b"].count == 6
    assert by_label["w-b"].details["window_total_events"] == 200


def test_value_novelty_allowlist_suppresses_value_everywhere():
    """An allowlisted (field, value) is dropped regardless of its event."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[
                    ("keep_me", 1, datetime(2024, 1, 1), "evt-1"),
                    ("known_good", 1, datetime(2024, 1, 1), "evt-2"),
                ],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
        ]
    )
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact"],
        allowlist={("artifact", "known_good")},
    )
    assert [r.value for r in result.results] == ["keep_me"]


def test_value_novelty_small_window_warns():
    """A suspect window below _MIN_WINDOW_EVENTS gets a warning, not suppression."""
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 10, tzinfo=UTC),
        datetime(2024, 1, 11, tzinfo=UTC),
        datetime(2024, 1, 12, tzinfo=UTC),
        label="tiny",
    )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(600, 5)], column_names=["bl", "w0"]),
            FakeQueryResult(
                result_rows=[("user", "svc_x", 0, 3, datetime(2024, 1, 11), "evt-a", 1, 0.6)],
                column_names=[
                    "key",
                    "val",
                    "baseline_cnt",
                    "w0_cnt",
                    "w0_first",
                    "w0_evt",
                    "hits",
                    "best",
                ],
            ),
        ]
    )
    result = svc.find_value_novelty("c1", ["s1"], fields=["attr:user"], windows=windows)
    assert len(result.results) == 1  # still surfaced
    assert any("tiny" in w and "unstable" in w for w in result.warnings)


def test_value_novelty_batched_sql_shape_self_baseline():
    """Attr fields share one ARRAY JOIN pass; top-level fields stay per-field."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[], column_names=[]),  # batched attr scan
            FakeQueryResult(result_rows=[], column_names=[]),  # artifact per-field scan
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_value_novelty("c1", ["s1"], fields=["attr:user", "attr:host", "artifact"])

    batched_sql = client.full_queries[1]
    assert "ARRAY JOIN mapKeys(attributes) AS key, mapValues(attributes) AS val" in batched_sql
    assert "has({nkeys:Array(String)}, key)" in batched_sql
    assert "GROUP BY key, val" in batched_sql
    assert "ORDER BY key ASC, cnt ASC, first_seen ASC" in batched_sql
    assert "LIMIT {lim:UInt32} BY key" in batched_sql
    assert "attributes[{fk:String}]" not in batched_sql
    assert sorted(client._all_parameters[1]["nkeys"]) == ["host", "user"]

    artifact_sql = client.full_queries[2]
    assert "ARRAY JOIN" not in artifact_sql
    assert "artifact AS val" in artifact_sql
    assert "LIMIT {lim:UInt32}" in artifact_sql
    assert "BY key" not in artifact_sql


def test_value_novelty_batched_sql_shape_temporal():
    """The temporal batched scan keeps windows, sentinel guard and per-key limit."""
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 10, tzinfo=UTC),
        datetime(2024, 1, 11, tzinfo=UTC),
        datetime(2024, 1, 12, tzinfo=UTC),
    )
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(60, 10)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),  # batched attr scan
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_value_novelty("c1", ["s1"], fields=["attr:user"], windows=windows)

    batched_sql = client.full_queries[2]
    assert "ARRAY JOIN mapKeys(attributes) AS key, mapValues(attributes) AS val" in batched_sql
    assert "HAVING baseline_cnt = 0 AND hits > 0" in batched_sql
    assert "LIMIT {lim:UInt32} BY key" in batched_sql
    assert "timestamp !=" in batched_sql  # sentinel guard
    # Window bounds bound as parameters (forensic reproducibility).
    params = client._all_parameters[2]
    assert params["b0"] == "2024-01-01 00:00:00.000"
    assert params["w0s"] == "2024-01-11 00:00:00.000"


def test_value_novelty_mapped_field_stays_per_field():
    """A canonical mapped field keeps the coalesce per-field query (no batching)."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[], column_names=[]),  # mapped per-field scan
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_value_novelty(
        "c1", ["s1"], fields=["user"], field_mappings={"user": ["username", "uid"]}
    )

    mapped_sql = client.full_queries[1]
    assert "ARRAY JOIN" not in mapped_sql
    assert "coalesce(nullif(attributes[{fk_m0:String}], '')" in mapped_sql


def test_full_bucket_starts_excludes_partial_edges():
    """_full_bucket_starts only yields buckets fully inside the window."""
    # Window [00:10, 02:00) with 1h buckets: only the 01:00 bucket is fully in.
    w = TimeWindow(
        "x", datetime(2024, 1, 1, 0, 10, tzinfo=UTC), datetime(2024, 1, 1, 2, 0, tzinfo=UTC)
    )
    starts = _full_bucket_starts(w, 3600)
    assert starts == [datetime(2024, 1, 1, 1, 0, tzinfo=UTC)]


# ---------------------------------------------------------------------------
# find_frequency_anomalies
# ---------------------------------------------------------------------------


def _make_freq_responses(
    min_ts: str = "2024-01-01 00:00:00",
    max_ts: str = "2024-01-02 00:00:00",
    bucket_rows: list[tuple] | None = None,
) -> list[FakeQueryResult]:
    """Build the canned responses for find_frequency_anomalies."""
    from datetime import datetime

    min_dt = datetime.strptime(min_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    max_dt = datetime.strptime(max_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)

    if bucket_rows is None:
        # Default: flat series of 5 buckets + one spike (4h buckets for a
        # 24h window with 6 buckets).
        bucket_rows = [
            (min_dt, "LOG", 10),
            (min_dt.replace(hour=4), "LOG", 10),
            (min_dt.replace(hour=8), "LOG", 10),
            (min_dt.replace(hour=12), "LOG", 10),
            (min_dt.replace(hour=16), "LOG", 10),
            # Spike: 5× the mean
            (min_dt.replace(hour=20), "LOG", 50),
        ]

    return [
        FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
    ]


def test_frequency_no_data_when_no_events():
    """Returns no_data when the events table has no timestamps."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(None, None)], column_names=["min", "max"]),
        ]
    )
    result = svc.find_frequency_anomalies("c1", ["s1"])
    assert result.status == "no_data"


def test_frequency_no_data_when_no_bucket_rows():
    """Returns no_data when the bucket query returns no rows."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    result = svc.find_frequency_anomalies("c1", ["s1"])
    assert result.status == "no_data"


def test_frequency_spike_detected():
    """A window 5× the series mean should be flagged as anomalous."""
    svc = _svc(_make_freq_responses())
    # Suppress the hydration query for this test (no DB available).
    # find_frequency_anomalies calls _hydrate_freq_findings which hits CH.
    # We monkey-patch it to be a no-op.
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1",
        ["s1"],
        series_field="artifact",
        z_threshold=2.0,
        limit=10,
    )
    assert result.status == "ok"
    assert result.detector == "frequency"
    assert result.method == "z-score"
    assert len(result.results) >= 1

    # The spike bucket should rank first.
    top = result.results[0]
    assert isinstance(top, FreqFinding)
    assert top.observed > top.expected
    assert top.z_score > 0
    assert top.score == abs(top.z_score)
    assert top.series_field == "artifact"
    assert top.series_value == "LOG"


def test_frequency_details_shape():
    """FreqFinding.details carries the expected keys."""
    svc = _svc(_make_freq_responses())
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=2.0)
    assert result.status == "ok"
    for r in result.results:
        d = r.details
        assert d["detector"] == "frequency"
        assert "series_field" in d
        assert "series_value" in d
        assert "window_start" in d
        assert "window_end" in d
        assert "observed" in d
        assert "expected" in d
        assert "z_score" in d
        assert "interval_seconds" in d


def test_frequency_silence_detected():
    """A fully-silent bucket inside a series' active span must score as a drop.

    Regression: self-baseline mode built each series only from non-empty
    GROUP BY buckets, so a bucket with zero events never entered the series —
    silences were undetectable and the dropped zeros inflated mean/std.
    """
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    max_dt = datetime(2024, 1, 1, 6, 0, tzinfo=UTC)
    # 6h range, 6 buckets → 1h interval; grid covers 7 aligned starts.
    # Steady 10/bucket with hour 3 completely absent (the silence).
    bucket_rows = [(min_dt.replace(hour=h), "LOG", 10) for h in (0, 1, 2, 4, 5, 6)]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], bucket_count=6, z_threshold=2.0)
    assert result.status == "ok"
    assert len(result.results) == 1
    silence = result.results[0]
    assert silence.observed == 0
    assert silence.z_score < 0
    assert silence.window_start == min_dt.replace(hour=3).isoformat()


def test_frequency_coverage_boundary_not_silence():
    """Grid buckets outside a series' own first/last active bucket are coverage
    boundaries (e.g. disjoint multi-source spans), not silences — no findings."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, 0, 0, tzinfo=UTC)
    # 24h range, 24 buckets → 1h interval. Series only active hours 0-5,
    # perfectly flat there; hours 6-24 have no events at all.
    bucket_rows = [(min_dt.replace(hour=h), "LOG", 10) for h in range(6)]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], bucket_count=24, z_threshold=2.0)
    assert result.status == "ok"
    assert result.results == []


def test_frequency_constant_series_ignored():
    """A perfectly flat series (std=0) must not produce any findings."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    max_dt = datetime(2024, 1, 1, 6, 0, tzinfo=UTC)
    # Perfectly uniform, gap-free series over the whole 1h-bucket grid.
    bucket_rows = [(min_dt.replace(hour=h), "LOG", 10) for h in range(7)]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], bucket_count=6, z_threshold=2.0)
    assert result.status == "ok"
    assert result.results == []


def test_frequency_insufficient_buckets_series_skipped():
    """Series with < _MIN_FREQUENCY_BUCKETS data points are skipped, and the
    result says so via status="insufficient_data" rather than a silent "ok"
    with an empty result list (indistinguishable from "nothing anomalous")."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 2, tzinfo=UTC)
    # Only 2 buckets — below the minimum of 3.
    bucket_rows = [
        (min_dt, "LOG", 10),
        (min_dt.replace(hour=12), "LOG", 100),
    ]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=1.0)
    assert result.status == "insufficient_data"
    assert result.results == []


def test_frequency_windowed_bounds_hydration_input():
    """Temporal frequency can emit a finding per suspect-window bucket (every
    silent bucket is a drop vs the baseline). Hydration must run on a bounded
    slice, not the full list, or its ClickHouse query params overflow the
    field-length limit ("Field value too long") on a real cluster."""
    # Baseline 6h → 6 full 1h buckets; a long suspect window (72h → 72 buckets)
    # for a series that is silent throughout → ~72 drop findings.
    windows = _one_suspect(
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 4, 6, 0, tzinfo=UTC),
        label="wide",
    )
    # Baseline buckets ~10/bucket so a silent (0) suspect bucket scores as a drop.
    bucket_rows = [(_h(i), "LOG", 10) for i in range(6)]
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])

    hydration_sizes: list[int] = []

    def _spy(findings, *a, **kw):
        hydration_sizes.append(len(findings))
        return findings

    svc._hydrate_freq_findings = _spy  # type: ignore[method-assign]
    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=windows, bucket_count=6, z_threshold=2.0, limit=50
    )
    assert result.status == "ok"
    assert len(result.results) <= 50
    # Hydration saw at most the buffered cap (max(limit*3, 100)), not all ~72.
    assert hydration_sizes and all(n <= max(50 * 3, 100) for n in hydration_sizes)


def test_frequency_limit_applied():
    """Limit caps the number of findings returned."""
    from datetime import datetime

    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 4, tzinfo=UTC)
    # 3 series each with one spike bucket.
    bucket_rows = []
    for i, sv in enumerate(["A", "B", "C"]):
        for h in range(5):
            cnt = 10 if h < 4 else 100  # spike on last bucket
            bucket_rows.append(
                (
                    min_dt.replace(hour=0) + __import__("datetime").timedelta(hours=h * 12 + i * 2),
                    sv,
                    cnt,
                )
            )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
            FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "series_val", "cnt"]),
        ]
    )
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], limit=2, z_threshold=2.0)
    assert len(result.results) <= 2


def test_frequency_sorted_by_z_score():
    """Findings must be sorted by |z_score| descending."""
    svc = _svc(_make_freq_responses())
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies("c1", ["s1"], z_threshold=1.0)
    scores = [r.score for r in result.results]
    assert scores == sorted(scores, reverse=True)


def _freq_windows() -> AnalysisWindows:
    """6h baseline + 3h suspect window; with bucket_count=6 the interval is 1h,
    giving 6 full baseline buckets and 3 full suspect buckets."""
    return _one_suspect(
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 9, 0, tzinfo=UTC),
        label="incident",
    )


def _h(hour: int) -> datetime:
    return datetime(2024, 1, 1, hour, 0, tzinfo=UTC)


def test_frequency_temporal_baseline():
    """Temporal mode learns mean/std from the baseline window's (zero-filled,
    full-only) buckets and scores each suspect-window bucket against them.
    The windowed path issues a single bucket scan — no min/max query."""
    bucket_rows = [
        (_h(0), "LOG", 10),
        (_h(1), "LOG", 12),
        (_h(2), "LOG", 11),
        (_h(3), "LOG", 10),
        (_h(4), "LOG", 9),
        (_h(5), "LOG", 10),
        # Suspect window buckets: a spike at 07:00, quiet otherwise.
        (_h(6), "LOG", 10),
        (_h(7), "LOG", 200),
        (_h(8), "LOG", 10),
    ]
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=_freq_windows(), bucket_count=6, z_threshold=2.0
    )
    assert result.status == "ok"
    assert result.method == "temporal-z-score"
    assert len(result.results) == 1
    spike = result.results[0]
    assert spike.observed == 200
    assert spike.z_score > 0
    assert spike.details["suspect_window_label"] == "incident"
    assert result.windows["suspect_windows"][0]["label"] == "incident"


def test_frequency_temporal_zero_baseline_flagged():
    """A series absent from the baseline but active in a suspect window is flagged.

    Zero-fill makes its baseline mean 0; the std floor lets the suspect-window
    activity score instead of dividing by ~0 — exactly the "brand-new activity
    after the incident start" case temporal mode exists to surface.
    """
    bucket_rows = [
        (_h(6), "NEWPROC", 50),
        (_h(7), "NEWPROC", 60),
    ]
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=_freq_windows(), bucket_count=6, z_threshold=2.0
    )
    assert result.status == "ok"
    assert {r.series_value for r in result.results} == {"NEWPROC"}


def test_frequency_temporal_hand_computed_z():
    """The suspect-window z-score matches a hand-computed mean/std over the
    zero-filled baseline buckets."""
    import numpy as np

    baseline_counts = [10, 12, 11, 10, 9, 10]
    bucket_rows = [(_h(i), "LOG", c) for i, c in enumerate(baseline_counts)]
    bucket_rows.append((_h(7), "LOG", 200))
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=_freq_windows(), bucket_count=6, z_threshold=2.0
    )
    arr = np.array(baseline_counts, dtype=float)
    mean = arr.mean()
    std = max(arr.std(ddof=1), 0.5)
    expected_z = (200 - mean) / std
    spike = next(r for r in result.results if r.observed == 200)
    assert abs(spike.z_score - round(expected_z, 4)) < 0.01


def test_frequency_temporal_partial_buckets_excluded():
    """Buckets cut by a window edge are excluded from scoring — a window
    shorter than one interval yields a warning, never a bogus 1-bucket z."""
    # Baseline 6h (6 full 1h buckets); suspect window only 30 min → no full bucket.
    windows = _one_suspect(
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 15, tzinfo=UTC),
        datetime(2024, 1, 1, 6, 45, tzinfo=UTC),
        label="tiny",
    )
    bucket_rows = [(_h(i), "LOG", 10) for i in range(6)]
    svc = _svc([FakeQueryResult(result_rows=bucket_rows, column_names=["bucket", "sv", "cnt"])])
    svc._hydrate_freq_findings = lambda findings, *a, **kw: findings  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=windows, bucket_count=6, z_threshold=2.0
    )
    assert result.results == []
    assert any("tiny" in w and "shorter than" in w for w in result.warnings)


def test_frequency_temporal_baseline_too_short_warns():
    """A baseline spanning fewer than _MIN_FREQUENCY_BUCKETS full buckets can't
    build a distribution — insufficient_data with an explanatory warning."""
    # Baseline 90 min, bucket_count=6 → interval 900s (15 min) → 6 buckets, ok.
    # Force too-few by making the baseline only 20 min with a 15-min interval.
    windows = _one_suspect(
        datetime(2024, 1, 1, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 0, 20, tzinfo=UTC),
        datetime(2024, 1, 1, 1, 0, tzinfo=UTC),
        datetime(2024, 1, 1, 2, 0, tzinfo=UTC),
    )
    svc = _svc([FakeQueryResult(result_rows=[], column_names=["bucket", "sv", "cnt"])])
    # bucket_count=2 over a 20-min baseline → interval 600s (10 min) → only 2
    # full buckets, below the _MIN_FREQUENCY_BUCKETS=3 floor.
    result = svc.find_frequency_anomalies(
        "c1", ["s1"], windows=windows, bucket_count=2, z_threshold=2.0
    )
    assert result.status == "insufficient_data"
    assert any("Baseline window" in w for w in result.warnings)


def test_frequency_exclude_event_ids_backfills_from_next_ranked():
    """Excluded findings must be dropped before the limit slice, not after.

    Regression test: filtering exclude_event_ids after `[:limit]` silently
    shrinks the page below `limit` when the top-ranked finding is excluded,
    instead of promoting the next-ranked finding to fill the slot.
    """
    from dataclasses import replace as _replace
    from datetime import datetime as _dt

    min_dt = _dt(2024, 1, 1, tzinfo=UTC)
    bucket_rows = [
        # Series "A": baseline of 10s + a huge spike (highest-ranked finding).
        (min_dt, "A", 10),
        (min_dt.replace(hour=4), "A", 10),
        (min_dt.replace(hour=8), "A", 10),
        (min_dt.replace(hour=12), "A", 100),
        # Series "B": baseline of 10s + a smaller spike (lower-ranked finding).
        (min_dt.replace(hour=16), "B", 10),
        (min_dt.replace(hour=18), "B", 10),
        (min_dt.replace(hour=20), "B", 10),
        (min_dt.replace(hour=22), "B", 40),
    ]
    svc = _svc(_make_freq_responses(bucket_rows=bucket_rows))

    def _fake_hydrate(findings, *a, **kw):
        # Assign event ids in insertion order: series "A" is scanned first,
        # so its spike (the higher-ranked finding) becomes evt-0.
        return [_replace(f, event_id=f"evt-{i}") for i, f in enumerate(findings)]

    svc._hydrate_freq_findings = _fake_hydrate  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1",
        ["s1"],
        z_threshold=1.0,
        limit=1,
        exclude_event_ids={"evt-0"},
    )
    assert result.status == "ok"
    # With the top-ranked finding (series "A") excluded, series "B"'s
    # finding must backfill the slot rather than leaving the page empty.
    assert len(result.results) == 1
    assert result.results[0].series_value == "B"


# ---------------------------------------------------------------------------
# _classify_field unit tests
# ---------------------------------------------------------------------------


def test_classify_field_constant():
    kind, recommended = _classify_field(distinct=1, non_empty_count=1000)
    assert kind == "constant"
    assert recommended is False


def test_classify_field_zero_distinct():
    kind, recommended = _classify_field(distinct=0, non_empty_count=0)
    assert kind == "constant"
    assert recommended is False


def test_classify_field_identifier():
    # 900 unique values out of 1000 non-empty → ratio = 0.9 = exact boundary
    kind, recommended = _classify_field(distinct=900, non_empty_count=1000)
    assert kind == "identifier"
    assert recommended is False


def test_classify_field_identifier_near_unique():
    # Near-unique: hash-like field (9800/9800 = 1.0)
    kind, recommended = _classify_field(distinct=9800, non_empty_count=9800)
    assert kind == "identifier"
    assert recommended is False


def test_classify_field_sparse():
    # Only 1% coverage — sparse (50 non-empty out of 5000 total)
    kind, recommended = _classify_field(distinct=50, non_empty_count=50, total=5000)
    assert kind == "sparse"
    assert recommended is False


def test_classify_field_categorical():
    # Good cardinality and coverage: 10 distinct out of 950 non-empty (0.01 ratio)
    kind, recommended = _classify_field(distinct=10, non_empty_count=950, total=1000)
    assert kind == "categorical"
    assert recommended is True


def test_classify_field_categorical_moderate():
    # status_code-like: 5 distinct, 1000 non-empty
    kind, recommended = _classify_field(distinct=5, non_empty_count=1000, total=1000)
    assert kind == "categorical"
    assert recommended is True


# ---------------------------------------------------------------------------
# recommend_novelty_fields tests
# ---------------------------------------------------------------------------


def _svc_with_recommend_responses(
    top_row: tuple,
    attr_rows: list[tuple],
    total: int = 1000,
) -> StatisticalAnomalyService:
    """Build a service whose FakeClient provides recommend_novelty_fields responses."""
    responses = [
        # Total count
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        # Top-level batch (4 columns × 2 agg each = 8 values in one row)
        FakeQueryResult(result_rows=[top_row], column_names=["c"] * 8),
        # Attribute keys + cardinality
        FakeQueryResult(result_rows=attr_rows, column_names=["key", "dist", "cov_count"]),
    ]
    return _svc(responses)


def test_recommend_novelty_fields_empty_on_no_data():
    """Returns empty list when no events exist."""
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.recommend_novelty_fields("c1", ["s1"])
    assert result == []


def test_recommend_novelty_fields_categorical_recommended():
    """Categorical fields (moderate cardinality) should be recommended."""
    # artifact: 5 distinct, 1000 non-empty  → categorical
    # timestamp_desc: 20 distinct, 950 non-empty → categorical
    # display_name: 1 distinct (constant) → not recommended
    # parser_name: 1000 distinct, 1000 non-empty (identifier, ratio=1.0) → not recommended
    # (total=1000 for all coverage computations)
    top_row = (
        5,
        1000,  # artifact: 5 distinct, 1000 non-empty
        20,
        950,  # timestamp_desc: 20 distinct, 950 non-empty
        1,
        900,  # display_name: 1 distinct → constant
        1000,
        1000,  # parser_name: 1000/1000 = 1.0 → identifier
    )
    attr_rows = [
        # (key, distinct, non_empty_count)
        ("status_code", 6, 1000),  # 6/1000=0.006 → categorical
        ("url_path", 840, 1000),  # 840/1000=0.84 < 0.9 → categorical
        ("session_id", 980, 980),  # 980/980=1.0 → identifier
    ]
    svc = _svc_with_recommend_responses(top_row, attr_rows)
    result = svc.recommend_novelty_fields("c1", ["s1"])

    assert isinstance(result, list)
    assert all(isinstance(f, NoveltyFieldInfo) for f in result)

    by_token = {f.token: f for f in result}

    # artifact: categorical cardinality, but synthetic (pipeline-added) →
    # never auto-recommended
    assert by_token["artifact"].kind == "categorical"
    assert by_token["artifact"].recommended is False

    # timestamp_desc: categorical
    assert by_token["timestamp_desc"].kind == "categorical"
    assert by_token["timestamp_desc"].recommended is True

    # display_name: constant
    assert by_token["display_name"].kind == "constant"
    assert by_token["display_name"].recommended is False

    # parser_name: identifier
    assert by_token["parser_name"].kind == "identifier"
    assert by_token["parser_name"].recommended is False

    # status_code: categorical
    assert by_token["attr:status_code"].kind == "categorical"
    assert by_token["attr:status_code"].recommended is True

    # session_id: identifier
    assert by_token["attr:session_id"].kind == "identifier"
    assert by_token["attr:session_id"].recommended is False


def test_recommend_novelty_fields_recommended_first():
    """Recommended fields should appear before non-recommended ones."""
    top_row = (
        5,
        1000,  # artifact — categorical
        20,
        950,  # timestamp_desc — categorical
        1,
        900,  # display_name — constant → not recommended
        1000,
        1000,  # parser_name — identifier → not recommended
    )
    attr_rows: list[tuple] = []
    svc = _svc_with_recommend_responses(top_row, attr_rows)
    result = svc.recommend_novelty_fields("c1", ["s1"])

    recommended = [f for f in result if f.recommended]
    not_recommended = [f for f in result if not f.recommended]
    # All recommended fields should appear before non-recommended.
    if recommended and not_recommended:
        last_rec_idx = max(i for i, f in enumerate(result) if f.recommended)
        first_not_rec_idx = min(i for i, f in enumerate(result) if not f.recommended)
        assert last_rec_idx < first_not_rec_idx


def test_recommend_novelty_fields_ranking_is_total_order():
    """Equal-coverage fields rank in a fixed order, whatever order they arrive in.

    Coverage ties are the norm, not an edge case: every field of one source
    covers that source's events exactly. Without a final tie-break the order
    came from the inventory, i.e. from a ClickHouse ``GROUP BY`` that does not
    promise a stable row order — so ``find_value_novelty`` (top 1) and
    ``find_value_combos`` (top 2) could score the same timeline on different
    fields each time it was opened. Caught by the demo coverage suite, where
    the auto-picked pair flipped between one that finds the fabricated lateral
    movement and one that finds nothing.
    """
    inventory = [
        ("attr:computer_name", 14, 371),
        ("attr:event_id", 5, 371),
        ("attr:user", 13, 879),
        ("attr:logon_type", 4, 298),
    ]
    svc = _svc([])
    ranked = [
        f.token for f in svc.recommend_novelty_fields("c1", ["s1"], total=1000, inventory=inventory)
    ]
    assert ranked == ["attr:user", "attr:computer_name", "attr:event_id", "attr:logon_type"]

    shuffled = [inventory[1], inventory[3], inventory[0], inventory[2]]
    reranked = [
        f.token for f in svc.recommend_novelty_fields("c1", ["s1"], total=1000, inventory=shuffled)
    ]
    assert reranked == ranked


def test_merged_inventory_truncation_is_deterministic():
    """The cached inventory's ``max_attr_keys`` cut must not depend on arrival order.

    ``merged_inventory`` is what actually feeds auto field selection (the live
    ``field_inventory`` scan is the fallback), so making
    ``recommend_novelty_fields`` a total order is not enough on its own: if the
    candidate set it ranks is itself truncated differently between runs, the
    auto-picked field still moves. Coverage ties at the cutoff are the norm and
    ``stats`` arrives in Postgres row order, which is not promised to be stable.
    """
    from vestigo.db.field_stats import merged_inventory

    def _stats(keys: list[str]) -> dict[str, tuple[int, dict]]:
        # One source, every attribute at identical coverage — all ties, so only
        # the tie-break decides which two survive max_attr_keys=2.
        return {
            "s1": (
                10,
                {
                    "top_level": {},
                    "attributes": {k: {"distinct": 3, "coverage": 10} for k in keys},
                },
            )
        }

    keys = ["attr_d", "attr_a", "attr_c", "attr_b"]
    inv, total = merged_inventory(_stats(keys), max_attr_keys=2)
    assert total == 10
    attrs = [tok for tok, _, _ in inv if tok.startswith("attr:")]
    assert attrs == ["attr:attr_a", "attr:attr_b"]

    reordered, _ = merged_inventory(_stats(list(reversed(keys))), max_attr_keys=2)
    assert [tok for tok, _, _ in reordered if tok.startswith("attr:")] == attrs


def test_recommend_numeric_fields_ranking_is_total_order(monkeypatch):
    """Numeric-ratio ties must not reshuffle the fields the range scan picks.

    ``find_range_violations`` takes the top ``_MAX_AUTO_SCAN_FIELDS`` of this
    list when the caller names no fields, and a ratio of exactly 1.0 is the
    common case (every value in the field parses), so without a final key the
    same timeline can be scored on different fields between runs — the same
    defect ``recommend_novelty_fields`` was fixed for.
    """
    inventory = [
        ("attr:bytes_out", 40, 900),
        ("attr:duration_ms", 30, 900),
        ("attr:port", 20, 900),
        ("attr:label", 5, 900),
    ]
    svc = _svc([])
    # Ratios keyed by token so a reordered inventory gets the same answers a
    # real probe would: 1.0 for the three numeric fields (the tie), 0.0 for the
    # non-numeric one.
    ratios = {"attr:bytes_out": 1.0, "attr:duration_ms": 1.0, "attr:port": 1.0, "attr:label": 0.0}
    monkeypatch.setattr(
        type(svc),
        "_numeric_ratio_probe",
        lambda self, case_id, source_ids, tokens, *a, **k: [ratios[t] for t in tokens],
    )

    ranked = [
        f.token for f in svc.recommend_numeric_fields("c1", ["s1"], total=1000, inventory=inventory)
    ]
    assert ranked == ["attr:bytes_out", "attr:duration_ms", "attr:port", "attr:label"]

    shuffled = [inventory[2], inventory[0], inventory[3], inventory[1]]
    reranked = [
        f.token for f in svc.recommend_numeric_fields("c1", ["s1"], total=1000, inventory=shuffled)
    ]
    assert reranked == ranked


def test_field_inventory_empty_on_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    inventory, total = svc.field_inventory("c1", ["s1"])
    assert inventory == []
    assert total == 0


def test_field_inventory_returns_unclassified_counts():
    """Every candidate field appears with raw distinct/non-empty counts —
    no novelty classification, no constant/identifier filtering."""
    top_row = (
        5,
        1000,  # artifact
        20,
        950,  # timestamp_desc
        1,
        900,  # display_name — constant, but still listed
        1000,
        1000,  # parser_name — identifier, but still listed
    )
    attr_rows = [
        ("status_code", 6, 1000),
        ("session_id", 980, 980),
    ]
    svc = _svc_with_recommend_responses(top_row, attr_rows)
    inventory, total = svc.field_inventory("c1", ["s1"])

    assert total == 1000
    assert inventory == [
        ("artifact", 5, 1000),
        ("timestamp_desc", 20, 950),
        ("display_name", 1, 900),
        ("parser_name", 1000, 1000),
        ("attr:status_code", 6, 1000),
        ("attr:session_id", 980, 980),
    ]


def test_field_inventory_skips_count_query_when_total_supplied():
    top_row = (5, 100, 2, 90, 1, 80, 100, 100)
    responses = [
        FakeQueryResult(result_rows=[top_row], column_names=["c"] * 8),
        FakeQueryResult(result_rows=[], column_names=["key", "dist", "cov_count"]),
    ]
    svc = _svc(responses)
    inventory, total = svc.field_inventory("c1", ["s1"], total=100)
    assert total == 100
    assert len(inventory) == 4
    # No count() round-trip: only the two enumeration queries ran.
    assert len(svc.ch.client._calls) == 2


# ---------------------------------------------------------------------------
# find_value_novelty — smart default via recommender
# ---------------------------------------------------------------------------


def test_value_novelty_smart_default_calls_recommender():
    """When fields=None, the recommender is invoked and its tokens are used.

    find_value_novelty computes `total` once and passes it into
    recommend_novelty_fields, which then skips its own identical count()
    query (C12) — so the total is queried only once, not twice.
    """
    total = 500
    responses = [
        # find_value_novelty's own total (reused by recommend_novelty_fields):
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        # recommend_novelty_fields (total supplied, no count() query of its own):
        FakeQueryResult(
            result_rows=[(5, 500, 1, 500, 1, 500, 1, 500)],  # 1 categorical, 3 constant/id
            column_names=["c"] * 8,
        ),  # top-level batch
        FakeQueryResult(
            result_rows=[("status_code", 6, 500)],  # attr categorical
            column_names=["key", "dist", "cov_count"],
        ),  # attribute keys
        # find_value_novelty scans (recommended fields = artifact + attr:status_code):
        # the batched attribute pass runs first, then the per-field top-level scan.
        FakeQueryResult(
            result_rows=[("status_code", "404", 2, datetime(2024, 1, 1), "evt-1")],
            column_names=["key", "val", "cnt", "first_seen", "evt_id"],
        ),  # batched attr scan (attr:status_code)
        FakeQueryResult(
            result_rows=[],
            column_names=[],
        ),  # artifact per-field scan
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty("c1", ["s1"], rarity_floor=3)
    assert result.status == "ok"
    # Findings from the attribute field should be present.
    assert len(result.results) >= 1
    # Only one total-count round trip, not one per caller (C12).
    total_count_calls = [c for c in svc.ch.client._calls if c.startswith("SELECT count()")]
    assert len(total_count_calls) == 1


def test_value_novelty_auto_mode_caps_scanned_fields():
    """C11: auto-selected fields are capped at _MAX_AUTO_SCAN_FIELDS — the cap
    now bounds the batched ARRAY JOIN key set (expansion width / LIMIT BY
    output), not a per-field round-trip count: all plain-attribute fields
    share a single batched scan."""
    from vestigo.db.anomaly_stats import _MAX_AUTO_SCAN_FIELDS, NoveltyFieldInfo

    total = 1000
    many_fields = [
        NoveltyFieldInfo(
            token=f"attr:field_{i}",
            distinct=10,
            coverage=0.9,
            kind="categorical",
            recommended=True,
        )
        for i in range(_MAX_AUTO_SCAN_FIELDS + 10)
    ]
    svc = _svc([FakeQueryResult(result_rows=[(total,)], column_names=["count()"])])
    svc.recommend_novelty_fields = lambda *a, **k: many_fields  # noqa: ARG005

    svc.find_value_novelty("c1", ["s1"], rarity_floor=3)

    # First call is the total() count; all attr fields collapse into ONE
    # batched scan whose key list carries exactly the capped field set.
    field_scan_calls = svc.ch.client._calls[1:]
    assert len(field_scan_calls) == 1
    batched_params = svc.ch.client._all_parameters[1]
    assert len(batched_params["nkeys"]) == _MAX_AUTO_SCAN_FIELDS


# ---------------------------------------------------------------------------
# exclude_event_ids suppression
# ---------------------------------------------------------------------------


def test_value_novelty_exclude_event_ids():
    """Findings whose event_id is in exclude_event_ids should be suppressed."""
    total = 100
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("malware.exe", 1, datetime(2024, 1, 1), "evt-bad"),
                ("tool.exe", 2, datetime(2024, 1, 1), "evt-ok"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_novelty(
        "c1",
        ["s1"],
        fields=["artifact"],
        exclude_event_ids={"evt-bad"},
    )
    assert result.status == "ok"
    event_ids = [r.event_id for r in result.results]
    assert "evt-bad" not in event_ids
    assert "evt-ok" in event_ids


def test_frequency_exclude_event_ids():
    """Findings whose event_id is in exclude_event_ids are suppressed after hydration."""
    svc = _svc(_make_freq_responses())

    # Hydrate with a pre-set event_id so we can test suppression.
    def _fake_hydrate(findings, *a, **kw):
        out = []
        for i, f in enumerate(findings):
            from dataclasses import replace as _replace

            out.append(_replace(f, event_id=f"evt-{i}"))
        return out

    svc._hydrate_freq_findings = _fake_hydrate  # type: ignore[method-assign]

    result = svc.find_frequency_anomalies(
        "c1",
        ["s1"],
        z_threshold=2.0,
        exclude_event_ids={"evt-0"},
    )
    assert result.status == "ok"
    assert all(f.event_id != "evt-0" for f in result.results)


def test_hydrate_freq_findings_batches_into_a_single_query():
    """_hydrate_freq_findings issues one query for all findings, not one each.

    Regression test: hydration previously ran one ClickHouse round-trip per
    finding (up to `limit`, capped at 500); it must now batch every
    (series_value, window) pair into a single grouped query.
    """
    from datetime import timedelta

    from vestigo.db.anomaly_stats import FreqFinding

    bucket_a = datetime(2024, 1, 1, 0, 0, 0, tzinfo=UTC)
    bucket_b = datetime(2024, 1, 1, 4, 0, 0, tzinfo=UTC)
    findings = [
        FreqFinding(
            series_field="artifact",
            series_value="A",
            window_start=bucket_a.isoformat(),
            window_end=(bucket_a + timedelta(hours=1)).isoformat(),
            observed=100,
            expected=10.0,
            z_score=90.0,
            score=90.0,
            event_id=None,
            event=None,
            details={},
        ),
        FreqFinding(
            series_field="artifact",
            series_value="B",
            window_start=bucket_b.isoformat(),
            window_end=(bucket_b + timedelta(hours=1)).isoformat(),
            observed=40,
            expected=10.0,
            z_score=30.0,
            score=30.0,
            event_id=None,
            event=None,
            details={},
        ),
    ]

    svc = _svc(
        [
            FakeQueryResult(
                result_rows=[
                    (bucket_a.replace(tzinfo=None), "A", "evt-a"),
                    (bucket_b.replace(tzinfo=None), "B", "evt-b"),
                ],
                column_names=["bucket", "series_val", "evt_id"],
            ),
        ]
    )
    svc.ch.events_by_id = {
        "evt-a": {"event_id": "evt-a", "message": "spike A"},
        "evt-b": {"event_id": "evt-b", "message": "spike B"},
    }

    hydrated = svc._hydrate_freq_findings(
        findings,
        "c1",
        ["s1"],
        "artifact",
        "vestigo",
        {},
        3600,
    )

    # One grouped argMin(event_id) scan + one batched get_events_by_ids.
    assert len(svc.ch.client._calls) == 1
    assert svc.ch.hydration_calls == [["evt-a", "evt-b"]]
    assert {f.event_id for f in hydrated} == {"evt-a", "evt-b"}
    by_value = {f.series_value: f for f in hydrated}
    assert by_value["A"].event["message"] == "spike A"
    assert by_value["B"].event["message"] == "spike B"


# ---------------------------------------------------------------------------
# get_timeline_midpoint
# ---------------------------------------------------------------------------


def test_get_timeline_midpoint_returns_midpoint():
    min_dt = datetime(2024, 1, 1, tzinfo=UTC)
    max_dt = datetime(2024, 1, 3, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(min_dt, max_dt)], column_names=["min", "max"]),
        ]
    )
    mid = svc.get_timeline_midpoint("c1", ["s1"])
    assert mid == datetime(2024, 1, 2, tzinfo=UTC)


def test_get_timeline_midpoint_returns_none_when_no_events():
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(None, None)], column_names=["min", "max"]),
        ]
    )
    assert svc.get_timeline_midpoint("c1", ["s1"]) is None


# ---------------------------------------------------------------------------
# find_order_violations — timestamp-order detector (D2)
# ---------------------------------------------------------------------------


def test_order_no_data():
    """Returns no_data when the source has no events."""
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_order_violations("c1", ["s1"])
    assert result.status == "no_data"
    assert result.detector == "timestamp_order"
    assert result.method == "sequential"
    assert result.results == []


# Query order (per-source scans): count, then one summary per source, then
# one detail per violating source; messages hydrated via get_events_by_ids.
_ORD_SUMMARY_COLS = ["n_viol", "max_skew"]
_ORD_DETAIL_COLS = ["event_id", "timestamp", "prev_ts", "skew", "byte_offset", "line_number"]


def test_order_no_violations():
    """Total events > 0 but zero backwards jumps → ok with empty results."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            # per-source summary: zero violations
            FakeQueryResult(result_rows=[(0, None)], column_names=_ORD_SUMMARY_COLS),
        ]
    )
    result = svc.find_order_violations("c1", ["s1"])
    assert result.status == "ok"
    assert result.results == []
    assert result.baseline_size == 100


def test_order_flags_backwards_jump_ranked_by_skew():
    """Violations returned worst-skew first, with prev/skew details."""
    ts_a = datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC)
    prev_a = datetime(2024, 1, 1, 12, 1, 5, tzinfo=UTC)  # 60s ahead
    ts_b = datetime(2024, 1, 1, 12, 0, 30, tzinfo=UTC)
    prev_b = datetime(2024, 1, 1, 12, 0, 35, tzinfo=UTC)  # 5s ahead
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(2, 60.0)], column_names=_ORD_SUMMARY_COLS),
            FakeQueryResult(
                result_rows=[
                    ("evt-a", ts_a, prev_a, 60.0, 100, 3),
                    ("evt-b", ts_b, prev_b, 5.0, 250, 8),
                ],
                column_names=_ORD_DETAIL_COLS,
            ),
        ]
    )
    svc.ch.events_by_id = {"evt-a": {"message": "record a"}}
    result = svc.find_order_violations("c1", ["s1"], min_skew_seconds=1.0)
    assert result.status == "ok"
    assert [f.event_id for f in result.results] == ["evt-a", "evt-b"]
    assert result.total_findings == 2
    worst = result.results[0]
    assert worst.skew_seconds == 60.0
    assert worst.score == 60.0
    assert worst.byte_offset == 100
    assert worst.prev_timestamp == prev_a.isoformat()
    assert worst.timestamp == ts_a.isoformat()
    assert worst.details["source_total_violations"] == 2
    assert worst.details["source_max_skew"] == 60.0
    assert worst.details["min_skew_seconds"] == 1.0
    assert worst.event["byte_offset"] == 100
    # message comes from the post-scan hydration, one batched fetch.
    assert worst.event["message"] == "record a"
    assert svc.ch.hydration_calls == [["evt-a", "evt-b"]]


def test_order_min_skew_bound_as_param():
    """min_skew_seconds is bound into both summary and detail queries."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1, 3.0)], column_names=_ORD_SUMMARY_COLS),
            FakeQueryResult(
                result_rows=[
                    (
                        "evt-a",
                        datetime(2024, 1, 1, tzinfo=UTC),
                        datetime(2024, 1, 1, 0, 0, 3, tzinfo=UTC),
                        3.0,
                        10,
                        1,
                    )
                ],
                column_names=_ORD_DETAIL_COLS,
            ),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_order_violations("c1", ["s1"], min_skew_seconds=2.5)
    params = svc.ch.client._all_parameters
    # params[0] = count(); [1] = summary(s1); [2] = detail(s1)
    assert params[1]["skew"] == 2.5
    assert params[2]["skew"] == 2.5
    summary_sql, detail_sql = svc.ch.client.full_queries[1], svc.ch.client.full_queries[2]
    # ClickHouse can't spill a window-function sort to disk, so the scans are
    # per source (bounded sort) and never drag `message` through it — that
    # combination is what blew the memory cap on a 300M-row case.
    for sql in (summary_sql, detail_sql):
        assert "message" not in sql
        assert "source_id = {sid:String}" in sql
        assert "PARTITION BY" not in sql
        # The shared guardrails spill sorts too, not just GROUP BYs.
        assert "max_bytes_before_external_sort" in sql
    assert "toString(event_id)" not in summary_sql
    assert "toString(event_id) AS event_id" in detail_sql


def test_order_excludes_normal_marked_events():
    """Events marked normal are suppressed before the limit is applied."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(2, 60.0)], column_names=_ORD_SUMMARY_COLS),
            FakeQueryResult(
                result_rows=[
                    (
                        "evt-a",
                        datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC),
                        datetime(2024, 1, 1, 12, 1, 5, tzinfo=UTC),
                        60.0,
                        100,
                        3,
                    ),
                    (
                        "evt-b",
                        datetime(2024, 1, 1, 12, 0, 30, tzinfo=UTC),
                        datetime(2024, 1, 1, 12, 0, 35, tzinfo=UTC),
                        5.0,
                        250,
                        8,
                    ),
                ],
                column_names=_ORD_DETAIL_COLS,
            ),
        ]
    )
    result = svc.find_order_violations("c1", ["s1"], exclude_event_ids={"evt-a"})
    assert [f.event_id for f in result.results] == ["evt-b"]


# ---------------------------------------------------------------------------
# find_value_combos — value-combo detector (D1)
# ---------------------------------------------------------------------------


def test_value_combo_requires_two_explicit_fields():
    """Explicit single-field selection is rejected."""
    import pytest

    svc = _svc([FakeQueryResult(result_rows=[(100,)], column_names=["count()"])])
    with pytest.raises(ValueError):
        svc.find_value_combos("c1", ["s1"], fields=["artifact"])


def test_value_combo_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_value_combos("c1", ["s1"], fields=["artifact", "display_name"])
    assert result.status == "no_data"
    assert result.detector == "value_combo"


def test_value_combo_self_baseline_returns_rare_combos():
    """Rare (field-a, field-b) combinations, rarest first, with surprise score."""
    import math

    total = 1000
    fs = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("login_ok", "03:00", 1, fs, "evt-a"),
                ("login_ok", "09:00", 3, fs, "evt-b"),
            ],
            column_names=["v0", "v1", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos(
        "c1", ["s1"], fields=["attr:action", "attr:hour"], rarity_floor=3
    )
    assert result.status == "ok"
    assert result.detector == "value_combo"
    # Rarest (count 1) first.
    assert result.results[0].values == ["login_ok", "03:00"]
    assert result.results[0].count == 1
    assert result.results[0].fields == ["attr:action", "attr:hour"]
    assert result.results[0].score == round(-math.log(1 / total), 4)
    assert result.results[0].event_id == "evt-a"
    assert result.results[0].details["detector"] == "value_combo"


def test_value_combo_binds_distinct_prefixes_for_attr_fields():
    """Two attribute-key fields bind to fk0 / fk1 without colliding."""
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("a", "b", 1, datetime(2024, 1, 1, tzinfo=UTC), "e")],
                column_names=["v0", "v1", "cnt", "first_seen", "evt_id"],
            ),
        ]
    )
    svc.find_value_combos("c1", ["s1"], fields=["attr:action", "attr:hour"])
    combo_params = svc.ch.client._all_parameters[1]
    assert combo_params["fk0"] == "action"
    assert combo_params["fk1"] == "hour"


def test_value_combo_temporal_flags_new_combos():
    """Temporal mode flags combos absent from baseline, present in a suspect window."""
    total = 500
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
    )
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        # window totals: baseline_total, w0_total
        FakeQueryResult(result_rows=[(300, 80)], column_names=["bl_total", "w0_total"]),
        FakeQueryResult(
            result_rows=[
                # v0, v1, baseline_cnt, w0_cnt, w0_first, w0_evt, hits, best
                ("admin", "10.0.0.9", 0, 2, fs, "evt-x", 1, 0.025),
            ],
            column_names=[
                "v0",
                "v1",
                "baseline_cnt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
                "hits",
                "best",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos("c1", ["s1"], fields=["attr:user", "attr:ip"], windows=windows)
    assert result.method == "temporal"
    assert result.baseline_size == 300
    assert result.results[0].values == ["admin", "10.0.0.9"]
    assert result.results[0].count == 2
    assert result.results[0].details["baseline_size"] == 300
    assert result.results[0].details["window_total_events"] == 80
    # The combo's allowlist key flattens the tuple reversibly.
    assert result.results[0].details["allowlist_value"] == "admin\x1f10.0.0.9"


def test_value_combo_auto_insufficient_when_fewer_than_two_recommended():
    """Auto mode with <2 recommended fields returns insufficient_data, not an error."""
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        # field_inventory: top-level agg (4 candidate cols → all constant/identifier)
        FakeQueryResult(
            result_rows=[(1, 100, 1, 100, 1, 100, 1, 100)],
            column_names=[
                "artifact_dist",
                "artifact_cov",
                "timestamp_desc_dist",
                "timestamp_desc_cov",
                "display_name_dist",
                "display_name_cov",
                "parser_name_dist",
                "parser_name_cov",
            ],
        ),
        # attr keys: none
        FakeQueryResult(result_rows=[], column_names=["key", "dist", "cov_count"]),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos("c1", ["s1"], fields=None)
    assert result.status == "insufficient_data"


def test_value_combo_excludes_normal_marked_events():
    total = 1000
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("a", "1", 1, fs, "evt-keep"),
                ("b", "2", 1, fs, "evt-drop"),
            ],
            column_names=["v0", "v1", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_value_combos(
        "c1", ["s1"], fields=["attr:x", "attr:y"], exclude_event_ids={"evt-drop"}
    )
    assert [f.event_id for f in result.results] == ["evt-keep"]


def test_value_combo_total_is_the_sql_count_not_the_page():
    """`total_findings` is what the scan counted, not how many rows came back."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    rows = [(f"a{i}", "b", 1, fs, f"e{i}") for i in range(50)]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=rows,
                column_names=["v0", "v1", "cnt", "first_seen", "evt_id"],
            ),
        ],
        totals=[4000],
    )
    result = svc.find_value_combos("c1", ["s1"], fields=["attr:a", "attr:b"], limit=50)
    assert len(result.results) == 50
    assert result.total_findings == 4000
    assert result.total_findings_exact is True


def test_value_combo_suppression_is_bound_into_the_sql():
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_value_combos(
        "c1",
        ["s1"],
        fields=["attr:a", "attr:b"],
        allowlist={("attr:a,attr:b", "x\x1fy")},
        exclude_event_ids={"e9"},
    )
    sql = client.full_queries[-1]
    p = client._all_parameters[-1]
    _assert_paired(
        sql,
        client.total_queries[-1],
        "NOT has({allow:Array(String)}, concat(v0, '\x1f', v1))",
        "NOT has({excl:Array(String)}, evt_id)",
    )
    assert p["allow"] == ["x\x1fy"]
    assert p["excl"] == ["e9"]
    assert result.total_findings == 0
    assert result.total_findings_exact is True


def test_value_combo_oversized_exclusion_marks_the_total_inexact():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    big = {f"e{i}" for i in range(_SQL_SUPPRESSION_MAX + 1)}
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("a", "b", 1, fs, "e1"), ("c", "d", 1, fs, "zz")],
                column_names=["v0", "v1", "cnt", "first_seen", "evt_id"],
            ),
        ],
        totals=[7],
    )
    result = svc.find_value_combos("c1", ["s1"], fields=["attr:a", "attr:b"], exclude_event_ids=big)
    # The page is still filtered; the count could not be.
    assert [f.event_id for f in result.results] == ["zz"]
    assert result.total_findings == 7
    assert result.total_findings_exact is False
    assert any("page only" in w for w in result.warnings)


def test_value_combo_temporal_orders_by_best_window_score():
    """The page is the true top-N by the score the finding reports."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(300, 80, 20)], column_names=["bl", "w0", "w1"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    windows = AnalysisWindows(
        baseline=TimeWindow(
            "baseline", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC)
        ),
        suspects=(
            TimeWindow("s0", datetime(2024, 1, 3, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)),
            TimeWindow("s1", datetime(2024, 1, 5, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC)),
        ),
    )
    svc.find_value_combos("c1", ["s1"], fields=["attr:a", "attr:b"], windows=windows)
    sql = client.full_queries[-1]
    p = client._all_parameters[-1]
    assert "ORDER BY best ASC" in sql
    assert client.total_queries[-1].startswith("SELECT sum(hits) FROM (")
    assert "(w0_cnt > 0) + (w1_cnt > 0) AS hits" in sql
    assert p["w0_total"] == 80.0
    assert p["w1_total"] == 20.0


def test_value_combo_temporal_total_counts_window_hits_not_groups():
    """One group hit in two suspect windows is two findings — the total says so."""
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    windows = AnalysisWindows(
        baseline=TimeWindow(
            "baseline", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC)
        ),
        suspects=(
            TimeWindow("s0", datetime(2024, 1, 3, tzinfo=UTC), datetime(2024, 1, 4, tzinfo=UTC)),
            TimeWindow("s1", datetime(2024, 1, 5, tzinfo=UTC), datetime(2024, 1, 6, tzinfo=UTC)),
        ),
    )
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(300, 80, 20)], column_names=["bl", "w0", "w1"]),
            FakeQueryResult(
                result_rows=[("a", "b", 0, 2, fs, "e0", 1, fs, "e1", 2, 0.025)],
                column_names=[
                    "v0",
                    "v1",
                    "baseline_cnt",
                    "w0_cnt",
                    "w0_first",
                    "w0_evt",
                    "w1_cnt",
                    "w1_first",
                    "w1_evt",
                    "hits",
                    "best",
                ],
            ),
        ],
        totals=[9],
    )
    result = svc.find_value_combos("c1", ["s1"], fields=["attr:a", "attr:b"], windows=windows)
    assert len(result.results) == 2
    assert result.total_findings == 9


# ---------------------------------------------------------------------------
# find_range_violations — numeric-range detector (D4)
# ---------------------------------------------------------------------------


def test_range_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"])
    assert result.status == "no_data"
    assert result.detector == "numeric_range"


def test_range_insufficient_when_baseline_too_small():
    """A field with < _MIN_RANGE_BASELINE numeric samples is skipped."""
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        # stats: only 5 numeric samples → below the floor
        FakeQueryResult(result_rows=[(10.0, 20.0, 5)], column_names=["q1", "q3", "n"]),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"])
    assert result.status == "insufficient_data"


def test_range_self_baseline_iqr_flags_outliers():
    """Self-baseline uses a Tukey fence; values outside [q1-1.5IQR, q3+1.5IQR] flag."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    # q1=100, q3=200 → IQR=100 → band [-50, 350]. Value 9000 is far above.
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(100.0, 200.0, 500)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[(9000.0, 2, fs, "evt-hi")],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"])
    assert result.status == "ok"
    assert result.method == "iqr"
    f = result.results[0]
    assert f.value == 9000.0
    assert f.direction == "above"
    assert f.lower == -50.0
    assert f.upper == 350.0
    # excess = 9000 - 350 = 8650; width = 400 → score = 21.625
    assert f.score == round(8650.0 / 400.0, 4)
    assert f.details["q1"] == 100.0
    assert f.details["baseline_n"] == 500


def test_range_temporal_uses_baseline_minmax():
    """Temporal mode learns exact min/max from the baseline window and
    attributes the violation to the suspect window it fell in."""
    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
        label="spike",
    )
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # baseline min=10, max=500, n=300
        FakeQueryResult(result_rows=[(10.0, 500.0, 300)], column_names=["lo", "hi", "n"]),
        FakeQueryResult(
            # val, cnt, first_seen, evt_id, win_idx
            result_rows=[(9999.0, 1, fs, "evt-x", 0)],
            column_names=["val", "cnt", "first_seen", "evt_id", "win_idx"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes"], windows=windows)
    assert result.method == "temporal-range"
    f = result.results[0]
    assert f.lower == 10.0
    assert f.upper == 500.0
    assert f.direction == "above"
    assert f.details["baseline_min"] == 10.0
    assert f.details["baseline_max"] == 500.0
    assert f.details["window_label"] == "spike"


def test_range_excludes_normal_marked_events():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(100.0, 200.0, 500)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[
                (9000.0, 1, fs, "evt-drop"),
                (8000.0, 1, fs, "evt-keep"),
            ],
            column_names=["val", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_range_violations(
        "c1", ["s1"], fields=["attr:bytes"], exclude_event_ids={"evt-drop"}
    )
    assert [f.event_id for f in result.results] == ["evt-keep"]


def test_range_total_is_the_sql_count_and_page_is_at_least_the_limit():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(100.0, 200.0, 500)], column_names=["q1", "q3", "n"]),
            FakeQueryResult(
                result_rows=[(9000.0 + i, 1, fs, f"e{i}") for i in range(80)],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
        ],
        totals=[640],
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_range_violations(
        "c1",
        ["s1"],
        fields=["attr:bytes"],
        limit=80,
        allowlist={("attr:bytes", "9000.0")},
        exclude_event_ids={"e5"},
    )
    assert len(result.results) == 78  # page-level pass drops the two the fake ignored
    assert result.total_findings == 640
    assert result.total_findings_exact is True
    sql = client.full_queries[2]
    p = client._all_parameters[2]
    assert p["plim"] == 80
    # A float key: `str(9000.0)` and ClickHouse's `toString(9000.)` disagree, so
    # the allowlist binds as numbers and compares as numbers.
    _assert_paired(
        sql,
        client.total_queries[-1],
        "NOT has({allow:Array(Float64)}, val)",
        "NOT has({excl:Array(String)}, evt_id)",
    )
    assert p["allow"] == [9000.0]


def test_range_total_sums_across_fields():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(100.0, 200.0, 500)], column_names=["q1", "q3", "n"]),
            FakeQueryResult(
                result_rows=[(9000.0, 1, fs, "e1")],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
            FakeQueryResult(result_rows=[(1.0, 2.0, 500)], column_names=["q1", "q3", "n"]),
            FakeQueryResult(
                result_rows=[(50.0, 1, fs, "e2")],
                column_names=["val", "cnt", "first_seen", "evt_id"],
            ),
        ],
        totals=[30, 12],
    )
    result = svc.find_range_violations("c1", ["s1"], fields=["attr:bytes", "attr:ms"])
    assert result.total_findings == 42


def test_recommend_numeric_fields_filters_by_ratio():
    """Only fields whose values mostly parse as numbers are recommended."""
    # inventory: (token, distinct, non_empty_count); total passed explicitly.
    inventory = [("attr:bytes", 50, 100), ("attr:user", 40, 100)]
    responses = [
        # probe: bytes 98/100 numeric, user 3/100 numeric
        FakeQueryResult(
            result_rows=[(98, 100, 3, 100)],
            column_names=["num0", "ne0", "num1", "ne1"],
        ),
    ]
    svc = _svc(responses)
    fields = svc.recommend_numeric_fields("c1", ["s1"], total=100, inventory=inventory)
    by_token = {f.token: f for f in fields}
    assert by_token["attr:bytes"].recommended is True
    assert by_token["attr:bytes"].numeric_ratio == 0.98
    assert by_token["attr:user"].recommended is False


def test_heavy_detector_scans_carry_memory_settings():
    """Every whole-corpus detector scan must carry the shared SETTINGS clause
    (external GROUP BY spill + per-query memory cap + thread cap) — a scan
    without it trusts the server-wide limit and can take the box down on a
    300M-row case."""
    from vestigo.db._scan import heavy_scan_settings, scan_fanout

    class _RecordingClient(FakeClient):
        def __init__(self) -> None:
            super().__init__([])
            self.full_queries: list[str] = []

        def query(self, sql: str, parameters: dict | None = None) -> FakeQueryResult:
            self.full_queries.append(sql)
            if sql.strip().startswith("SELECT count()"):
                return FakeQueryResult(result_rows=[(100,)], column_names=["count()"])
            return FakeQueryResult(result_rows=[], column_names=[])

    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    store = FakeClickHouseStore(FakeClient([]))
    client = _RecordingClient()
    store.client = client
    svc.ch = store

    svc.find_value_novelty("c1", ["s1"], fields=["artifact"])
    svc.find_value_combos("c1", ["s1"], fields=["artifact", "timestamp_desc"])
    svc.find_charset_novelty("c1", ["s1"], fields=["artifact"])
    svc.find_entropy_outliers("c1", ["s1"], fields=["artifact"])
    svc.field_inventory("c1", ["s1"], total=100)

    def _is_total(q: str) -> bool:
        return ") AS scanned" in q

    def _is_count_probe(q: str) -> bool:
        # The bare `SELECT count() FROM db.events ...` size probe — not a
        # companion total, which also starts with `SELECT count()` but wraps
        # the page's core as `(...) AS scanned`.
        return q.strip().startswith("SELECT count()") and not _is_total(q)

    scans = [
        q
        for q in client.full_queries
        if not _is_count_probe(q) and "min(timestamp), max(timestamp)" not in q
    ]
    assert scans
    solo = _max_memory(heavy_scan_settings())
    with scan_fanout(2):
        halved = _max_memory(heavy_scan_settings())
    # The paged detectors issue a page and a companion count under one slot;
    # both carry half the cap. Everything else runs alone and carries it whole.
    totals = [q for q in scans if _is_total(q)]
    assert totals, "no paged scan ran under this fake"
    for sql in scans:
        assert "max_bytes_before_external_group_by" in sql, sql[:120]
        assert _max_memory(sql) in (solo, halved), sql[-160:]
    for sql in totals:
        assert _max_memory(sql) == halved, sql[-160:]
    # Every total has its page beside it at the same halved cap — no page at
    # the full cap next to a count at half, which is what the old assertion
    # would have accepted.
    assert sum(_max_memory(q) == halved for q in scans) == 2 * len(totals)


# ---------------------------------------------------------------------------
# find_charset_novelty — charset detector (D3)
# ---------------------------------------------------------------------------


def test_charset_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"])
    assert result.status == "no_data"
    assert result.detector == "charset"


def test_charset_insufficient_when_baseline_too_small():
    """A field with < _MIN_CHARSET_BASELINE distinct values is skipped."""
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        # per-char distinct-value counts + the total distinct-value count folded
        # into the same scan (3rd column). Only 5 distinct values → below floor.
        FakeQueryResult(result_rows=[("a", 5, 5), ("b", 5, 5)], column_names=["c", "n", "n_vals"]),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"])
    assert result.status == "insufficient_data"


def test_charset_skips_huge_alphabet():
    """A reference charset larger than _MAX_CHARSET_SIZE (free text in large
    scripts) is skipped — "novel character" is meaningless there."""
    big = [(chr(0x4E00 + i), 50, 80) for i in range(5001)]
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        FakeQueryResult(result_rows=big, column_names=["c", "n", "n_vals"]),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:msg"])
    assert result.status == "insufficient_data"


def test_charset_self_baseline_flags_rare_char():
    """Self mode: chars in ≤ rarity_floor distinct values are rare; values
    containing them flag with -log(n_vals_with_char / n_vals) surprise."""
    import math

    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # 'a'/'b' common (90/85 distinct values), NUL byte rare (1 value); the
        # total distinct-value count (100) is folded in as the 3rd column.
        FakeQueryResult(
            result_rows=[("a", 90, 100), ("b", 85, 100), ("\x00", 1, 100)],
            column_names=["c", "n", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[("ab\x00ab", ["\x00"], 2, fs, "evt-nul", 0.0)],
            column_names=["val", "novel", "cnt", "first_seen", "evt_id", "score"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"])
    assert result.status == "ok"
    assert result.method == "rare-chars"
    f = result.results[0]
    assert f.field == "attr:user"
    assert f.novel_chars == ["\x00"]
    assert f.count == 2
    assert f.score == round(-math.log(1 / 100), 4)
    assert f.details["codepoints"] == ["U+0000"]
    assert f.details["rarity_floor"] == 3
    assert f.details["char_value_counts"] == {"\x00": 1}
    # The reference-set parameter must exclude the rare char.
    base_params = [p["base"] for p in svc.ch.client._all_parameters if "base" in p]
    assert base_params == [["a", "b"]]


def test_charset_temporal_flags_never_seen_chars_and_guards_sentinel():
    """Temporal mode: reference set = baseline-window charset; suspect-window
    values with never-seen chars flag, with the year-2299 sentinel excluded."""
    import math

    from vestigo.db._dt import VESTIGO_NOT_SENTINEL_SQL

    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
        label="win",
    )
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # baseline charset over 50 distinct baseline values
        FakeQueryResult(
            result_rows=[(c, 1, 50) for c in "abcdefghij"],
            column_names=["c", "n_vals_with_c", "n_vals"],
        ),
        FakeQueryResult(
            # val, novel, cnt, first_seen, evt_id, win_idx
            result_rows=[("ab☃cd", ["☃"], 1, fs, "evt-snow", 0, 0.0)],
            column_names=[
                "val",
                "novel",
                "cnt",
                "first_seen",
                "evt_id",
                "win_idx",
                "score",
            ],
        ),
    ]
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    client = RecordingClient(responses)
    svc.ch = FakeClickHouseStore(FakeClient([]))
    svc.ch.client = client
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], windows=windows)
    assert result.status == "ok"
    assert result.method == "temporal-charset"
    f = result.results[0]
    assert f.novel_chars == ["☃"]
    assert f.details["window_label"] == "win"
    # Never seen in baseline → +1-smoothed surprise over 50 distinct values.
    assert f.score == round(math.log(51), 4)
    baseline_sql = client.full_queries[1]
    detect_sql = client.full_queries[2]
    # Baseline learns from the baseline window; detect scans the suspect union.
    assert "{b1:String}" in baseline_sql
    assert "{w0s:String}" in detect_sql
    assert VESTIGO_NOT_SENTINEL_SQL in detect_sql


def test_charset_excludes_normal_marked_events_and_limits():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[("a", 90, 100), ("$", 1, 100), ("%", 1, 100)],
            column_names=["c", "n", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[
                ("x$", ["$"], 1, fs, "evt-drop", 0.0),
                ("y%", ["%"], 1, fs, "evt-keep", 0.0),
            ],
            column_names=["val", "novel", "cnt", "first_seen", "evt_id", "score"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty(
        "c1", ["s1"], fields=["attr:user"], exclude_event_ids={"evt-drop"}, limit=1
    )
    assert [f.event_id for f in result.results] == ["evt-keep"]
    # Hydration happens once, on the surviving slice only.
    assert svc.ch.hydration_calls == [["evt-keep"]]


def test_charset_orders_by_score_in_sql_and_counts_exactly():
    """The page is the true top-N by the score the finding reports, not by how
    many novel characters a value happens to contain."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("a", 90, 100), ("$", 2, 100), ("%", 1, 100)],
                column_names=["c", "n", "n_vals"],
            ),
            FakeQueryResult(
                result_rows=[(f"x{i}%", ["%"], 1, fs, f"e{i}", 4.6151) for i in range(50)],
                column_names=["val", "novel", "cnt", "first_seen", "evt_id", "score"],
            ),
        ],
        totals=[730],
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_charset_novelty(
        "c1",
        ["s1"],
        fields=["attr:user"],
        limit=80,
        allowlist={("attr:user", "ok$")},
        exclude_event_ids={"e-x"},
    )
    assert len(result.results) == 50
    assert result.total_findings == 730
    assert result.total_findings_exact is True
    sql = client.full_queries[2]
    p = client._all_parameters[2]
    assert p["plim"] == 80
    assert "ORDER BY score DESC, cnt ASC" in sql
    assert client.total_queries[-1].startswith("SELECT count() FROM (")
    # The rare characters and their counts ride in as parallel arrays so the
    # SQL sums the same per-character surprise Python reports.
    assert p["rc"] == ["$", "%"]
    assert p["rn"] == [2.0, 1.0]
    assert p["nv"] == 100.0
    assert "log({nv:Float64} + 1)" in sql
    _assert_paired(
        sql,
        client.total_queries[-1],
        "NOT has({allow:Array(String)}, val)",
        "NOT has({excl:Array(String)}, evt_id)",
    )
    assert p["allow"] == ["ok$"]


def test_charset_grouped_binds_per_group_rarity_for_the_sql_score():
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            # per-group learn rows: grp, c, n, n_vals
            FakeQueryResult(
                result_rows=[
                    ("h1", "a", 90, 100),
                    ("h1", "$", 1, 100),
                    ("h2", "a", 40, 50),
                    ("h2", "%", 2, 50),
                ],
                column_names=["grp", "c", "n", "n_vals"],
            ),
            FakeQueryResult(result_rows=[], column_names=[]),  # probe
            FakeQueryResult(result_rows=[], column_names=[]),  # violations
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    sql = client.full_queries[-1]
    p = client._all_parameters[-1]
    assert "ORDER BY score DESC, cnt ASC" in sql
    assert "LIMIT {plim:UInt32} BY grp" in sql
    # The grouped page is paged per group; its total is still one number over
    # the whole post-HAVING set.
    total_sql = client.total_queries[-1]
    assert total_sql.startswith("SELECT count() FROM (")
    assert "LIMIT" not in total_sql.rsplit(") AS scanned", 1)[1]
    assert p["grps"] == ["h1", "h2"]
    assert p["rcs"] == [["$"], ["%"]]
    assert p["rns"] == [[1.0], [2.0]]
    assert p["nvs"] == [100.0, 50.0]
    assert "{fb_rc:Array(String)}" in sql and "{fb_nv:Float64}" in sql


# ---------------------------------------------------------------------------
# find_entropy_outliers — entropy detector (D5)
# ---------------------------------------------------------------------------


def test_entropy_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"])
    assert result.status == "no_data"
    assert result.detector == "entropy"


def test_entropy_insufficient_when_baseline_too_small():
    """A field with < _MIN_ENTROPY_BASELINE qualifying distinct values is skipped."""
    responses = [
        FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 3.0, 5)], column_names=["q1", "q3", "n"]),
    ]
    svc = _svc(responses)
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"])
    assert result.status == "insufficient_data"


def test_entropy_self_baseline_iqr_flags_both_directions():
    """Self mode: Tukey fence over corpus entropies; score = excess / width."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    # q1=2.0, q3=3.0 → IQR=1.0 → band [0.5, 4.5], width 4.0.
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 3.0, 200)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[
                ("kq3v9xz2m8w1", 5.5, 3, fs, "evt-dga"),
                ("aaaaaaaaaaaa", 0.1, 7, fs, "evt-pad"),
            ],
            column_names=["val", "ent", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"])
    assert result.status == "ok"
    assert result.method == "iqr"
    hi, lo = result.results[0], result.results[1]
    assert hi.direction == "above"
    assert hi.entropy == 5.5
    assert hi.lower == 0.5
    assert hi.upper == 4.5
    # excess = 5.5 - 4.5 = 1.0; width = 4.0 → 0.25
    assert hi.score == 0.25
    assert lo.direction == "below"
    # excess = 0.5 - 0.1 = 0.4; width = 4.0 → 0.1
    assert lo.score == 0.1
    assert hi.details["q1"] == 2.0
    assert hi.details["baseline_n"] == 200


def test_entropy_bigram_variant_learns_a_surprisal_table_and_binds_it():
    """D11: the bigram variant learns pair totals + the top-K table from the
    reference distinct values, binds them into both scans, and reports the
    statistic under `bigram-iqr` with the table's provenance in details."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            # pair totals: N = 100 pairs, V = 3 distinct
            FakeQueryResult(result_rows=[(100, 3)], column_names=["n_pairs", "v"]),
            # the table, most frequent first
            FakeQueryResult(
                result_rows=[("th", 60), ("he", 30), ("er", 10)], column_names=["bg", "c"]
            ),
            # band over per-value mean surprisal
            FakeQueryResult(result_rows=[(1.0, 2.0, 200)], column_names=["q1", "q3", "n"]),
            FakeQueryResult(
                result_rows=[("kqzvxwmjpl", 6.2, 2, fs, "evt-dga")],
                column_names=["val", "ent", "cnt", "first_seen", "evt_id"],
            ),
        ],
        totals=[1],
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"], variant="bigram")
    assert result.status == "ok"
    assert result.method == "bigram-iqr"
    f = result.results[0]
    assert f.direction == "above"
    assert f.details["variant"] == "bigram"
    assert f.details["bigram_pairs"] == 100
    assert f.details["bigram_distinct"] == 3
    assert f.details["bigram_table"] == 3
    # Add-one smoothing over N + V + 1 = 104: unseen = -log2(1/104).
    assert abs(f.details["bigram_unseen_surprisal"] - math.log2(104)) < 1e-3
    totals_sql, table_sql, band_sql, viol_sql = client.full_queries[1:5]
    assert "ngrams(val, 2)" in totals_sql and "GROUP BY bg" in table_sql
    assert "{bgcap:UInt32}" in table_sql
    for sql in (band_sql, viol_sql):
        assert "transform(g, {bgk:Array(String)}, {bgv:Array(Float64)}, {bgu:Float64})" in sql
        assert "arrayReduce('entropy'" not in sql
    bound = client._all_parameters[3]
    assert bound["bgk"] == ["th", "he", "er"]
    assert abs(bound["bgv"][0] - (-math.log2(61 / 104))) < 1e-9
    assert abs(bound["bgu"] - math.log2(104)) < 1e-9
    assert client._all_parameters[4]["bgk"] == ["th", "he", "er"]


def test_entropy_bigram_variant_warns_when_the_table_is_capped():
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(100_000, 9000)], column_names=["n_pairs", "v"]),
            FakeQueryResult(result_rows=[("th", 60)], column_names=["bg", "c"]),
            FakeQueryResult(result_rows=[(1.0, 2.0, 200)], column_names=["q1", "q3", "n"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ],
        totals=[0],
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"], variant="bigram")
    assert any("capped at 1 of 9000" in w for w in result.warnings)
    assert result.results == []
    assert result.status == "ok"
    with pytest.raises(ValueError, match="variant"):
        svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"], variant="trigram")


def test_entropy_shannon_variant_binds_no_table():
    """The default stays byte-for-byte the pre-D11 scan: no table pass, no bound arrays."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(2.0, 3.0, 200)], column_names=["q1", "q3", "n"]),
            FakeQueryResult(
                result_rows=[("kq3v9xz2m8w1", 5.5, 3, fs, "evt-dga")],
                column_names=["val", "ent", "cnt", "first_seen", "evt_id"],
            ),
        ],
        totals=[1],
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"])
    assert result.method == "iqr"
    assert result.results[0].details["variant"] == "shannon"
    assert len(client.full_queries) == 3
    assert "bgk" not in client._all_parameters[1]
    assert "arrayReduce('entropy'" in client.full_queries[1]


def test_entropy_temporal_learns_band_from_baseline_and_guards_sentinel():
    """Temporal mode: fence from the baseline window only; suspect-window
    values scored with the sentinel excluded; min-length clause applies to
    baseline and detect alike."""

    from vestigo.db._dt import VESTIGO_NOT_SENTINEL_SQL

    windows = _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
        datetime(2024, 1, 5, tzinfo=UTC),
        label="win",
    )
    fs = datetime(2024, 1, 3, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 2.5, 100)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            # val, ent, cnt, first_seen, evt_id, win_idx
            result_rows=[("x9k2q8vz", 4.9, 1, fs, "evt-hi", 0)],
            column_names=["val", "ent", "cnt", "first_seen", "evt_id", "win_idx"],
        ),
    ]
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    client = RecordingClient(responses)
    svc.ch = FakeClickHouseStore(FakeClient([]))
    svc.ch.client = client
    result = svc.find_entropy_outliers("c1", ["s1"], fields=["attr:host"], windows=windows)
    assert result.status == "ok"
    assert result.method == "temporal-iqr"
    f = result.results[0]
    assert f.direction == "above"
    assert f.details["window_label"] == "win"
    baseline_sql = client.full_queries[1]
    detect_sql = client.full_queries[2]
    assert "{b1:String}" in baseline_sql
    assert "{w0s:String}" in detect_sql
    assert VESTIGO_NOT_SENTINEL_SQL in detect_sql
    assert "lengthUTF8" in baseline_sql
    assert "lengthUTF8" in detect_sql


def test_entropy_excludes_normal_marked_events():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2.0, 3.0, 200)], column_names=["q1", "q3", "n"]),
        FakeQueryResult(
            result_rows=[
                ("zzzz11119999", 5.9, 1, fs, "evt-drop"),
                ("q8m2x7c4v1n6", 5.5, 1, fs, "evt-keep"),
            ],
            column_names=["val", "ent", "cnt", "first_seen", "evt_id"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_entropy_outliers(
        "c1", ["s1"], fields=["attr:host"], exclude_event_ids={"evt-drop"}
    )
    assert [f.event_id for f in result.results] == ["evt-keep"]
    assert svc.ch.hydration_calls == [["evt-keep"]]


def test_entropy_total_is_the_sql_count_and_suppression_is_bound():
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(2.0, 3.0, 200)], column_names=["q1", "q3", "n"]),
            FakeQueryResult(
                result_rows=[(f"v{i:011d}", 5.5, 1, fs, f"e{i}") for i in range(50)],
                column_names=["val", "ent", "cnt", "first_seen", "evt_id"],
            ),
        ],
        totals=[900],
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_entropy_outliers(
        "c1",
        ["s1"],
        fields=["attr:host"],
        limit=80,
        allowlist={("attr:host", "known-random")},
        exclude_event_ids={"e-x"},
    )
    assert len(result.results) == 50
    assert result.total_findings == 900
    assert result.total_findings_exact is True
    sql = client.full_queries[2]
    p = client._all_parameters[2]
    assert p["plim"] == 80
    _assert_paired(
        sql,
        client.total_queries[-1],
        "NOT has({allow:Array(String)}, val)",
        "NOT has({excl:Array(String)}, evt_id)",
    )
    assert p["allow"] == ["known-random"]


# ---------------------------------------------------------------------------
# proportion_shift — statistics helpers
# ---------------------------------------------------------------------------


def test_g_statistic_hand_computed():
    """G for a hand-computable 2×2 table, and 0 for identical proportions."""
    # rows (baseline 10/1000, window 40/1000): expected cells 25/975 each row.
    # G = 2·(10·ln(10/25) + 990·ln(990/975) + 40·ln(40/25) + 960·ln(960/975))
    assert abs(_g_statistic(10, 990, 40, 960) - 19.7360) < 1e-3
    # Identical proportions carry zero evidence.
    assert _g_statistic(10, 990, 10, 990) == 0.0
    # Zero cells contribute nothing rather than NaN.
    assert _g_statistic(0, 0, 0, 0) == 0.0
    assert _g_statistic(200, 800, 0, 500) > 0


def test_chi2_sf_df1_known_values():
    """The erfc closed form matches the classic df=1 chi² critical values."""
    assert abs(_chi2_sf_df1(3.841459) - 0.05) < 1e-4
    assert abs(_chi2_sf_df1(6.634897) - 0.01) < 1e-4
    assert _chi2_sf_df1(0.0) == 1.0


def test_bh_qvalues():
    """BH step-up with monotone enforcement, returned in input order."""
    q = _bh_qvalues([0.01, 0.04, 0.03, 0.5])
    assert abs(q[0] - 0.04) < 1e-9
    assert abs(q[1] - 0.04 * 4 / 3) < 1e-9
    # p=0.03 (rank 2) would be 0.06 raw; monotonicity pulls it down to rank 3's q.
    assert abs(q[2] - 0.04 * 4 / 3) < 1e-9
    assert abs(q[3] - 0.5) < 1e-9
    assert _bh_qvalues([]) == []


# ---------------------------------------------------------------------------
# proportion_shift — detector
# ---------------------------------------------------------------------------


def _shift_windows() -> AnalysisWindows:
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        label="incident",
    )


def test_proportion_shift_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:user"], windows=_shift_windows()
    )
    assert result.status == "no_data"


def test_proportion_shift_flags_up_direction():
    """A value whose share jumps 0.5% → 8% is flagged 'up' with G as score."""
    responses = [
        FakeQueryResult(result_rows=[(11000,)], column_names=["count()"]),
        # window totals: baseline 10000, suspect 1000
        FakeQueryResult(result_rows=[(10000, 1000)], column_names=["bl", "w0"]),
        # val, baseline_cnt, bl_last, bl_evt, w0_cnt, w0_first, w0_evt
        FakeQueryResult(
            result_rows=[
                ("4625", 50, datetime(2024, 1, 14), "evt-bl", 80, datetime(2024, 1, 16), "evt-w"),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:eventid"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert result.method == "g-test"
    assert result.baseline_size == 10000
    assert len(result.results) == 1
    r = result.results[0]
    assert r.value == "4625"
    assert r.direction == "up"
    assert r.count == 80
    assert r.baseline_count == 50
    # G computed on raw counts: (50, 9950, 80, 920).
    assert abs(r.g_statistic - 225.2477) < 1e-3
    assert r.score == r.g_statistic
    assert r.q_value <= 0.05
    assert abs(r.rate_ratio - 16.0) < 0.01
    assert r.event_id == "evt-w"
    assert r.first_seen is not None
    assert r.details["window_label"] == "incident"
    assert r.details["allowlist_field"] == "attr:eventid"
    assert r.details["allowlist_value"] == "4625"
    assert r.details["m_tests"] == 1


def test_proportion_shift_vanished_value_is_down():
    """A baseline value absent from the suspect window is a maximal 'down'."""
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[
                ("heartbeat", 200, datetime(2024, 1, 14, 23), "evt-last", 0, None, ""),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:service"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.direction == "down"
    assert r.count == 0
    assert r.first_seen is None
    # Representative event = last baseline occurrence.
    assert r.event_id == "evt-last"
    assert r.details["last_seen_baseline"] is not None
    # Ratio uses Haldane–Anscombe smoothing (0.5/500 over 200/1000).
    assert abs(r.rate_ratio - (0.5 / 500) / 0.2) < 1e-6
    # The test itself used raw counts: G(200, 800, 0, 500).
    assert abs(r.g_statistic - 177.2186) < 1e-3


def test_proportion_shift_sql_excludes_first_seen():
    """The candidate scan prunes only first-seen values and keeps baseline rows."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_proportion_shifts("c1", ["s1"], fields=["attr:user"], windows=_shift_windows())
    candidate_sql = client.full_queries[2]
    assert "HAVING baseline_cnt >= 1" in candidate_sql
    # Baseline predicate must be in the WHERE union so vanished values survive.
    assert "{b0:String}" in candidate_sql and "{b1:String}" in candidate_sql
    assert "ORDER BY (baseline_cnt + w0_cnt) DESC" in candidate_sql


def test_proportion_shift_effect_floor():
    """A statistically significant but small (<min_ratio) shift is suppressed."""
    responses = [
        FakeQueryResult(result_rows=[(200000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(100000, 100000)], column_names=["bl", "w0"]),
        # 10% → 12%: hugely significant at this volume, ratio only 1.2.
        FakeQueryResult(
            result_rows=[
                (
                    "200",
                    10000,
                    datetime(2024, 1, 14),
                    "evt-bl",
                    12000,
                    datetime(2024, 1, 16),
                    "evt-w",
                ),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:status"], windows=_shift_windows(), min_ratio=2.0
    )
    assert result.status == "ok"
    assert result.results == []


def test_proportion_shift_fdr_gates_insignificant_shift():
    """A large ratio without statistical evidence (tiny counts) fails the q gate."""
    responses = [
        FakeQueryResult(result_rows=[(1100,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 100)], column_names=["bl", "w0"]),
        # 1/1000 → 1/100: ratio 10× but one event in the window — no evidence.
        FakeQueryResult(
            result_rows=[
                ("rareval", 1, datetime(2024, 1, 14), "evt-bl", 1, datetime(2024, 1, 16), "evt-w"),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:user"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_proportion_shift_allowlist_suppression():
    responses = [
        FakeQueryResult(result_rows=[(11000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(10000, 1000)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[
                ("4625", 50, datetime(2024, 1, 14), "evt-bl", 80, datetime(2024, 1, 16), "evt-w"),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1",
        ["s1"],
        fields=["attr:eventid"],
        windows=_shift_windows(),
        allowlist={("attr:eventid", "4625")},
    )
    assert result.status == "ok"
    assert result.results == []


def test_proportion_shift_tiny_window_warning():
    """A suspect window under the event floor is warned about, never dropped."""
    responses = [
        FakeQueryResult(result_rows=[(1030,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 30)], column_names=["bl", "w0"]),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:user"], windows=_shift_windows()
    )
    assert any("only 30 events" in w for w in result.warnings)


def test_proportion_shift_candidate_cap_warning():
    """Hitting the per-field candidate cap surfaces an FDR-coverage warning."""
    responses = [
        FakeQueryResult(result_rows=[(11000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(10000, 1000)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[
                ("a", 500, datetime(2024, 1, 14), "e1", 50, datetime(2024, 1, 16), "e2"),
                ("b", 400, datetime(2024, 1, 14), "e3", 40, datetime(2024, 1, 16), "e4"),
            ],
            column_names=[
                "val",
                "baseline_cnt",
                "bl_last",
                "bl_evt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1",
        ["s1"],
        fields=["attr:user"],
        windows=_shift_windows(),
        max_candidates_per_field=2,
    )
    assert any("candidate cap" in w and "attr:user" in w for w in result.warnings)


def test_proportion_shift_insufficient_data_empty_baseline():
    """A baseline window with zero events cannot anchor a proportion."""
    responses = [
        FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(0, 500)], column_names=["bl", "w0"]),
    ]
    svc = _svc(responses)
    result = svc.find_proportion_shifts(
        "c1", ["s1"], fields=["attr:user"], windows=_shift_windows()
    )
    assert result.status == "insufficient_data"
    assert any("baseline window contains no events" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# interval_periodicity — statistics helpers
# ---------------------------------------------------------------------------


def test_poisson_rate_g_equal_rates_is_zero():
    assert _poisson_rate_g(100, 10.0, 50, 5.0) == 0.0


def test_poisson_rate_g_total_silence():
    # All 100 events on one side of an even exposure split: G = 2·100·ln 2.
    g = _poisson_rate_g(100, 1.0, 0, 1.0)
    assert abs(g - 200.0 * np.log(2.0)) < 1e-9


def test_poisson_rate_g_degenerate_inputs():
    assert _poisson_rate_g(0, 1.0, 0, 1.0) == 0.0
    assert _poisson_rate_g(10, 0.0, 5, 1.0) == 0.0


def test_greenwood_p_too_few_spacings():
    assert _greenwood_p(0.5, 1) == (0.0, 1.0)


def test_greenwood_moments_match_simulation():
    """The Greenwood E[G]/Var[G] constants match a uniform-spacings simulation."""
    rng = np.random.default_rng(42)
    n = 10
    gs = []
    for _ in range(4000):
        pts = np.sort(rng.random(n - 1))
        spacings = np.diff(np.concatenate(([0.0], pts, [1.0])))
        gs.append(float(np.sum(spacings**2)))
    mean_expected = 2.0 / (n + 1)
    var_expected = 4.0 * (n - 1) / ((n + 1) ** 2 * (n + 2) * (n + 3))
    assert abs(np.mean(gs) - mean_expected) < 0.02 * mean_expected
    assert abs(np.var(gs) - var_expected) < 0.10 * var_expected
    # Perfectly even spacings sit in the left tail — but at the N = 10
    # minimum the tail is shallow (z ≈ -1.9): a minimal beacon train is
    # borderline by design, not a slam dunk.
    _, p_even_10 = _greenwood_p(1.0 / n, n)
    assert 0.01 < p_even_10 < 0.05
    # With a longer train the same perfect regularity is decisive.
    _, p_even_100 = _greenwood_p(1.0 / 100, 100)
    assert p_even_100 < 1e-6
    # A G at the null expectation is unremarkable.
    _, p_null = _greenwood_p(mean_expected, n)
    assert 0.4 < p_null < 0.6


# ---------------------------------------------------------------------------
# interval_periodicity — detector
# ---------------------------------------------------------------------------

# Row layout produced by the candidate scan: val, then 10 columns per window
# (n, k, mean, std, med, sum2, first, last, first_evt, last_evt), baseline
# first. `_iv_row` keeps the tests readable.

_IV_COLS = ["val"] + [
    f"w{w}_{c}"
    for w in (0, 1)
    for c in ("n", "k", "mean", "std", "med", "sum2", "first", "last", "first_evt", "last_evt")
]


def _iv_row(val: str, bl: tuple, wb: tuple) -> tuple:
    return (val, *bl, *wb)


def _iv_empty_block() -> tuple:
    # ClickHouse shape for a window with no occurrences: NaN aggregates and
    # type-default min/max/argMin values.
    nan = float("nan")
    epoch = datetime(1970, 1, 1)
    return (0, 0, nan, nan, nan, 0.0, epoch, epoch, "", "")


def _iv_windows() -> AnalysisWindows:
    # Baseline 14 days, suspect 4 days (d_b = 1,209,600 s, d_w = 345,600 s).
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        label="incident",
    )


def _beacon_windows() -> AnalysisWindows:
    # Short 2-hour suspect window (d_w = 7,200 s) so a 100-minute beacon train
    # covers well over the span floor.
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, 0, 0, tzinfo=UTC),
        datetime(2024, 1, 16, 2, 0, tzinfo=UTC),
        label="incident",
    )


# A 60-second heartbeat over the 14-day baseline: regular (CV ≈ 0.017).
_HEARTBEAT_BL = (
    20160,
    20159,
    60.0,
    1.0,
    60.0,
    20159 * 3601.0,
    datetime(2024, 1, 1, 0, 0),
    datetime(2024, 1, 14, 23, 59),
    "evt-bl-first",
    "evt-bl-last",
)

# A bursty baseline (CV = 1.5) — beaconing-gate eligible, cadence-ineligible.
_BURSTY_BL = (
    51,
    50,
    100.0,
    150.0,
    40.0,
    50 * 32500.0,
    datetime(2024, 1, 2),
    datetime(2024, 1, 14),
    "evt-bl-first",
    "evt-bl-last",
)


def test_interval_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "no_data"


def test_interval_missed_cadence_full_silence():
    """A baseline heartbeat with zero suspect-window events is a 'missed' finding."""
    responses = [
        FakeQueryResult(result_rows=[(21000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(20500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("heartbeat", _HEARTBEAT_BL, _iv_empty_block())],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert result.method == "cadence"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.direction == "missed"
    assert r.count == 0
    assert r.baseline_count == 20160
    assert r.first_seen is None
    # Representative event = last baseline occurrence (the D6 silence case).
    assert r.event_id == "evt-bl-last"
    assert r.details["last_seen_baseline"] is not None
    # ~5,760 arrivals expected from the 60 s baseline cadence over 4 days.
    assert abs(r.details["expected_count"] - 5760) < 1
    assert r.q_value <= 0.05
    assert r.score > 10
    assert r.details["allowlist_field"] == "attr:service"
    assert r.details["allowlist_value"] == "heartbeat"
    assert r.details["m_tests"] == 1


def test_interval_accelerated_cadence():
    """A regular value whose rate jumps 6× is flagged 'accelerated'."""
    wb = (
        34560,
        34559,
        10.0,
        0.5,
        10.0,
        34559 * 100.25,
        datetime(2024, 1, 16, 0, 0),
        datetime(2024, 1, 19, 23, 59),
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(60000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(21000, 35000)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("heartbeat", _HEARTBEAT_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.direction == "accelerated"
    assert abs(r.details["rate_ratio"] - 6.0) < 0.01
    assert r.event_id == "evt-w-first"
    assert r.first_seen is not None
    assert r.window_median_interval == 10.0


def test_interval_beaconing_new_regularity():
    """A bursty baseline value arriving every 60 s in the window is beaconing."""
    wb = (
        101,
        100,
        60.0,
        0.0,
        60.0,
        100 * 3600.0,  # Σδ² with every delta exactly 60 s
        datetime(2024, 1, 16, 0, 5),
        datetime(2024, 1, 16, 1, 45),  # span = 6,000 s of the 7,200 s window
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("10.0.0.66", _BURSTY_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:dest_ip"], windows=_beacon_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.direction == "new_regularity"
    assert r.window_cv == 0.0
    assert r.baseline_cv == 1.5
    # G = 360,000 / 6,000² = 0.01, well below E[G] = 2/101.
    assert abs(r.statistic - 0.01) < 1e-9
    assert r.details["greenwood_z"] < -3
    assert abs(r.details["span_fraction"] - 6000 / 7200) < 1e-3
    assert r.q_value <= 0.05
    assert r.event_id == "evt-w-first"


def test_interval_beacon_span_floor_suppresses_burst():
    """An evenly spaced but short burst (tiny span fraction) is not beaconing."""
    wb = (
        101,
        100,
        60.0,
        0.0,
        60.0,
        100 * 3600.0,
        datetime(2024, 1, 16, 0, 5),
        datetime(2024, 1, 16, 1, 45),  # span 6,000 s of a 345,600 s window
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("10.0.0.66", _BURSTY_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    # Same data, but the long 4-day suspect window: span fraction ≈ 0.017.
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:dest_ip"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_beacon_cv_floor():
    """A significant Greenwood z with a loose window CV (> ceiling) is suppressed."""
    wb = (
        101,
        100,
        60.0,
        30.0,  # CV 0.5 > beacon_cv_max 0.3
        60.0,
        100 * 4500.0,  # Σδ² = k·(mean² + var) = 100·(3600 + 900)
        datetime(2024, 1, 16, 0, 5),
        datetime(2024, 1, 16, 1, 45),
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("10.0.0.66", _BURSTY_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:dest_ip"], windows=_beacon_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_rate_ratio_effect_floor():
    """A significant but small (< min_rate_ratio) rate change is suppressed."""
    wb = (
        8640,  # 1.5× the baseline rate over the 4-day window
        8639,
        40.0,
        2.0,
        40.0,
        8639 * 1604.0,
        datetime(2024, 1, 16, 0, 0),
        datetime(2024, 1, 19, 23, 59),
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(30000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(21000, 9000)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("heartbeat", _HEARTBEAT_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_regularity_gate_excludes_bursty_baseline():
    """A bursty baseline value with a count drop gets no cadence test at all."""
    wb = (
        3,
        2,
        1000.0,
        800.0,
        900.0,
        2 * 1640000.0,
        datetime(2024, 1, 16, 1, 0),
        datetime(2024, 1, 16, 2, 0),
        "evt-w-first",
        "evt-w-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("job", _BURSTY_BL, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    # Bursty baseline → no cadence-break test; k_w = 2 < beacon minimum → no
    # beacon test either. status stays ok (the field was evaluated).
    assert result.status == "ok"
    assert result.results == []


def test_interval_dead_band_gets_no_test():
    """A baseline CV between the regular ceiling and irregular floor is untested."""
    dead_band_bl = (
        201,
        200,
        100.0,
        65.0,  # CV 0.65 — inside the deliberate dead band [0.5, 0.8]
        90.0,
        200 * 14225.0,
        datetime(2024, 1, 1),
        datetime(2024, 1, 14),
        "evt-bl-first",
        "evt-bl-last",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("svc", dead_band_bl, _iv_empty_block())],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_fdr_gates_weak_evidence():
    """A regular but sparse baseline (7 events) can't establish a missed arrival."""
    sparse_regular_bl = (
        7,
        6,
        172800.0,  # every ~2 days
        1000.0,
        172800.0,
        6 * 172800.0**2,
        datetime(2024, 1, 1),
        datetime(2024, 1, 13),
        "evt-bl-first",
        "evt-bl-last",
    )
    wb = (
        1,
        0,
        float("nan"),
        float("nan"),
        float("nan"),
        0.0,
        datetime(2024, 1, 17),
        datetime(2024, 1, 17),
        "evt-w",
        "evt-w",
    )
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("backup", sparse_regular_bl, wb)],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    # Expected ~2 arrivals, observed 1 — p ≈ 0.5, nowhere near the q gate.
    assert result.status == "ok"
    assert result.results == []


def test_interval_allowlist_suppression():
    responses = [
        FakeQueryResult(result_rows=[(21000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(20500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[_iv_row("heartbeat", _HEARTBEAT_BL, _iv_empty_block())],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1",
        ["s1"],
        fields=["attr:service"],
        windows=_iv_windows(),
        allowlist={("attr:service", "heartbeat")},
    )
    assert result.status == "ok"
    assert result.results == []


def test_interval_candidate_cap_warning():
    responses = [
        FakeQueryResult(result_rows=[(21000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(20500, 500)], column_names=["bl", "w0"]),
        FakeQueryResult(
            result_rows=[
                _iv_row("a", _HEARTBEAT_BL, _iv_empty_block()),
                _iv_row("b", _HEARTBEAT_BL, _iv_empty_block()),
            ],
            column_names=_IV_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1",
        ["s1"],
        fields=["attr:service"],
        windows=_iv_windows(),
        max_candidates_per_field=2,
    )
    assert any("candidate cap" in w and "attr:service" in w for w in result.warnings)


def test_interval_tiny_window_warning():
    responses = [
        FakeQueryResult(result_rows=[(1030,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 30)], column_names=["bl", "w0"]),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert any("only 30 events" in w for w in result.warnings)


def test_interval_insufficient_data_empty_baseline():
    responses = [
        FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(0, 500)], column_names=["bl", "w0"]),
    ]
    svc = _svc(responses)
    result = svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_iv_windows()
    )
    assert result.status == "insufficient_data"
    assert any("baseline window contains no events" in w for w in result.warnings)


def test_interval_sql_partitions_deltas_within_windows():
    """The candidate scan partitions the lag by (value, window) and keeps baseline rows."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_interval_periodicity("c1", ["s1"], fields=["attr:service"], windows=_iv_windows())
    candidate_sql = client.full_queries[2]
    # Deltas must be computed strictly within one (value, window) partition —
    # a boundary-straddling delta would corrupt both windows' statistics.
    assert "PARTITION BY val, win" in candidate_sql
    assert "lagInFrame(toNullable(ts))" in candidate_sql
    assert "arrayJoin(arrayFilter" in candidate_sql
    # Baseline-only (silent) values must survive; first-seen must not.
    assert "HAVING w0_n >= 1" in candidate_sql
    assert "{b0:String}" in candidate_sql and "{b1:String}" in candidate_sql


# ---------------------------------------------------------------------------
# W2 — per-source clock-skew correction (offset-corrected effective timestamp)
# ---------------------------------------------------------------------------


def _w2_windows() -> AnalysisWindows:
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 2, tzinfo=UTC),
        datetime(2024, 1, 3, tzinfo=UTC),
    )


def test_window_preds_fast_path_is_bare_timestamp():
    """No active offset → predicates are byte-identical to the pre-W2 form and
    no offset params are bound (keeps ClickHouse's primary-index path intact)."""
    params: dict[str, Any] = {}
    bp, sps = _window_preds(_w2_windows(), params, None)
    assert bp == "(timestamp >= {b0:String} AND timestamp < {b1:String})"
    assert sps == ["(timestamp >= {w0s:String} AND timestamp < {w0e:String})"]
    assert OFFSET_SRC_PARAM not in params and OFFSET_VAL_PARAM not in params


def test_window_preds_offset_path_uses_effective_ts_and_binds_arrays():
    """An active offset rewrites the predicates over the effective-ts expression
    and binds the parallel source/offset arrays consumed by transform()."""
    params: dict[str, Any] = {}
    offsets = {"s1": 3600}
    bp, sps = _window_preds(_w2_windows(), params, offsets)
    eff = effective_ts_sql(offsets)
    assert eff != "timestamp"
    assert bp == f"({eff} >= {{b0:String}} AND {eff} < {{b1:String}})"
    assert sps == [f"({eff} >= {{w0s:String}} AND {eff} < {{w0e:String}})"]
    assert params[OFFSET_SRC_PARAM] == ["s1"]
    assert params[OFFSET_VAL_PARAM] == [3600]


def test_value_novelty_temporal_fast_path_binds_no_offset_params():
    """A zero/absent offset map leaves every query's params free of the offset
    arrays — the byte-identical fast path detectors share with the query layer."""
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 10)], column_names=["bl", "w0"]),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    svc.find_value_novelty("c1", ["s1"], fields=["artifact"], windows=_w2_windows())
    assert all(OFFSET_SRC_PARAM not in p for p in svc.ch.client._all_parameters)


def test_value_novelty_temporal_offset_uses_effective_ts():
    """With an offset, the temporal scan's representative aggregates and window
    predicates are built over the effective timestamp and the arrays are bound."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 10)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_value_novelty(
        "c1", ["s1"], fields=["artifact"], windows=_w2_windows(), source_offsets={"s1": 3600}
    )
    scan_sql = client.full_queries[2]
    assert "addSeconds(timestamp, transform(source_id" in scan_sql
    assert "minIf(if(" in scan_sql  # minIf over the effective-ts expression
    assert any(p.get(OFFSET_VAL_PARAM) == [3600] for p in client._all_parameters)


def test_range_violations_offset_projects_source_id_in_subquery():
    """The effective-ts expression references source_id, so the numeric
    subqueries must project it — otherwise the outer window predicate / min()
    would reference an out-of-scope column."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            # baseline stat (min/max/n)
            FakeQueryResult(result_rows=[(0.0, 100.0, 500)], column_names=["lo", "hi", "n"]),
            # violations
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_range_violations(
        "c1", ["s1"], fields=["attr:bytes"], windows=_w2_windows(), source_offsets={"s1": -120}
    )
    stat_sql = client.full_queries[1]
    viol_sql = client.full_queries[2]
    # source_id projected into the inner num subqueries (fast path omits it).
    assert "AS num, timestamp, event_id, source_id" in stat_sql
    assert "source_id" in viol_sql
    assert "addSeconds(timestamp, transform(source_id" in stat_sql
    assert any(p.get(OFFSET_VAL_PARAM) == [-120] for p in client._all_parameters)


def test_frequency_self_baseline_offset_buckets_effective_ts():
    """Self-baseline bucketing runs over the effective timestamp so a skewed
    source's events land in their corrected bucket."""
    client = RecordingClient(
        [
            # timeline range (min, max)
            FakeQueryResult(
                result_rows=[(datetime(2024, 1, 1), datetime(2024, 1, 3))],
                column_names=["min", "max"],
            ),
            # bucket scan
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_frequency_anomalies("c1", ["s1"], series_field="artifact", source_offsets={"s1": 3600})
    range_sql = client.full_queries[0]
    bucket_sql = client.full_queries[1]
    assert "min(if(" in range_sql  # range over effective-ts
    assert "toStartOfInterval(if(" in bucket_sql
    assert any(p.get(OFFSET_SRC_PARAM) == ["s1"] for p in client._all_parameters)


def test_get_timeline_range_offset_uses_effective_ts():
    client = RecordingClient(
        [
            FakeQueryResult(
                result_rows=[(datetime(2024, 1, 1), datetime(2024, 1, 3))], column_names=[]
            )
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.get_timeline_range("c1", ["s1"], source_offsets={"s1": 3600})
    sql = client.full_queries[0]
    assert "min(if(" in sql and "max(if(" in sql
    assert client._all_parameters[0].get(OFFSET_VAL_PARAM) == [3600]


def test_order_violations_offset_shifts_reported_timestamps_only():
    """The skew math stays on the raw column (offset cancels within a source);
    only the reported timestamp/prev_timestamp are shifted for presentation."""
    ts = datetime(2024, 1, 1, 12, 0, 5, tzinfo=UTC)
    prev = datetime(2024, 1, 1, 12, 1, 5, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(100,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1, 60.0)], column_names=_ORD_SUMMARY_COLS),
            FakeQueryResult(
                result_rows=[("evt-a", ts, prev, 60.0, 100, 3)],
                column_names=_ORD_DETAIL_COLS,
            ),
        ]
    )
    result = svc.find_order_violations(
        "c1", ["s1"], min_skew_seconds=1.0, source_offsets={"s1": 3600}
    )
    f = result.results[0]
    assert f.timestamp == (ts + timedelta(hours=1)).isoformat()
    assert f.prev_timestamp == (prev + timedelta(hours=1)).isoformat()
    # Skew delta is invariant to a uniform per-source shift.
    assert f.skew_seconds == 60.0


def test_interval_periodicity_offset_uses_effective_ts_for_gaps():
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_interval_periodicity(
        "c1", ["s1"], fields=["attr:service"], windows=_w2_windows(), source_offsets={"s1": 3600}
    )
    candidate_sql = client.full_queries[2]
    # ts (the lag/gap column) is the effective timestamp, so gaps are computed
    # on the corrected timeline.
    assert "addSeconds(timestamp, transform(source_id" in candidate_sql
    assert "AS ts" in candidate_sql


# ---------------------------------------------------------------------------
# sequence_novelty — detector
# ---------------------------------------------------------------------------

# Query order: count, window totals, per-window n-gram totals, novel n-grams.
# Novel-gram row layout: gram (list of values), baseline_cnt, then 3 columns
# per suspect window (cnt, first_ts, first_evt).

_SEQ_TOTALS_COLS = ["w_idx", "n"]
_SEQ_NOVEL_COLS = ["gram", "baseline_cnt", "w0_cnt", "w0_first", "w0_evt"]


def _seq_windows() -> AnalysisWindows:
    return _one_suspect(
        datetime(2024, 1, 1, tzinfo=UTC),
        datetime(2024, 1, 15, tzinfo=UTC),
        datetime(2024, 1, 16, tzinfo=UTC),
        datetime(2024, 1, 20, tzinfo=UTC),
        label="incident",
    )


def _seq_responses(
    total: int,
    window_totals: tuple[int, int],
    ngram_totals: list[tuple[int, int]],
    novel_rows: list[tuple],
) -> list[FakeQueryResult]:
    return [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[window_totals], column_names=["bl_total", "w0_total"]),
        FakeQueryResult(result_rows=ngram_totals, column_names=_SEQ_TOTALS_COLS),
        FakeQueryResult(result_rows=novel_rows, column_names=_SEQ_NOVEL_COLS),
    ]


def test_sequence_ngram_validation():
    svc = _svc([])
    for bad in (1, 6):
        try:
            svc.find_sequence_novelty("c1", ["s1"], ngram=bad, windows=_seq_windows())
        except ValueError as exc:
            assert "between 2 and 5" in str(exc)
        else:
            raise AssertionError(f"ngram={bad} did not raise")
    assert svc.ch.client._calls == []


def test_sequence_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows())
    assert result.status == "no_data"
    assert result.windows is not None


def test_sequence_baseline_without_ngrams_insufficient():
    """A baseline window with no complete n-grams cannot vouch for anything."""
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(2, 500)], column_names=["bl_total", "w0_total"]),
        # Only the suspect window has complete n-grams.
        FakeQueryResult(result_rows=[(0, 498)], column_names=_SEQ_TOTALS_COLS),
    ]
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows())
    assert result.status == "insufficient_data"
    assert any("no complete sequences of length 3" in w for w in result.warnings)
    # The novel-gram query must not have run.
    assert len(svc.ch.client._calls) == 3


def test_sequence_novel_ngram_flagged():
    first_ts = datetime(2024, 1, 17, 12, 0, tzinfo=UTC)
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 2000),
        ngram_totals=[(-1, 7998), (0, 1998)],
        novel_rows=[(["login", "priv_esc", "wipe"], 0, 2, first_ts, "evt-1")],
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows())
    assert result.status == "ok"
    assert result.detector == "sequence_novelty"
    assert result.method == "ngram"
    assert result.baseline_size == 8000
    assert len(result.results) == 1
    r = result.results[0]
    assert r.field == "artifact"
    assert r.values == ["login", "priv_esc", "wipe"]
    assert r.value == "login → priv_esc → wipe"
    assert r.count == 2
    assert abs(r.score - (-np.log(2 / 1998))) < 1e-3
    assert r.event_id == "evt-1"
    assert r.first_seen is not None and r.first_seen.startswith("2024-01-17T12:00")
    assert r.details["n"] == 3
    assert r.details["window_ngram_total"] == 1998
    assert r.details["baseline_ngram_total"] == 7998
    assert r.details["window_label"] == "incident"
    assert r.details["allowlist_field"] == "artifact"
    assert r.details["allowlist_value"] == "login → priv_esc → wipe"
    assert result.windows is not None


def test_sequence_multiple_suspect_windows():
    """One finding per (gram, suspect window with cnt > 0)."""
    windows = AnalysisWindows(
        baseline=TimeWindow(
            "baseline", datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 15, tzinfo=UTC)
        ),
        suspects=(
            TimeWindow("w-a", datetime(2024, 1, 16, tzinfo=UTC), datetime(2024, 1, 18, tzinfo=UTC)),
            TimeWindow("w-b", datetime(2024, 1, 19, tzinfo=UTC), datetime(2024, 1, 21, tzinfo=UTC)),
        ),
    )
    ts_a = datetime(2024, 1, 16, 1, 0, tzinfo=UTC)
    ts_b = datetime(2024, 1, 19, 1, 0, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[(8000, 1000, 1000)], column_names=["bl_total", "w0_total", "w1_total"]
        ),
        FakeQueryResult(
            result_rows=[(-1, 7998), (0, 998), (1, 998)], column_names=_SEQ_TOTALS_COLS
        ),
        FakeQueryResult(
            result_rows=[(["a", "b", "c"], 0, 3, ts_a, "evt-a", 1, ts_b, "evt-b")],
            column_names=[
                "gram",
                "baseline_cnt",
                "w0_cnt",
                "w0_first",
                "w0_evt",
                "w1_cnt",
                "w1_first",
                "w1_evt",
            ],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=windows)
    assert result.status == "ok"
    assert len(result.results) == 2
    by_label = {r.details["window_label"]: r for r in result.results}
    assert by_label["w-a"].count == 3 and by_label["w-a"].event_id == "evt-a"
    assert by_label["w-b"].count == 1 and by_label["w-b"].event_id == "evt-b"
    # Rarer (count 1) window scores higher.
    assert by_label["w-b"].score > by_label["w-a"].score


def test_sequence_allowlist_suppression():
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 2000),
        ngram_totals=[(-1, 7998), (0, 1998)],
        novel_rows=[
            (["a", "b", "c"], 0, 2, first_ts, "evt-1"),
            (["x", "y", "z"], 0, 5, first_ts, "evt-2"),
        ],
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty(
        "c1",
        ["s1"],
        windows=_seq_windows(),
        allowlist={("artifact", "a → b → c")},
    )
    assert result.status == "ok"
    assert [r.value for r in result.results] == ["x → y → z"]


def test_sequence_exclude_event_ids():
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 2000),
        ngram_totals=[(-1, 7998), (0, 1998)],
        novel_rows=[(["a", "b", "c"], 0, 2, first_ts, "evt-normal")],
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty(
        "c1", ["s1"], windows=_seq_windows(), exclude_event_ids={"evt-normal"}
    )
    assert result.status == "ok"
    assert result.results == []


def test_sequence_tiny_window_and_cap_warnings():
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    novel_rows = [([f"v{i}", "b", "c"], 0, 1, first_ts, f"evt-{i}") for i in range(3)]
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 30),
        ngram_totals=[(-1, 7998), (0, 28)],
        novel_rows=novel_rows,
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows(), max_candidates=3)
    assert result.status == "ok"
    assert any("only 28 complete sequences" in w for w in result.warnings)
    assert any("candidate cap" in w for w in result.warnings)


def test_sequence_limit_applied():
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    novel_rows = [([f"v{i}", "b", "c"], 0, i + 1, first_ts, f"evt-{i}") for i in range(5)]
    responses = _seq_responses(
        total=10_000,
        window_totals=(8000, 2000),
        ngram_totals=[(-1, 7998), (0, 1998)],
        novel_rows=novel_rows,
    )
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows(), limit=2)
    assert len(result.results) == 2
    # Sorted by surprise descending — lowest counts first.
    assert [r.count for r in result.results] == [1, 2]


def test_sequence_sql_shape():
    """The generated SQL is the explainable per-(source, window) lag chain."""
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    client = RecordingClient(
        _seq_responses(
            total=10_000,
            window_totals=(8000, 2000),
            ngram_totals=[(-1, 7998), (0, 1998)],
            novel_rows=[(["a", "b", "c"], 0, 2, first_ts, "evt-1")],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_sequence_novelty(
        "c1", ["s1"], series_field="attr:proc", ngram=3, windows=_seq_windows()
    )
    assert result.status == "ok"
    totals_sql = client.full_queries[2]
    novel_sql = client.full_queries[3]
    for sql in (totals_sql, novel_sql):
        assert "PARTITION BY source_id, w_idx" in sql
        assert "ORDER BY ets, byte_offset, line_number, event_id" in sql
        assert "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW" in sql
        assert "lagInFrame(val, 2) OVER w" in sql
        assert "lagInFrame(val, 1) OVER w" in sql
        assert "lagInFrame(toNullable(val), 2) OVER w AS guard" in sql
        assert "guard IS NOT NULL" in sql
        assert "attributes[{fk:String}]" in sql
        assert "multiIf(" in sql
    assert "HAVING baseline_cnt = 0 AND (w0_cnt) > 0" in novel_sql
    # Window bounds bound as parameters (forensic reproducibility).
    params = client._all_parameters[3]
    assert params["b0"] == "2024-01-01 00:00:00.000"
    assert params["w0s"] == "2024-01-16 00:00:00.000"


def test_sequence_offset_uses_effective_ts():
    """W2: active source offsets switch ordering and windows to effective ts."""
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    client = RecordingClient(
        _seq_responses(
            total=10_000,
            window_totals=(8000, 2000),
            ngram_totals=[(-1, 7998), (0, 1998)],
            novel_rows=[(["a", "b", "c"], 0, 2, first_ts, "evt-1")],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows(), source_offsets={"s1": 3600})
    novel_sql = client.full_queries[3]
    assert "addSeconds(timestamp, transform(source_id" in novel_sql
    assert "AS ets" in novel_sql


def test_sequence_single_source_skips_cross_source_baseline_check():
    """With one source, Query C is unnecessary: exactly 4 queries run
    (count, window totals, per-source totals, per-source novel grams)."""
    first_ts = datetime(2024, 1, 17, tzinfo=UTC)
    svc = _svc(
        _seq_responses(
            total=10_000,
            window_totals=(8000, 2000),
            ngram_totals=[(-1, 7998), (0, 1998)],
            novel_rows=[(["a", "b", "c"], 0, 2, first_ts, "evt-1")],
        )
    )
    result = svc.find_sequence_novelty("c1", ["s1"], windows=_seq_windows())
    assert result.status == "ok"
    assert len(svc.ch.client._calls) == 4


def test_sequence_multi_source_merges_counts_and_verifies_baselines():
    """Per-source scans (X4): counts are summed across sources, the earliest
    occurrence supplies the representative event, and a candidate present in
    ANOTHER source's baseline is killed by the cross-source check."""
    ts_early = datetime(2024, 1, 16, 8, 0, tzinfo=UTC)
    ts_late = datetime(2024, 1, 17, 12, 0, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(8000, 2000)], column_names=["bl_total", "w0_total"]),
        # totals per source — summed: baseline 5000+3000, suspect 1000+998.
        FakeQueryResult(result_rows=[(-1, 5000), (0, 1000)], column_names=_SEQ_TOTALS_COLS),
        FakeQueryResult(result_rows=[(-1, 3000), (0, 998)], column_names=_SEQ_TOTALS_COLS),
        # novel grams per source: gram A in both (s2 earlier), gram B only s1.
        FakeQueryResult(
            result_rows=[
                (["a", "b", "c"], 0, 2, ts_late, "evt-s1"),
                (["x", "y", "z"], 0, 4, ts_late, "evt-s1x"),
            ],
            column_names=_SEQ_NOVEL_COLS,
        ),
        FakeQueryResult(
            result_rows=[(["a", "b", "c"], 0, 3, ts_early, "evt-s2")],
            column_names=_SEQ_NOVEL_COLS,
        ),
        # cross-source baseline check: s1's baseline vouches for nothing,
        # s2's baseline contains gram B — killed.
        FakeQueryResult(result_rows=[], column_names=["gram"]),
        FakeQueryResult(result_rows=[(["x", "y", "z"],)], column_names=["gram"]),
    ]
    svc = _svc(responses)
    result = svc.find_sequence_novelty("c1", ["s1", "s2"], windows=_seq_windows())
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.value == "a → b → c"
    # 2 (s1) + 3 (s2), scored against the summed window total.
    assert r.count == 5
    assert abs(r.score - (-np.log(5 / 1998))) < 1e-3
    assert r.details["window_ngram_total"] == 1998
    assert r.details["baseline_ngram_total"] == 8000
    # Earliest occurrence (s2) supplies the representative event.
    assert r.event_id == "evt-s2"
    assert r.first_seen is not None and r.first_seen.startswith("2024-01-16T08:00")
    # 8 queries: count, window totals, 2x totals, 2x novel, 2x baseline check.
    assert len(svc.ch.client._calls) == 8
    # Every per-source query is bound to exactly one source.
    for p in svc.ch.client._all_parameters[2:]:
        assert p["src"] in (["s1"], ["s2"])


# ---------------------------------------------------------------------------
# sequence_motif — detector
# ---------------------------------------------------------------------------

# Query order: count, then per source one support query, then per source one
# cadence query (skipped entirely when no gram survives min_support).
# Support row layout: gram, support, first_occ, last_occ, evt.
# Cadence row layout: gram, k, mean, std, med, sum2, first, last.

_MOTIF_SUPPORT_COLS = ["gram", "support", "first_occ", "last_occ", "evt"]
_MOTIF_CADENCE_COLS = ["gram", "k", "mean", "std", "med", "sum2", "first", "last"]


def _motif_responses(
    total: int,
    support_rows: list[tuple],
    cadence_rows: list[tuple] | None = None,
) -> list[FakeQueryResult]:
    responses = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(result_rows=support_rows, column_names=_MOTIF_SUPPORT_COLS),
    ]
    if cadence_rows is not None:
        responses.append(
            FakeQueryResult(result_rows=cadence_rows, column_names=_MOTIF_CADENCE_COLS)
        )
    return responses


def test_motif_validation():
    svc = _svc([])
    for bad in (1, 6):
        try:
            svc.find_sequence_motifs("c1", ["s1"], ngram=bad)
        except ValueError as exc:
            assert "between 2 and 5" in str(exc)
        else:
            raise AssertionError(f"ngram={bad} did not raise")
    try:
        svc.find_sequence_motifs("c1", ["s1"], min_support=1)
    except ValueError as exc:
        assert "min_support" in str(exc)
    else:
        raise AssertionError("min_support=1 did not raise")
    assert svc.ch.client._calls == []


def test_motif_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_sequence_motifs("c1", ["s1"])
    assert result.status == "no_data"
    assert result.detector == "sequence_motif"
    assert result.method == "motif"


def test_motif_empty_mining_is_ok():
    """No gram over min_support is a valid mining answer — ok, no cadence pass."""
    svc = _svc(_motif_responses(total=10_000, support_rows=[]))
    result = svc.find_sequence_motifs("c1", ["s1"])
    assert result.status == "ok"
    assert result.results == []
    assert result.total_findings == 0
    # count + one support query, no cadence query.
    assert len(svc.ch.client._calls) == 2


def test_motif_recurring_sequence_scored():
    first_occ = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    last_occ = datetime(2024, 1, 1, 4, 0, tzinfo=UTC)
    responses = _motif_responses(
        total=10_000,
        support_rows=[(["login", "sync", "logout"], 47, first_occ, last_occ, "evt-1")],
        # 46 gaps, tight cadence: mean 300s, std 15s, median 300s.
        cadence_rows=[
            (
                ["login", "sync", "logout"],
                46,
                300.0,
                15.0,
                300.0,
                46 * 300.0**2,
                first_occ,
                last_occ,
            )
        ],
    )
    svc = _svc(responses)
    result = svc.find_sequence_motifs("c1", ["s1"])
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.field == "artifact"
    assert r.value == "login → sync → logout"
    assert r.support == 47
    assert r.sources_count == 1
    assert r.period_seconds == 300.0
    assert r.cv == 0.05
    assert r.regularity_score == 0.95
    assert abs(r.score - round(np.log10(47) * 1.95, 4)) < 1e-6
    assert r.first_seen is not None and r.first_seen.startswith("2024-01-01T00:00")
    assert r.last_seen is not None and r.last_seen.startswith("2024-01-01T04:00")
    assert r.event_id == "evt-1"
    # 46 ≥ the Greenwood floor — regularity p-value computed and auditable.
    assert r.details["greenwood_p"] is not None
    assert r.details["n_intervals"] == 46
    assert r.details["representative_source_id"] == "s1"
    assert r.details["allowlist_field"] == "artifact"
    assert r.details["allowlist_value"] == "login → sync → logout"
    assert result.windows is None


def test_motif_without_cadence_ranks_on_support_alone():
    """A motif with < 2 gaps in every source gets regularity 0, score log10(support)."""
    first_occ = datetime(2024, 1, 1, tzinfo=UTC)
    responses = _motif_responses(
        total=1_000,
        support_rows=[(["a", "b", "c"], 3, first_occ, first_occ, "evt-1")],
        # One gap only → std is NaN-gated to None, no CV.
        cadence_rows=[(["a", "b", "c"], 1, 60.0, float("nan"), 60.0, 3600.0, first_occ, first_occ)],
    )
    svc = _svc(responses)
    result = svc.find_sequence_motifs("c1", ["s1"])
    r = result.results[0]
    assert r.cv is None
    assert r.regularity_score == 0.0
    assert abs(r.score - round(np.log10(3), 4)) < 1e-6
    # Median gap still reported (k >= 1) via the most-intervals fallback.
    assert r.period_seconds == 60.0
    assert r.details["greenwood_p"] is None


def test_motif_multi_source_merge_and_representative_cadence():
    """Supports sum, sources_count counts, earliest occurrence wins the
    representative event, and the lowest-CV source supplies the cadence."""
    ts_early = datetime(2024, 1, 1, 8, 0, tzinfo=UTC)
    ts_late = datetime(2024, 1, 2, 12, 0, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
        # support per source
        FakeQueryResult(
            result_rows=[(["a", "b", "c"], 20, ts_late, ts_late, "evt-s1")],
            column_names=_MOTIF_SUPPORT_COLS,
        ),
        FakeQueryResult(
            result_rows=[(["a", "b", "c"], 30, ts_early, ts_late, "evt-s2")],
            column_names=_MOTIF_SUPPORT_COLS,
        ),
        # cadence per source: s1 sloppy (cv 0.5), s2 tight (cv 0.1).
        FakeQueryResult(
            result_rows=[
                (["a", "b", "c"], 19, 200.0, 100.0, 180.0, 19 * 200.0**2, ts_late, ts_late)
            ],
            column_names=_MOTIF_CADENCE_COLS,
        ),
        FakeQueryResult(
            result_rows=[
                (["a", "b", "c"], 29, 300.0, 30.0, 290.0, 29 * 300.0**2, ts_early, ts_late)
            ],
            column_names=_MOTIF_CADENCE_COLS,
        ),
    ]
    svc = _svc(responses)
    result = svc.find_sequence_motifs("c1", ["s1", "s2"])
    assert result.status == "ok"
    r = result.results[0]
    assert r.support == 50
    assert r.sources_count == 2
    assert r.event_id == "evt-s2"
    assert r.first_seen is not None and r.first_seen.startswith("2024-01-01T08:00")
    # Representative cadence = lowest CV (s2).
    assert r.cv == 0.1
    assert r.period_seconds == 290.0
    assert r.details["representative_source_id"] == "s2"
    assert len(r.details["per_source_cadence"]) == 2
    # 5 queries: count, 2x support, 2x cadence — each bound to one source.
    assert len(svc.ch.client._calls) == 5
    for p in svc.ch.client._all_parameters[1:]:
        assert p["src"] in (["s1"], ["s2"])


def test_motif_candidate_cap_and_top_k_warnings():
    first_occ = datetime(2024, 1, 1, tzinfo=UTC)
    support_rows = [
        ([f"v{i}", "b", "c"], 10 - i, first_occ, first_occ, f"evt-{i}") for i in range(3)
    ]
    responses = _motif_responses(total=10_000, support_rows=support_rows, cadence_rows=[])
    svc = _svc(responses)
    result = svc.find_sequence_motifs("c1", ["s1"], max_candidates=3, cadence_top_k=2)
    assert result.status == "ok"
    assert any("candidate cap" in w for w in result.warnings)
    assert any("cadence-scored" in w for w in result.warnings)
    # Only the top-2 grams by support were sent to the cadence pass.
    cands = svc.ch.client._all_parameters[2]["cands"]
    assert len(cands) == 2
    assert cands[0] == ["v0", "b", "c"]


def test_motif_allowlist_and_exclude():
    first_occ = datetime(2024, 1, 1, tzinfo=UTC)
    responses = _motif_responses(
        total=10_000,
        support_rows=[
            (["a", "b", "c"], 10, first_occ, first_occ, "evt-1"),
            (["x", "y", "z"], 5, first_occ, first_occ, "evt-routine"),
        ],
        cadence_rows=[],
    )
    svc = _svc(responses)
    result = svc.find_sequence_motifs(
        "c1",
        ["s1"],
        allowlist={("artifact", "a → b → c")},
        exclude_event_ids={"evt-routine"},
    )
    assert result.status == "ok"
    assert result.results == []


def test_motif_sql_shape_and_time_scope():
    """Single pseudo-window lag chain; start/end fold into the scope predicate."""
    first_occ = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        _motif_responses(
            total=10_000,
            support_rows=[(["a", "b", "c"], 10, first_occ, first_occ, "evt-1")],
            cadence_rows=[],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_sequence_motifs(
        "c1",
        ["s1"],
        series_field="attr:proc",
        min_support=4,
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 2, 1, tzinfo=UTC),
    )
    assert result.status == "ok"
    support_sql = client.full_queries[1]
    cadence_sql = client.full_queries[2]
    for sql in (support_sql, cadence_sql):
        assert "PARTITION BY source_id, w_idx" in sql
        assert "ORDER BY ets, byte_offset, line_number, event_id" in sql
        assert "ROWS BETWEEN 2 PRECEDING AND CURRENT ROW" in sql
        assert "lagInFrame(val, 2) OVER w" in sql
        assert "guard IS NOT NULL" in sql
        assert "attributes[{fk:String}]" in sql
        # Time-frame scope instead of analysis windows.
        assert "{ms:String}" in sql and "{me:String}" in sql
        assert "multiIf(" not in sql
    assert "HAVING support >= {msup:UInt32}" in support_sql
    assert "ORDER BY support DESC, gram ASC" in support_sql
    assert "has({cands:Array(Array(String))}, gram)" in cadence_sql
    assert "PARTITION BY gram" in cadence_sql
    assert "ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING" in cadence_sql
    params = client._all_parameters[1]
    assert params["msup"] == 4
    assert params["ms"] == "2024-01-01 00:00:00.000"
    assert params["me"] == "2024-02-01 00:00:00.000"


def test_motif_unscoped_has_no_window_params():
    first_occ = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        _motif_responses(
            total=10_000,
            support_rows=[(["a", "b", "c"], 10, first_occ, first_occ, "evt-1")],
            cadence_rows=[],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_sequence_motifs("c1", ["s1"])
    support_sql = client.full_queries[1]
    assert "AND (1)" in support_sql
    assert "{ms:String}" not in support_sql and "{me:String}" not in support_sql


# ---------------------------------------------------------------------------
# resolve_motif_occurrences
# ---------------------------------------------------------------------------


class _OccurrenceRecordingStore(FakeClickHouseStore):
    """FakeClickHouseStore that records motif-occurrence inserts."""

    def __init__(self, client: FakeClient) -> None:
        super().__init__(client)
        self.inserted: list[list[tuple]] = []

    def insert_motif_occurrences(self, rows: list[tuple]) -> int:
        self.inserted.append(rows)
        return len(rows)


def test_resolve_motif_occurrences_inserts_member_rows():
    occ_ts = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        [
            FakeQueryResult(
                result_rows=[
                    ("s1", occ_ts, "evt-1"),
                    ("s1", occ_ts, "evt-2"),
                    ("s1", occ_ts, "evt-3"),
                ],
                column_names=["source_id", "first_ts", "member_eid"],
            )
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = _OccurrenceRecordingStore(client)
    written, warnings = svc.resolve_motif_occurrences(
        "c1", ["s1"], "artifact", ["a", "b", "c"], "disp-1"
    )
    assert written == 3
    assert warnings == []
    rows = svc.ch.inserted[0]
    assert rows[0] == ("c1", "disp-1", "s1", "evt-1", occ_ts)
    sql = client.full_queries[0]
    assert "arrayJoin(eids)" in sql
    assert "gram = {g:Array(String)}" in sql
    assert "PARTITION BY source_id" in sql
    # Deterministic truncation under the row cap: earliest occurrences first,
    # ordered before LIMIT — re-marking the same motif collapses the same events.
    assert "ORDER BY first_ts, member_eid" in sql
    assert sql.index("ORDER BY first_ts, member_eid") < sql.index("LIMIT {lim:UInt64}")
    params = client._all_parameters[0]
    assert params["g"] == ["a", "b", "c"]
    assert params["lim"] == 500_000


def test_resolve_motif_occurrences_honors_mining_scope():
    """A time-scoped mined motif materializes with the same scope predicate,
    at the same (pre-window) query level — collapse covers exactly what was
    mined. Unscoped stays the no-op predicate."""
    client = RecordingClient(
        [FakeQueryResult(result_rows=[], column_names=["source_id", "first_ts", "member_eid"])]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = _OccurrenceRecordingStore(client)
    svc.resolve_motif_occurrences(
        "c1",
        ["s1"],
        "artifact",
        ["a", "b"],
        "disp-1",
        start=datetime(2024, 1, 1, tzinfo=UTC),
        end=datetime(2024, 2, 1, tzinfo=UTC),
    )
    sql = client.full_queries[0]
    assert "{ms:String}" in sql and "{me:String}" in sql
    # Scope must apply before n-gram assembly (inside the pre-window SELECT),
    # same as the miner — not as a post-hoc filter on assembled grams.
    assert sql.index("{ms:String}") < sql.index("WINDOW w AS")
    params = client._all_parameters[0]
    assert params["ms"] == "2024-01-01 00:00:00.000"
    assert params["me"] == "2024-02-01 00:00:00.000"


def test_resolve_motif_occurrences_cap_warning():
    occ_ts = datetime(2024, 1, 1, tzinfo=UTC)
    client = RecordingClient(
        [
            FakeQueryResult(
                result_rows=[("s1", occ_ts, f"evt-{i}") for i in range(4)],
                column_names=["source_id", "first_ts", "member_eid"],
            )
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = _OccurrenceRecordingStore(client)
    written, warnings = svc.resolve_motif_occurrences(
        "c1", ["s1", "s2"], "artifact", ["a", "b"], "disp-1", max_rows=4
    )
    assert written == 4
    # s1 hit the cap; s2 was never scanned — both facts surfaced.
    assert any("partial" in w for w in warnings)
    assert any("not collapsed" in w for w in warnings)
    assert len(client.full_queries) == 1


# ---------------------------------------------------------------------------
# value_distribution_drift detector
# ---------------------------------------------------------------------------


def test_chi2_sf_matches_known_values():
    """General chi² survival: df=1 erfc form, df=2 closed form, df=5 reference."""
    assert abs(_chi2_sf(3.84, 1) - _chi2_sf_df1(3.84)) < 1e-12
    # df=2 closed form: P(χ²₂ ≥ x) = exp(-x/2).
    assert abs(_chi2_sf(4.0, 2) - np.exp(-2.0)) < 1e-12
    # Classic table value: P(χ²₅ ≥ 11.07) ≈ 0.0500.
    assert abs(_chi2_sf(11.07, 5) - 0.05) < 1e-3
    # Both gamma branches (series y < a+1, continued fraction y ≥ a+1).
    assert 0.0 < _chi2_sf(1.0, 10) < 1.0
    assert _chi2_sf(50.0, 10) < 1e-6
    assert _chi2_sf(0.0, 3) == 1.0


def test_g_statistic_k_and_tvd_known_values():
    """2×k G against a hand-computed table; TVD against its definition."""
    # baseline (900, 100) vs window (500, 500): G = 406.9969 (hand-computed).
    assert abs(_g_statistic_k([900, 100], [500, 500]) - 406.9969) < 1e-3
    assert abs(_tvd([900, 100], [500, 500]) - 0.4) < 1e-12
    # Degenerate inputs contribute nothing.
    assert _g_statistic_k([0, 0], [0, 0]) == 0.0
    assert _tvd([0, 0], [1, 1]) == 0.0
    # All-empty columns are skipped (no NaN).
    assert _g_statistic_k([10, 0], [10, 0]) == 0.0


def _drift_probe(numeric: bool) -> FakeQueryResult:
    """Numeric-ratio probe response for one explicit field."""
    return FakeQueryResult(
        result_rows=[(95, 100) if numeric else (0, 100)],
        column_names=["num0", "ne0"],
    )


def _drift_ks_row(
    bl_n: int = 1000,
    w_n: int = 500,
    d: float = 0.35,
    p: float = 1e-8,
    bl_q: tuple = (1.0, 5.0, 9.0),
    w_q: tuple = (2.0, 8.0, 11.0),
) -> FakeQueryResult:
    """One numeric-field KS scan row (single suspect window)."""
    return FakeQueryResult(
        result_rows=[
            (
                bl_n,
                list(bl_q),
                w_n,
                list(w_q),
                (d, p),
                ("evt-lo", datetime(2024, 1, 16, 1, tzinfo=UTC)),
                ("evt-hi", datetime(2024, 1, 16, 2, tzinfo=UTC)),
            )
        ],
        column_names=["bl_n", "bl_q", "w0_n", "w0_q", "w0_ks", "w0_lo", "w0_hi"],
    )


def _drift_cat_rows(rows: list[tuple]) -> FakeQueryResult:
    """Categorical GROUP BY response: (val, bl_cnt, bl_last, bl_evt, w0_cnt, w0_first, w0_evt)."""
    return FakeQueryResult(
        result_rows=rows,
        column_names=["val", "baseline_cnt", "bl_last", "bl_evt", "w0_cnt", "w0_first", "w0_evt"],
    )


def _cat_row(val: str, bl: int, w: int) -> tuple:
    return (
        val,
        bl,
        datetime(2024, 1, 14, tzinfo=UTC),
        f"evt-bl-{val}",
        w,
        datetime(2024, 1, 16, tzinfo=UTC),
        f"evt-w-{val}",
    )


def test_distribution_drift_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows()
    )
    assert result.status == "no_data"


def test_distribution_drift_insufficient_data_empty_baseline():
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(0, 500)], column_names=["bl", "w0"]),
        ]
    )
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows()
    )
    assert result.status == "insufficient_data"
    assert any("baseline window contains no events" in w for w in result.warnings)


def test_distribution_drift_numeric_ks_detected():
    """A fake KS (D=0.35, p=1e-8) on a numeric field yields one 'up' finding."""
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        _drift_ks_row(),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert result.method == "drift"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.test == "ks"
    assert r.field == "attr:duration"
    assert r.window_label == "incident"
    assert r.statistic == 0.35
    assert r.effect == 0.35
    # Window median 8.0 > baseline median 5.0 → up; representative = max event.
    assert r.direction == "up"
    assert r.event_id == "evt-hi"
    assert r.first_seen is not None
    assert r.baseline_n == 1000
    assert r.window_n == 500
    # Score = -log10(p).
    assert abs(r.score - 8.0) < 1e-9
    assert r.details["ks_d"] == 0.35
    assert r.details["window_label"] == "incident"
    assert r.details["baseline_median"] == 5.0
    assert r.details["window_median"] == 8.0
    assert r.details["allowlist_field"] == "attr:duration"
    assert r.details["allowlist_value"] == "*"
    assert r.details["m_tests"] == 1


def test_distribution_drift_ks_dict_tuple_shape():
    """clickhouse_connect returns the KS named tuple as a dict — must parse.

    Verified against a live server: the result of kolmogorovSmirnovTestIf
    arrives as {'d_statistic': ..., 'p_value': ...}, not an indexable tuple.
    """
    row = _drift_ks_row()
    r0 = list(row.result_rows[0])
    r0[4] = {"d_statistic": 0.35, "p_value": 1e-8}
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        FakeQueryResult(result_rows=[tuple(r0)], column_names=row.column_names),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    assert result.results[0].statistic == 0.35


def test_distribution_drift_numeric_down_direction_uses_min_event():
    """Window median below baseline median → 'down', representative = min event."""
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        _drift_ks_row(bl_q=(1.0, 5.0, 9.0), w_q=(0.5, 2.0, 4.0)),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows()
    )
    r = result.results[0]
    assert r.direction == "down"
    assert r.event_id == "evt-lo"


def test_distribution_drift_categorical_gtest_detected():
    """A 3-category share shift is flagged with the hand-computed G and TVD."""
    rows = [_cat_row("a", 700, 200), _cat_row("b", 200, 300), _cat_row("c", 100, 500)]
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 1000)], column_names=["bl", "w0"]),
        _drift_probe(numeric=False),
        _drift_cat_rows(rows),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:status"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.test == "g-test-k"
    assert r.direction == "mixed"
    assert r.baseline_n == 1000
    assert r.window_n == 1000
    # Shares .7/.2/.1 → .2/.3/.5: TVD = 0.5·(0.5 + 0.1 + 0.4) = 0.5.
    assert abs(r.effect - 0.5) < 1e-9
    assert abs(r.statistic - _g_statistic_k([700, 200, 100], [200, 300, 500])) < 1e-3
    assert r.details["df"] == 2
    assert r.details["k_truncated"] is False
    assert r.details["k_categories"] == 3
    # Most-shifted category is 'a' (delta -0.5); still present in the window,
    # so its first window occurrence represents the finding.
    assert r.details["top_contributors"][0]["value"] == "a"
    assert r.event_id == "evt-w-a"
    assert r.details["allowlist_value"] == "*"


def test_distribution_drift_categorical_vanished_top_category_uses_baseline_event():
    """When the most-shifted category vanished, its last baseline event represents it."""
    rows = [
        ("gone", 800, datetime(2024, 1, 14, tzinfo=UTC), "evt-bl-gone", 0, None, ""),
        _cat_row("kept", 200, 900),
    ]
    responses = [
        FakeQueryResult(result_rows=[(2000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 1000)], column_names=["bl", "w0"]),
        _drift_probe(numeric=False),
        _drift_cat_rows(rows),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:service"], windows=_shift_windows()
    )
    r = result.results[0]
    assert r.details["top_contributors"][0]["value"] == "gone"
    assert r.event_id == "evt-bl-gone"
    assert r.first_seen is None


def test_distribution_drift_fdr_gates_insignificant():
    """A weak KS (p=0.5) never survives the q gate."""
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        _drift_ks_row(d=0.02, p=0.5),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert result.results == []


def test_distribution_drift_ks_effect_floor():
    """Significant p but D below min_ks_d is suppressed (300M-row guard)."""
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        _drift_ks_row(d=0.05, p=1e-12),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows(), min_ks_d=0.1
    )
    assert result.status == "ok"
    assert result.results == []


def test_distribution_drift_tvd_effect_floor():
    """Hugely significant but tiny categorical shift fails the TVD floor."""
    rows = [_cat_row("a", 100000, 103000), _cat_row("b", 100000, 97000)]
    responses = [
        FakeQueryResult(result_rows=[(400000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(200000, 200000)], column_names=["bl", "w0"]),
        _drift_probe(numeric=False),
        _drift_cat_rows(rows),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:status"], windows=_shift_windows(), min_tvd=0.05
    )
    assert result.status == "ok"
    assert result.results == []


def test_distribution_drift_min_sample_floor_skips_test():
    """A window side below min_samples is skipped (not pooled) with a warning."""
    responses = [
        FakeQueryResult(result_rows=[(1010,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 10)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        _drift_ks_row(w_n=10),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows(), min_samples=20
    )
    # The only candidate test was skipped → nothing evaluated.
    assert result.status == "insufficient_data"
    assert any("skipped" in w and "attr:duration" in w for w in result.warnings)


def test_distribution_drift_allowlist_suppression():
    """A field-level allowlist entry (field, '*') suppresses its findings."""
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        _drift_ks_row(),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1",
        ["s1"],
        fields=["attr:duration"],
        windows=_shift_windows(),
        allowlist={("attr:duration", "*")},
    )
    assert result.status == "ok"
    assert result.results == []


def test_distribution_drift_tiny_window_warning():
    """_window_size_warnings passes through for small suspect windows."""
    responses = [
        FakeQueryResult(result_rows=[(1030,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 30)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        _drift_ks_row(w_n=30),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows()
    )
    assert any("only 30 events" in w for w in result.warnings)


def test_distribution_drift_topk_other_bucket():
    """Categories beyond the top-K fold into one exact __other__ bucket."""
    from vestigo.db import anomaly_stats

    # 52 categories: 50 named + 2 folded. The two tail categories (lowest
    # baseline counts) shift hard into the window.
    rows = [_cat_row(f"v{i:02d}", 1000 - i, 500) for i in range(50)]
    rows += [_cat_row("tail1", 10, 5000), _cat_row("tail2", 5, 5000)]
    responses = [
        FakeQueryResult(result_rows=[(200000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(60000, 40000)], column_names=["bl", "w0"]),
        _drift_probe(numeric=False),
        _drift_cat_rows(rows),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:url"], windows=_shift_windows()
    )
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.details["k_categories"] == anomaly_stats._DRIFT_TOP_K
    assert r.details["k_truncated"] is True
    assert r.details["other_baseline"] == 15
    assert r.details["other_window"] == 10000
    # df bounded: 50 named + other = 51 buckets → df = 50.
    assert r.details["df"] == anomaly_stats._DRIFT_TOP_K


def test_distribution_drift_sql_shapes():
    """KS scan filters NULL numerics; categorical scan groups with the cap."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(3000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(2000, 1000)], column_names=["bl", "w0"]),
            FakeQueryResult(
                result_rows=[(95, 100, 0, 100)], column_names=["num0", "ne0", "num1", "ne1"]
            ),
            _drift_ks_row(),
            _drift_cat_rows([_cat_row("a", 900, 100), _cat_row("b", 100, 900)]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration", "attr:status"], windows=_shift_windows()
    )
    assert result.status == "ok"
    ks_sql = client.full_queries[3]
    assert "kolmogorovSmirnovTestIf('two-sided')" in ks_sql
    assert "WHERE num IS NOT NULL" in ks_sql
    assert "toFloat64OrNull" in ks_sql
    cat_sql = client.full_queries[4]
    assert "GROUP BY val" in cat_sql
    assert "LIMIT {catcap:UInt32}" in cat_sql
    assert "ORDER BY baseline_cnt DESC, val ASC" in cat_sql
    # Both branches' tests share one BH pool.
    assert all(r.details["m_tests"] == 2 for r in result.results)


def test_distribution_drift_equal_medians_reads_spread():
    """Significant D with unchanged median = pure shape change, not 'down'."""
    responses = [
        FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
        _drift_probe(numeric=True),
        # Same median (5.0); window widened symmetrically but the high tail
        # moved out further → representative = max event.
        _drift_ks_row(bl_q=(2.0, 5.0, 8.0), w_q=(1.0, 5.0, 12.0)),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:duration"], windows=_shift_windows()
    )
    r = result.results[0]
    assert r.direction == "spread"
    assert r.event_id == "evt-hi"
    assert r.details["baseline_median"] == r.details["window_median"]


def test_distribution_drift_other_bucket_can_headline_contributors():
    """A drift driven by the folded tail names __other__ as top contributor,
    while the representative event still comes from the best named category."""
    # 50 stable named categories, tail explodes in the window.
    rows = [_cat_row(f"v{i:02d}", 100, 100) for i in range(50)]
    rows += [_cat_row("tail1", 5, 4000), _cat_row("tail2", 5, 4000)]
    responses = [
        FakeQueryResult(result_rows=[(30000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(10000, 20000)], column_names=["bl", "w0"]),
        _drift_probe(numeric=False),
        _drift_cat_rows(rows),
    ]
    svc = _svc(responses)
    result = svc.find_distribution_drift(
        "c1", ["s1"], fields=["attr:url"], windows=_shift_windows()
    )
    r = result.results[0]
    tops = r.details["top_contributors"]
    assert tops[0]["value"] == "__other__"
    assert tops[0]["delta"] > 0
    # Representative event never points at the bucket — best named category.
    assert r.event_id.startswith("evt-w-v")


def test_distribution_drift_probe_is_windowed():
    """Field-classification probes carry the baseline+suspect predicate —
    classification must not pay a whole-case scan the tests never read."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1500,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[(1000, 500)], column_names=["bl", "w0"]),
            FakeQueryResult(result_rows=[(95, 100)], column_names=["num0", "ne0"]),
            _drift_ks_row(),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_distribution_drift("c1", ["s1"], fields=["attr:duration"], windows=_shift_windows())
    probe_sql = client.full_queries[2]
    assert "toFloat64OrNull" in probe_sql
    assert "{b0:String}" in probe_sql and "{w0s:String}" in probe_sql


# ---------------------------------------------------------------------------
# list_log_templates (W6)
# ---------------------------------------------------------------------------


def _template_rows(n_groups: int = 1) -> FakeQueryResult:
    """One listing row. The trailing `n_groups` is the `count() OVER ()`
    window value — the pre-LIMIT template total rides on every row, so the
    listing is a single scan rather than a listing plus a counting query."""
    return FakeQueryResult(
        result_rows=[
            (
                "12345",
                "Allow TCP <IP>:<NUM> -> <IP>:<NUM>",
                3,
                1,
                datetime(2026, 1, 1, tzinfo=UTC),
                datetime(2026, 1, 2, tzinfo=UTC),
                "Allow TCP 10.0.0.5:4433 -> 10.0.0.9:443",
                n_groups,
            )
        ],
        column_names=[
            "template_id",
            "template",
            "cnt",
            "distinct_sources",
            "first_seen",
            "last_seen",
            "example",
            "n_groups",
        ],
    )


def test_list_log_templates_default_field_uses_indexed_column():
    client = RecordingClient([_template_rows()])
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.list_log_templates("c1", ["s1"])
    assert result.field == "message"
    assert result.total_templates == 1
    assert len(result.templates) == 1
    row = result.templates[0]
    assert row.template_id == "12345"
    assert row.count == 3
    assert row.distinct_sources == 1
    assert row.example == "Allow TCP 10.0.0.5:4433 -> 10.0.0.9:443"
    # Indexed fast path groups by the materialized column, not a recomputed hash.
    assert "template_hash AS th" in client.full_queries[0]
    assert "cityHash64" not in client.full_queries[0]


def test_list_log_templates_non_message_field_computes_hash_inline():
    client = RecordingClient([_template_rows()])
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.list_log_templates("c1", ["s1"], field="attr:raw_line")
    sql = client.full_queries[0]
    assert "cityHash64(replaceRegexpAll(" in sql
    assert "attributes[{fk:String}]" in sql
    assert "!= ''" in sql  # empty-value guard applies to non-message fields


def test_list_log_templates_only_new_requires_baseline_end():
    svc = _svc([])
    try:
        svc.list_log_templates("c1", ["s1"], only_new=True)
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "baseline_end" in str(e)


def test_list_log_templates_only_new_filters_on_first_seen():
    client = RecordingClient([_template_rows()])
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.list_log_templates(
        "c1", ["s1"], only_new=True, baseline_end=datetime(2026, 1, 1, tzinfo=UTC)
    )
    assert "first_seen >= {baseline_end:String}" in client.full_queries[0]
    assert "baseline_end" in client._all_parameters[0]


def test_list_log_templates_rejects_unknown_order():
    svc = _svc([])
    try:
        svc.list_log_templates("c1", ["s1"], order="bogus")
        raise AssertionError("expected ValueError")
    except ValueError as e:
        assert "order" in str(e)


def test_list_log_templates_order_and_limit_applied():
    client = RecordingClient([_template_rows()])
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.list_log_templates("c1", ["s1"], order="first_seen", limit=25)
    sql = client.full_queries[0]
    assert "ORDER BY first_seen ASC" in sql
    assert client._all_parameters[0]["lim"] == 25


def test_list_log_templates_totals_come_from_one_scan():
    """`total_templates` is the window count carried on the listing rows —
    a second counting query would re-run the whole regex chain and GROUP BY."""
    client = RecordingClient([_template_rows(n_groups=42)])
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.list_log_templates("c1", ["s1"], limit=1)
    assert result.total_templates == 42
    assert len(result.templates) == 1
    assert len(client.full_queries) == 1
    assert "count() OVER () AS n_groups" in client.full_queries[0]


def test_list_log_templates_empty_result_reports_zero_total():
    client = RecordingClient([FakeQueryResult(result_rows=[], column_names=[])])
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.list_log_templates("c1", ["s1"])
    assert result.total_templates == 0
    assert result.templates == []


def test_charset_group_field_partitions_alphabets():
    """group_field learns one alphabet per group: a char common for host-a but
    unseen for host-b flags only on host-b's values (D14)."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # Grouped learning scan: (grp, c, n_vals_with_c, n_vals). NUL is common
        # for host-a (50 distinct values) but never seen for host-b.
        FakeQueryResult(
            result_rows=[
                ("host-a", "a", 90, 100),
                ("host-a", "b", 85, 100),
                ("host-a", "\x00", 50, 100),
                ("host-b", "a", 95, 100),
                ("host-b", "b", 90, 100),
            ],
            column_names=["grp", "c", "n", "n_vals"],
        ),
        # One violation scan for every group — host-b's row is the only one
        # whose value carries a character outside its own group's alphabet.
        FakeQueryResult(
            result_rows=[("ab\x00", "host-b", ["\x00"], 2, fs, "evt-nul", 0.0)],
            column_names=["val", "grp", "novel", "cnt", "first_seen", "evt_id", "score"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    assert result.status == "ok"
    assert len(result.results) == 1
    f = result.results[0]
    assert f.details["group_field"] == "attr:host"
    assert f.details["group_value"] == "host-b"
    assert f.details["group_basis"] == "scope"
    # Allowlist identity is unchanged: (field, value), group-independent, so
    # existing suppressions keep matching.
    assert f.details["allowlist_field"] == "attr:user"
    assert f.details["allowlist_value"] == "ab\x00"
    # Scored against host-b's own denominators, not the merged ones.
    assert f.details["baseline_distinct_values"] == 100
    # One scan for both groups (count + learn + violation = 3 queries), with the
    # per-group references carried in as parallel arrays.
    assert len(svc.ch.client._all_parameters) == 3
    viol = svc.ch.client._all_parameters[-1]
    assert viol["grps"] == ["host-a", "host-b"]
    assert [("\x00" in ref) for ref in viol["sets"]] == [True, False]
    assert viol["has_fb"] == 0


def test_charset_group_field_sql_groups_learning_scan():
    """With group_field, the learning scans and the skip guards go per-group."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    joined = "\n".join(client.full_queries)
    assert "AS grp" in joined
    assert "GROUP BY grp" in joined
    # The group column resolves through _col_expr with its own param prefix,
    # cast so a non-String column can never blow up the comparison.
    assert "toString(attributes[{gk:String}])" in joined


def test_charset_no_group_field_keeps_current_sql():
    """Default (no group_field) is bit-identical to the pre-D14 shape."""
    client = RecordingClient([FakeQueryResult(result_rows=[], column_names=[])])
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"])
    joined = "\n".join(client.full_queries)
    assert "grp" not in joined
    assert "{gk:String}" not in joined


def test_charset_group_field_scans_once_per_field():
    """Grouped runs scan `events` once per field, not once per group: the
    per-group references travel in as arrays picked by indexOf, and each group
    keeps its own finding budget via LIMIT ... BY grp."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[(f"host-{i}", "a", 90, 100) for i in range(40)],
                column_names=["grp", "c", "n", "n_vals"],
            ),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    # 40 groups, still one violation scan (count + learn + violation).
    assert len(client.full_queries) == 3
    viol = client.full_queries[-1]
    assert "indexOf({grps:Array(String)}, grp) AS gidx" in viol
    assert "{sets:Array(Array(String))}[greatest(gidx, 1)]" in viol
    assert "LIMIT {plim:UInt32} BY grp" in viol
    assert "LIMIT {tlim:UInt32}" in viol
    # No per-group equality pin — that was the per-group-scan shape.
    assert "{gval:String}" not in viol


def test_charset_group_field_temporal_falls_back_outside_suspect_windows():
    """A group the baseline window never saw is scored against a reference
    learned outside the suspect windows — not skipped, and not against a
    whole-scope alphabet that would contain the suspect values themselves."""
    fs = datetime(2024, 1, 1, 12, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # Per-group baseline alphabets: only host-a is in the baseline window.
        FakeQueryResult(
            result_rows=_alphabet_rows(*[("host-a", ["a", "b"], 60)]),
            column_names=_ALPHABET_COLUMNS,
        ),
        # Fallback learn (rare-chars outside the suspect windows).
        FakeQueryResult(
            result_rows=[("a", 80, 90), ("b", 70, 90)],
            column_names=["c", "n_vals_with_c", "n_vals"],
        ),
        # host-b was never in the baseline: scored by the fallback.
        FakeQueryResult(
            result_rows=[("abа", "host-b", ["а"], 3, fs, "evt-hom", 0, 0.0)],
            column_names=[
                "val",
                "grp",
                "novel",
                "cnt",
                "first_seen",
                "evt_id",
                "win_idx",
                "score",
            ],
        ),
    ]
    # The probe runs before the fallback learn: host-a is known, host-b is not,
    # so the fallback is worth paying for.
    responses.insert(
        2,
        FakeQueryResult(result_rows=[("host-a",), ("host-b",)], column_names=["grp"]),
    )
    client = RecordingClient(responses)
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_charset_novelty(
        "c1",
        ["s1"],
        fields=["attr:user"],
        windows=_one_suspect(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 11, tzinfo=UTC),
            datetime(2024, 1, 12, tzinfo=UTC),
        ),
        group_field="attr:host",
    )
    assert result.status == "ok"
    f = result.results[0]
    assert f.details["group_value"] == "host-b"
    assert f.details["group_basis"] == "outside-suspect-windows"
    # The fallback came from the rare-chars recipe, so its per-character counts
    # are reportable — a baseline-window reference has none.
    assert f.details["char_value_counts"] == {"а": 0}
    # host-b contributed no evidence of its own — that is *why* a fallback
    # scored it, and the finding says so rather than leaving it to be inferred.
    assert f.details["group_baseline_distinct_values"] == 0
    assert f.details["baseline_distinct_values"] == 90
    # The fallback learn excluded the suspect windows, or a suspect-window value
    # would sit in its own reference and mask itself.
    assert "AND NOT (" in client.full_queries[3]
    viol = client._all_parameters[-1]
    assert viol["has_fb"] == 1
    assert viol["grps"] == ["host-a"]
    assert viol["skip"] == []
    # …and the run says so.
    assert any("no baseline-window values" in w and "host-b" in w for w in result.warnings)


def test_charset_group_field_skips_fallback_learn_when_no_group_needs_it():
    """The whole-scope fallback learn is a heavy scan, so it only runs when the
    probe finds a suspect-window group with no baseline reference."""
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=_alphabet_rows(*[("host-a", ["a", "b"], 60), ("host-b", ["a"], 40)]),
            column_names=_ALPHABET_COLUMNS,
        ),
        # Probe: every suspect-window group already has a baseline reference.
        FakeQueryResult(
            result_rows=[("host-a",), ("host-b",)],
            column_names=["grp"],
        ),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    client = RecordingClient(responses)
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_charset_novelty(
        "c1",
        ["s1"],
        fields=["attr:user"],
        windows=_one_suspect(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 11, tzinfo=UTC),
            datetime(2024, 1, 12, tzinfo=UTC),
        ),
        group_field="attr:host",
    )
    # count + baseline learn + probe + violation — no fallback learn.
    assert len(client.full_queries) == 4
    assert not any("ARRAY JOIN chars" in q and "AND NOT (" in q for q in client.full_queries)
    assert client._all_parameters[-1]["has_fb"] == 0
    # Nothing deviated, so nothing to warn about.
    assert result.warnings == []


def test_charset_group_probe_at_ceiling_assumes_a_fallback_is_needed():
    """A probe that hits its row ceiling cannot prove every group is covered,
    so it answers 'needed' — an unchecked group is never assumed safe."""
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=_alphabet_rows(
                *[(f"host-{i}", ["a"], 60) for i in range(_CHARSET_GROUP_PROBE_LIMIT)]
            ),
            column_names=_ALPHABET_COLUMNS,
        ),
        # Probe returns exactly the ceiling — all known, but truncated.
        FakeQueryResult(
            result_rows=[(f"host-{i}",) for i in range(_CHARSET_GROUP_PROBE_LIMIT)],
            column_names=["grp"],
        ),
        FakeQueryResult(
            result_rows=[("a", 80, 90)],
            column_names=["c", "n_vals_with_c", "n_vals"],
        ),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    client = RecordingClient(responses)
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_charset_novelty(
        "c1",
        ["s1"],
        fields=["attr:user"],
        windows=_one_suspect(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 11, tzinfo=UTC),
            datetime(2024, 1, 12, tzinfo=UTC),
        ),
        group_field="attr:host",
    )
    # The fallback learn ran despite every probed group being known.
    assert len(client.full_queries) == 5
    assert client._all_parameters[-1]["has_fb"] == 1


def test_charset_wide_group_is_dropped_not_scored_by_fallback():
    """The two guards mean opposite things. A group whose alphabet is too wide
    fails the detector's *premise* — no reference makes 'novel character'
    meaningful there — so it is excluded from the scan rather than scored
    against the fallback."""
    fs = datetime(2024, 1, 1, 12, tzinfo=UTC)
    wide_charset = [chr(0x4E00 + i) for i in range(_MAX_CHARSET_SIZE + 1)]
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=_alphabet_rows(
                ("host-a", ["a", "b"], 60),
                # host-prose is CJK free text: alphabet over the ceiling.
                ("host-prose", wide_charset, 500),
            ),
            column_names=_ALPHABET_COLUMNS,
        ),
        # Probe: no group missing a reference, so no fallback learn.
        FakeQueryResult(result_rows=[("host-a",), ("host-prose",)], column_names=["grp"]),
        # Defensive: even if a host-prose row reached Python it is not scored.
        FakeQueryResult(
            result_rows=[("文字", "host-prose", ["文"], 2, fs, "evt-cjk", 0, 0.0)],
            column_names=[
                "val",
                "grp",
                "novel",
                "cnt",
                "first_seen",
                "evt_id",
                "win_idx",
                "score",
            ],
        ),
    ]
    client = RecordingClient(responses)
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_charset_novelty(
        "c1",
        ["s1"],
        fields=["attr:user"],
        windows=_one_suspect(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 11, tzinfo=UTC),
            datetime(2024, 1, 12, tzinfo=UTC),
        ),
        group_field="attr:host",
    )
    assert result.results == []
    viol = client._all_parameters[-1]
    # Carried out of the scan explicitly, so `gidx = 0` keeps one meaning.
    assert viol["skip"] == ["host-prose"]
    assert "NOT has({skip:Array(String)}, grp)" in client.full_queries[-1]
    assert any(
        "not evaluated" in w and "host-prose" in w and "novel character carries no signal" in w
        for w in result.warnings
    )


def test_charset_thin_group_is_scored_by_fallback_not_dropped():
    """A group with too few values of its own is short of *evidence*, not
    exonerated — 'absent from the baseline window' is just n_vals = 0, the
    degenerate case of the same condition, so both route to the fallback."""
    fs = datetime(2024, 1, 1, 12, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=_alphabet_rows(*[("host-a", ["a", "b"], 60), ("host-new", ["a"], 3)]),
            column_names=_ALPHABET_COLUMNS,
        ),
        # Probe: both groups have a reference, thin as host-new's is.
        FakeQueryResult(
            result_rows=[("host-a",), ("host-new",)],
            column_names=["grp"],
        ),
        FakeQueryResult(
            result_rows=[("a", 80, 90), ("b", 70, 90)],
            column_names=["c", "n_vals_with_c", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[("abа", "host-new", ["а"], 3, fs, "evt-hom", 0, 0.0)],
            column_names=[
                "val",
                "grp",
                "novel",
                "cnt",
                "first_seen",
                "evt_id",
                "win_idx",
                "score",
            ],
        ),
    ]
    client = RecordingClient(responses)
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_charset_novelty(
        "c1",
        ["s1"],
        fields=["attr:user"],
        windows=_one_suspect(
            datetime(2024, 1, 1, tzinfo=UTC),
            datetime(2024, 1, 10, tzinfo=UTC),
            datetime(2024, 1, 11, tzinfo=UTC),
            datetime(2024, 1, 12, tzinfo=UTC),
        ),
        group_field="attr:host",
    )
    # count + baseline learn + probe + fallback learn + violation.
    assert len(client.full_queries) == 5
    f = result.results[0]
    assert f.details["group_value"] == "host-new"
    assert f.details["group_basis"] == "outside-suspect-windows"
    # It had evidence, just not enough — distinct from the absent case.
    assert f.details["group_baseline_distinct_values"] == 3
    assert any(
        "fewer than 20 distinct values of their own" in w and "host-new" in w
        for w in result.warnings
    )
    assert not any("no baseline-window values" in w for w in result.warnings)


def test_charset_self_baseline_thin_group_falls_back_to_merged_scope():
    """Before group_field, a thin group was scored against the merged
    whole-scope alphabet. Enabling grouping must not delete it from the run, so
    self-baseline mode falls back to exactly that reference."""
    fs = datetime(2024, 1, 1, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("host-a", "a", 90, 100),
                ("host-tiny", "a", 4, 5),
            ],
            column_names=["grp", "c", "n", "n_vals"],
        ),
        # Merged whole-scope learn — the self-baseline fallback.
        FakeQueryResult(
            result_rows=[("a", 95, 105), ("b", 90, 105)],
            column_names=["c", "n_vals_with_c", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[("ab\x00", "host-tiny", ["\x00"], 2, fs, "evt-nul", 0.0)],
            column_names=["val", "grp", "novel", "cnt", "first_seen", "evt_id", "score"],
        ),
    ]
    client = RecordingClient(responses)
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    f = result.results[0]
    assert f.details["group_value"] == "host-tiny"
    assert f.details["group_basis"] == "scope-merged"
    assert f.details["group_baseline_distinct_values"] == 5
    # Self-baseline has no suspect windows to exclude from the fallback learn.
    assert "AND NOT (" not in client.full_queries[2]
    assert any("merged whole-scope alphabet" in w and "host-tiny" in w for w in result.warnings)


def test_charset_thin_group_without_usable_fallback_is_named_in_warnings():
    """When the fallback itself fails the guards there is nothing left to score
    against, and the run names the groups that lost out."""
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[("host-a", "a", 90, 100), ("host-tiny", "a", 4, 5)],
            column_names=["grp", "c", "n", "n_vals"],
        ),
        # Fallback learn is itself too thin to be a reference.
        FakeQueryResult(
            result_rows=[("a", 3, 4)],
            column_names=["c", "n_vals_with_c", "n_vals"],
        ),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    assert svc.ch.client._all_parameters[-1]["has_fb"] == 0
    assert any(
        "no fallback reference could be learned" in w and "host-tiny" in w for w in result.warnings
    )
    # Self-baseline mode has no "absent from the baseline window" case at all,
    # so that warning must not appear.
    assert not any("absent from the baseline window" in w for w in result.warnings)


def test_charset_group_field_reports_skipped_groups():
    """Only groups with a usable reference of their own travel into the scan as
    `grps`; a thin one is named in warnings and routed to the fallback rather
    than sharing another group's alphabet."""
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[
                ("host-a", "a", 90, 100),
                # host-tiny has 5 distinct values — below _MIN_CHARSET_BASELINE.
                ("host-tiny", "a", 4, 5),
            ],
            column_names=["grp", "c", "n", "n_vals"],
        ),
        # Merged whole-scope fallback, learned because host-tiny is thin.
        FakeQueryResult(
            result_rows=[("a", 95, 105)],
            column_names=["c", "n_vals_with_c", "n_vals"],
        ),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    assert any(
        "fewer than 20 distinct values of their own" in w and "host-tiny" in w
        for w in result.warnings
    )
    # Only the evaluated group's reference travelled into the scan; host-tiny
    # reaches the fallback through `gidx = 0`, not through someone else's set.
    viol = svc.ch.client._all_parameters[-1]
    assert viol["grps"] == ["host-a"]
    assert viol["has_fb"] == 1
    assert viol["skip"] == []


def test_charset_grouped_row_ceiling_is_reported_not_silent():
    """The 5,000-row ceiling sits *under* the per-group budget and orders by
    novelty across every group, so hitting it drops whole low-novelty groups.
    A truncated scan that came back looking clean would read as 'these are the
    groups with novel characters'."""
    fs = datetime(2024, 1, 1, 12, tzinfo=UTC)
    ceiling = _MAX_CHARSET_GROUPED_ROWS
    responses = [
        FakeQueryResult(result_rows=[(1_000_000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[(f"host-{i}", "a", 90, 100) for i in range(ceiling)],
            column_names=["grp", "c", "n", "n_vals"],
        ),
        # Exactly the ceiling: the query hit its LIMIT, so more rows existed.
        FakeQueryResult(
            result_rows=[
                (f"ab\x00{i}", f"host-{i}", ["\x00"], 2, fs, f"evt-{i}", 0.0)
                for i in range(ceiling)
            ],
            column_names=["val", "grp", "novel", "cnt", "first_seen", "evt_id", "score"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    assert result.status == "ok"
    assert any(f"{ceiling}-row ceiling" in w and "attr:user" in w for w in result.warnings), (
        result.warnings
    )


def test_charset_grouped_row_ceiling_is_quiet_below_it():
    """The ceiling warning must not fire on a run that simply returned fewer
    rows — it is a statement about truncation, not about volume."""
    fs = datetime(2024, 1, 1, 12, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        FakeQueryResult(
            result_rows=[("host-a", "a", 90, 100)],
            column_names=["grp", "c", "n", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[("ab\x00", "host-a", ["\x00"], 2, fs, "evt-nul", 0.0)],
            column_names=["val", "grp", "novel", "cnt", "first_seen", "evt_id", "score"],
        ),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="attr:host")
    assert not any("row ceiling" in w for w in result.warnings)


def test_charset_fallback_warnings_name_the_field():
    """The same group name can be thin for one field and absent from the
    baseline window for another. Merging them into one flat count would leave
    an analyst no way to tell which field a warning is about."""
    fs = datetime(2024, 1, 1, 12, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
        # attr:user — host-x is thin.
        FakeQueryResult(
            result_rows=[("host-a", "a", 90, 100), ("host-x", "a", 4, 5)],
            column_names=["grp", "c", "n", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[("a", 95, 105)],
            column_names=["c", "n_vals_with_c", "n_vals"],
        ),
        FakeQueryResult(
            result_rows=[("ab\x00", "host-x", ["\x00"], 2, fs, "evt-u", 0.0)],
            column_names=["val", "grp", "novel", "cnt", "first_seen", "evt_id", "score"],
        ),
        # attr:path — every group is well-evidenced, so no fallback is needed.
        FakeQueryResult(
            result_rows=[("host-a", "a", 90, 100)],
            column_names=["grp", "c", "n", "n_vals"],
        ),
        FakeQueryResult(result_rows=[], column_names=[]),
    ]
    svc = _svc(responses)
    result = svc.find_charset_novelty(
        "c1", ["s1"], fields=["attr:user", "attr:path"], group_field="attr:host"
    )
    thin = [w for w in result.warnings if "fewer than 20 distinct values" in w]
    assert len(thin) == 1
    # Named per field, in the `field: group` form the other grouped warnings use.
    assert "attr:user: host-x" in thin[0]
    assert "attr:path" not in thin[0]


def test_charset_rejects_non_string_group_field():
    """A group field that is not a String column would be a ClickHouse type
    error, so it is refused before any query runs."""
    svc = _svc([])
    with pytest.raises(ValueError, match="not a string field"):
        svc.find_charset_novelty("c1", ["s1"], fields=["attr:user"], group_field="timestamp")


def test_sequence_max_gap_adds_segment_partition():
    """max_gap_seconds breaks n-grams across gaps: the assembly window
    partitions by a segment counter incremented whenever consecutive events
    are too far apart (D14)."""
    client = RecordingClient(
        _seq_responses(
            total=10_000,
            window_totals=(8000, 2000),
            ngram_totals=[(-1, 7998), (0, 1998)],
            novel_rows=[],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_sequence_novelty(
        "c1",
        ["s1"],
        series_field="attr:proc",
        ngram=3,
        windows=_seq_windows(),
        max_gap_seconds=300,
    )
    assert result.status == "ok"
    joined = "\n".join(client.full_queries)
    assert "age('second', lagInFrame(ets, 1) OVER ord, ets)" in joined
    assert "PARTITION BY source_id, w_idx, seg" in joined
    assert "if(gap_s > 300, 1, 0)" in joined


def test_sequence_no_max_gap_keeps_current_sql():
    """Default (None) is bit-identical to the pre-D14 shape — no seg anywhere."""
    client = RecordingClient(
        _seq_responses(
            total=10_000,
            window_totals=(8000, 2000),
            ngram_totals=[(-1, 7998), (0, 1998)],
            novel_rows=[],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_sequence_novelty(
        "c1", ["s1"], series_field="attr:proc", ngram=3, windows=_seq_windows()
    )
    joined = "\n".join(client.full_queries)
    assert "seg" not in joined
    assert "PARTITION BY source_id, w_idx" in joined


def test_motif_max_gap_reaches_both_passes():
    """sequence_motif takes the same bound; the cadence pass reuses the inner
    assembly, so one parameter covers support and cadence alike (D14)."""
    client = RecordingClient(
        [
            FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[], column_names=[]),
            FakeQueryResult(result_rows=[], column_names=[]),
        ]
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_sequence_motifs("c1", ["s1"], series_field="attr:proc", max_gap_seconds=300)
    joined = "\n".join(client.full_queries)
    assert "PARTITION BY source_id, w_idx, seg" in joined
    assert "if(gap_s > 300, 1, 0)" in joined


# ---------------------------------------------------------------------------
# Self frame (D18): slices and the pure math behind the cadence null model
# ---------------------------------------------------------------------------


def test_self_slices_are_integer_arithmetic_and_reproducible():
    """Boundaries come from ceil(span/k) milliseconds; a rebuild hashes the same."""
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = datetime(2024, 1, 2, 0, 0, 0, 500_000, tzinfo=UTC)
    a = SelfSlices.build(start, end, 24)
    b = SelfSlices.build(start, end, 24)
    assert a is not None and a == b
    assert a.width_ms == math.ceil(86_400_500 / 24)
    assert a.config_hash() == b.config_hash()
    payload = a.payload()
    assert payload["k"] == 24 and len(payload["slices"]) == 24
    assert payload["slices"][6]["label"] == "slice 07/24"
    # Slice i starts exactly i widths after the span start.
    assert a.bounds(23)[0] == start + timedelta(milliseconds=23 * a.width_ms)


def test_self_slices_index_sql_clamps_the_last_event_into_the_last_slice():
    """least(k-1, …): the span end itself lands in slice k-1, never in a slice k."""
    slices = SelfSlices.build(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC), 24
    )
    params: dict = {}
    sql = slices.index_sql("timestamp", params)
    assert sql.startswith("least(23, ")
    assert params["ss"] == "2024-01-01 00:00:00.000"
    assert params["sw"] == 3_600_000
    assert "{sw:Int64}" in sql and "{ss:String}" in sql


def test_self_slices_reject_a_single_instant():
    t = datetime(2024, 1, 1, tzinfo=UTC)
    assert SelfSlices.build(t, t, 24) is None


def test_self_slices_last_reported_end_never_overruns_the_span():
    """ceil(span/k) can overshoot; the snapshot and the highlight must not.

    The last slice's reported end is what the run snapshot records and what
    the Explorer highlights, so an unclamped value asserts a boundary past
    any event that exists.
    """
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(milliseconds=100)  # 100 ms over k=8 -> width 13 ms
    slices = SelfSlices.build(start, end, 8)
    assert slices is not None and slices.width_ms == 13
    assert 8 * slices.width_ms == 104 > 100  # the raw arithmetic does overrun
    assert slices.bounds(7)[1] == end
    assert slices.bounds(6)[1] == start + timedelta(milliseconds=91)
    assert slices.payload()["slices"][7]["end"] == end.isoformat()
    # Every reported interval stays inside the span.
    assert all(slices.bounds(i)[1] <= end for i in range(8))


def test_self_slices_cover_treats_the_last_slice_as_closed_on_the_right():
    """A value first seen at the span end belongs to the last slice.

    Slices are half-open so an instant falls in exactly one of them, but the
    SQL index folds everything at or past the last slice's start into k-1.
    Comparing against the *clamped* end would drop a value whose first
    occurrence is the very last event.
    """
    start = datetime(2024, 1, 1, tzinfo=UTC)
    end = start + timedelta(milliseconds=100)
    slices = SelfSlices.build(start, end, 8)
    assert slices is not None
    assert slices.covers(7, end, end)  # first == last == span end
    assert not slices.covers(0, end, end)
    # A value living entirely in slice 0 touches no later slice.
    early = start + timedelta(milliseconds=5)
    assert slices.covers(0, start, early)
    assert not slices.covers(3, start, early)
    # A value spanning the middle covers every slice it overlaps.
    mid_lo, mid_hi = start + timedelta(milliseconds=20), start + timedelta(milliseconds=45)
    assert [i for i in range(8) if slices.covers(i, mid_lo, mid_hi)] == [1, 2, 3]


def test_spend_ks_budget_serves_every_field_before_serving_any_twice():
    """Round-robin, not a prefix: the budget narrows resolution, not coverage.

    A prefix truncation would give the first fields every slice and the last
    fields none, so the run would report on a field it never scanned.
    """
    eligible = [[0, 1, 2, 3], [0, 1, 2, 3], [0, 1, 2, 3]]
    picked = _spend_ks_budget(eligible, 6)
    assert len(picked) == 6
    # Two slices from each field, not four from the first and two from the second.
    assert sorted(fi for fi, _ in picked) == [0, 0, 1, 1, 2, 2]
    # Earliest slices first, and field-major order out.
    assert picked == [(0, 0), (0, 1), (1, 0), (1, 1), (2, 0), (2, 1)]


def test_spend_ks_budget_backfills_the_slack_a_short_field_leaves():
    """A field with fewer eligible slices than its share does not waste it."""
    eligible = [[5], [0, 1, 2, 3, 4]]
    picked = _spend_ks_budget(eligible, 4)
    assert len(picked) == 4
    assert picked == [(0, 5), (1, 0), (1, 1), (1, 2)]


def test_spend_ks_budget_returns_everything_when_the_budget_is_not_binding():
    eligible = [[0, 1], [2]]
    assert _spend_ks_budget(eligible, 60) == [(0, 0), (0, 1), (1, 2)]
    assert _spend_ks_budget([], 60) == []
    assert _spend_ks_budget([[0, 1]], 0) == []


def test_small_slice_warning_counts_empty_slices_separately():
    """An empty slice is tested by nobody and skipped by nobody — say so.

    A slice with no events produces no test and no skipped_by_field entry, so
    a sparse timeline advertises k slices while a handful carry every test.
    """
    slices = SelfSlices.build(
        datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 2, tzinfo=UTC), 24
    )
    assert slices is not None
    svc = StatisticalAnomalyService
    # 20 empty, 2 thin, 2 healthy.
    totals = [0] * 20 + [1, 2] + [10_000, 10_000]
    notes = svc._small_slice_warning(slices, totals)
    assert len(notes) == 2
    assert "20 of 24 slices hold no events" in notes[0]
    assert "clustered in 4 of its 24" in notes[0]
    assert "fewer than" in notes[1] and "2 of 24" in notes[1]
    # Nothing empty, nothing thin -> nothing to say.
    assert svc._small_slice_warning(slices, [10_000] * 24) == []
    # Empty only.
    only_empty = svc._small_slice_warning(slices, [0] * 12 + [10_000] * 12)
    assert len(only_empty) == 1 and "hold no events" in only_empty[0]


def test_gamma_sf_matches_closed_forms():
    """Shape 1 is the exponential tail; integer shapes agree with the chi² caller."""
    assert abs(_gamma_sf(1.0, 2.0) - math.exp(-2.0)) < 1e-12
    for df in (2, 3, 4, 7, 12):
        for x in (0.5, 2.0, 9.0, 30.0):
            assert abs(_gamma_sf(df / 2.0, x / 2.0) - _chi2_sf(x, df)) < 1e-12
    # Non-integer shape: Q(a, y) is monotone decreasing in y and bounded.
    tail = [_gamma_sf(4.6, y) for y in (0.1, 1.0, 4.6, 10.0, 40.0)]
    assert tail == sorted(tail, reverse=True)
    assert tail[0] > 0.999 and tail[-1] < 1e-9
    assert _gamma_sf(4.6, 0.0) == 1.0


def test_sidak_is_stable_for_tiny_tails():
    """1 − (1 − sf)^k via expm1/log1p keeps a 1e-300 tail from rounding to zero."""
    assert _sidak_p(1e-300, 1000) > 0.0
    assert abs(_sidak_p(0.01, 1) - 0.01) < 1e-15
    assert abs(_sidak_p(0.1, 3) - (1 - 0.9**3)) < 1e-12
    assert _sidak_p(1.0, 5) == 1.0
    assert _sidak_p(0.0, 5) == 0.0


def test_robust_cv_sits_on_the_cv_scale_and_is_floored():
    # Exponential gaps: q1 = ln(4/3)·μ, median = ln2·μ, q3 = ln4·μ → rcv ≈ 1.17.
    mu = 60.0
    rcv = _robust_cv(math.log(4 / 3) * mu, math.log(2) * mu, math.log(4) * mu)
    assert rcv is not None and abs(rcv - 1.175) < 0.01
    # A clockwork job with IQR = 0 is clamped, never zero.
    assert _robust_cv(60.0, 60.0, 60.0) == 0.05
    assert _robust_cv(1.0, 0.0, 2.0) is None


def test_gamma_from_median_recovers_the_median():
    """Wilson–Hilferty inverse: the fitted Gamma's median is the median it was fitted to."""
    rcv, median = 0.25, 300.0
    alpha, theta = _gamma_from_median(rcv, median)
    assert abs(alpha - 16.0) < 1e-9
    # Bisect Q(alpha, y) = 0.5 and compare y·theta to the median.
    lo, hi = 0.0, 1000.0
    for _ in range(200):
        mid = (lo + hi) / 2
        if _gamma_sf(alpha, mid) > 0.5:
            lo = mid
        else:
            hi = mid
    assert abs(lo * theta - median) / median < 0.01


# ---------------------------------------------------------------------------
# transition_time — detector (D15)
# ---------------------------------------------------------------------------

# Baseline frame query order: count, window totals, then per source one
# suspect-candidate scan (Query A) and, when candidates exist, one baseline
# learn scan (Query B). Query A row layout: gram ([from, to]), w_idx,
# fastest_ms, evt, at, pval, n. Query B row layout: gram, min_ms, n.
# Self frame: count, then per source one scan whose rows are gram,
# two_fastest_ms ([m1, m2]), evt, at, pval, n.

_TRANS_CAND_COLS = ["gram", "w_idx", "fastest_ms", "evt", "at", "pval", "n"]
_TRANS_LEARN_COLS = ["gram", "min_ms", "n"]
_TRANS_SELF_COLS = ["gram", "two_fastest_ms", "evt", "at", "pval", "n"]


def _trans_responses(
    total: int,
    window_totals: tuple[int, int],
    cand_rows: list[tuple],
    learn_rows: list[tuple] | None,
) -> list[FakeQueryResult]:
    out = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[window_totals], column_names=["bl_total", "w0_total"]),
        FakeQueryResult(result_rows=cand_rows, column_names=_TRANS_CAND_COLS),
    ]
    if learn_rows is not None:
        out.append(FakeQueryResult(result_rows=learn_rows, column_names=_TRANS_LEARN_COLS))
    return out


def test_transition_min_ratio_validation():
    svc = _svc([])
    with pytest.raises(ValueError, match="min_ratio"):
        svc.find_transition_times("c1", ["s1"], min_ratio=1.0, windows=_seq_windows())
    assert svc.ch.client._calls == []


def test_transition_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_transition_times("c1", ["s1"], windows=_seq_windows())
    assert result.status == "no_data"
    assert result.detector == "transition_time"
    assert result.windows is not None


def test_transition_baseline_flags_a_transition_faster_than_the_learned_floor():
    at = datetime(2024, 1, 17, 12, 0, tzinfo=UTC)
    svc = _svc(
        _trans_responses(
            total=10_000,
            window_totals=(8000, 2000),
            # JUMP-01 → FILE-01 in 3 s during the incident; 4 such transitions.
            cand_rows=[(["JUMP-01", "FILE-01"], 0, 3_000, "evt-1", at, "m.okonkwo", 4)],
            # The baseline never saw it faster than 41 minutes, over 12 transitions.
            learn_rows=[(["JUMP-01", "FILE-01"], 2_460_000, 12)],
        )
    )
    result = svc.find_transition_times(
        "c1",
        ["s1"],
        series_field="attr:computer_name",
        partition_field="attr:user",
        windows=_seq_windows(),
    )
    assert result.status == "ok"
    assert result.method == "min-transition"
    assert result.baseline_size == 8000
    assert len(result.results) == 1
    r = result.results[0]
    assert r.field == "attr:computer_name"
    assert r.values == ["JUMP-01", "FILE-01"]
    assert r.value == "JUMP-01 → FILE-01"
    assert r.partition_field == "attr:user"
    assert r.partition_value == "m.okonkwo"
    assert r.observed_seconds == 3.0
    assert r.reference_seconds == 2460.0
    assert r.reference_kind == "baseline-min"
    assert r.count == 4
    assert r.baseline_count == 12
    assert abs(r.score - (1 - 3 / 2460)) < 1e-5
    assert abs(r.speedup - 820.0) < 1e-9
    assert r.event_id == "evt-1"
    assert r.first_seen is not None and r.first_seen.startswith("2024-01-17T12:00")
    assert r.details["window_label"] == "incident"
    assert r.details["min_ratio"] == 2.0
    assert r.details["min_transitions"] == 3
    assert r.details["allowlist_field"] == "attr:computer_name"
    assert r.details["allowlist_value"] == "JUMP-01 → FILE-01"
    # Both scans ran once for the single source: count, totals, A, B.
    assert len(svc.ch.client._calls) == 4
    # The learn scan is bound to the candidate pairs.
    assert svc.ch.client._all_parameters[3]["cands"] == [["JUMP-01", "FILE-01"]]


def test_transition_baseline_effect_floor_and_learning_floor():
    """A transition merely faster than the floor is not flagged; neither is one
    whose pair the baseline saw too few times, nor one whose learned floor is
    zero (second-resolution logs make zero-length transitions routine)."""
    at = datetime(2024, 1, 17, tzinfo=UTC)
    svc = _svc(
        _trans_responses(
            total=10_000,
            window_totals=(8000, 2000),
            cand_rows=[
                # 1.5x faster than the learned floor — under min_ratio 2.
                (["a", "b"], 0, 20_000, "e1", at, "", 5),
                # 100x faster, but the baseline holds only 2 transitions.
                (["b", "c"], 0, 100, "e2", at, "", 5),
                # Learned floor is 0 — nothing can be faster than instant.
                (["c", "d"], 0, 0, "e3", at, "", 5),
                # 10x faster over a well-learned pair: the one finding.
                (["d", "e"], 0, 1_000, "e4", at, "", 2),
            ],
            learn_rows=[
                (["a", "b"], 30_000, 40),
                (["b", "c"], 10_000, 2),
                (["c", "d"], 0, 40),
                (["d", "e"], 10_000, 40),
            ],
        )
    )
    result = svc.find_transition_times("c1", ["s1"], windows=_seq_windows())
    assert result.status == "ok"
    assert [r.value for r in result.results] == ["d → e"]
    assert result.results[0].partition_field is None
    assert result.results[0].partition_value is None
    assert any("zero" in w for w in result.warnings)


def test_transition_baseline_without_learned_pairs_is_insufficient():
    """No candidate pair has a baseline floor: nothing to compare against."""
    at = datetime(2024, 1, 17, tzinfo=UTC)
    svc = _svc(
        _trans_responses(
            total=10_000,
            window_totals=(8000, 2000),
            cand_rows=[(["x", "y"], 0, 100, "e1", at, "", 3)],
            learn_rows=[],
        )
    )
    result = svc.find_transition_times("c1", ["s1"], windows=_seq_windows())
    assert result.status == "insufficient_data"
    assert any("baseline" in w for w in result.warnings)


def test_transition_baseline_multi_source_merges_floor_and_fastest():
    """Per-source scans: the learned floor is the minimum over every source's
    baseline, counts are summed, and the fastest suspect transition across
    sources supplies the representative event."""
    at_fast = datetime(2024, 1, 16, 8, 0, tzinfo=UTC)
    at_slow = datetime(2024, 1, 17, 8, 0, tzinfo=UTC)
    responses = [
        FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[(8000, 2000)], column_names=["bl_total", "w0_total"]),
        FakeQueryResult(
            result_rows=[(["a", "b"], 0, 5_000, "e-s1", at_slow, "", 2)],
            column_names=_TRANS_CAND_COLS,
        ),
        FakeQueryResult(
            result_rows=[(["a", "b"], 0, 2_000, "e-s2", at_fast, "", 3)],
            column_names=_TRANS_CAND_COLS,
        ),
        FakeQueryResult(result_rows=[(["a", "b"], 60_000, 4)], column_names=_TRANS_LEARN_COLS),
        FakeQueryResult(result_rows=[(["a", "b"], 30_000, 6)], column_names=_TRANS_LEARN_COLS),
    ]
    svc = _svc(responses)
    result = svc.find_transition_times("c1", ["s1", "s2"], windows=_seq_windows())
    assert result.status == "ok"
    assert len(result.results) == 1
    r = result.results[0]
    assert r.observed_seconds == 2.0
    assert r.reference_seconds == 30.0
    assert r.count == 5
    assert r.baseline_count == 10
    assert r.event_id == "e-s2"
    assert len(svc.ch.client._calls) == 6
    for p in svc.ch.client._all_parameters[2:]:
        assert p["src"] in (["s1"], ["s2"])


def test_transition_sql_shape_and_partition_field():
    at = datetime(2024, 1, 17, tzinfo=UTC)
    client = RecordingClient(
        _trans_responses(
            total=10_000,
            window_totals=(8000, 2000),
            cand_rows=[(["a", "b"], 0, 1_000, "e1", at, "u1", 3)],
            learn_rows=[(["a", "b"], 10_000, 9)],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_transition_times(
        "c1",
        ["s1"],
        series_field="attr:computer_name",
        partition_field="attr:user",
        windows=_seq_windows(),
    )
    assert result.status == "ok"
    cand_sql, learn_sql = client.full_queries[2], client.full_queries[3]
    for sql in (cand_sql, learn_sql):
        # A transition is one step of the shared n-gram assembly, per stream.
        assert "PARTITION BY source_id, w_idx, pkey" in sql
        assert "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW" in sql
        assert "guard IS NOT NULL" in sql
        # Same-value repeats are not transitions.
        assert "gram[1] != gram[2]" in sql
        assert "dateDiff('millisecond', first_ts, ets)" in sql
    assert "w_idx >= 0" in cand_sql
    assert "w_idx = -1" in learn_sql
    assert "has({cands:Array(Array(String))}, gram)" in learn_sql
    params = client._all_parameters[2]
    assert params["b0"] == "2024-01-01 00:00:00.000"
    assert params["w0s"] == "2024-01-16 00:00:00.000"
    # Both fields are bound as attribute keys, never inlined.
    assert "attributes[{fk:String}]" in cand_sql
    assert "attributes[{pk:String}]" in cand_sql
    assert params["fk"] == "computer_name"
    assert params["pk"] == "user"


def test_transition_self_frame_uses_the_next_fastest_transition():
    """Without a baseline the reference is leave-one-out: the pair's second
    fastest transition anywhere in the scope."""
    at = datetime(2024, 1, 17, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[
                    # 2 s against a next-fastest of 600 s: flagged.
                    (["JUMP-01", "FILE-01"], [2_000, 600_000], "e1", at, "m.okonkwo", 7),
                    # Two equally fast transitions: leave-one-out floor equals
                    # the observation, nothing is faster than its own twin.
                    (["a", "b"], [1_000, 1_000], "e2", at, "", 9),
                    # Below the transition floor of 3.
                    (["b", "c"], [1, 900_000], "e3", at, "", 2),
                    # 1.5x — under min_ratio.
                    (["c", "d"], [4_000, 6_000], "e4", at, "", 30),
                ],
                column_names=_TRANS_SELF_COLS,
            ),
        ]
    )
    result = svc.find_transition_times(
        "c1", ["s1"], series_field="attr:computer_name", partition_field="attr:user"
    )
    assert result.status == "ok"
    assert result.method == "self-min-transition"
    assert result.windows is None
    assert [r.value for r in result.results] == ["JUMP-01 → FILE-01"]
    r = result.results[0]
    assert r.observed_seconds == 2.0
    assert r.reference_seconds == 600.0
    assert r.reference_kind == "next-fastest"
    assert r.count == 7
    assert r.baseline_count == 7
    assert r.partition_value == "m.okonkwo"
    assert abs(r.score - (1 - 2 / 600)) < 1e-5
    assert r.details["scope_transitions"] == 7
    assert "window_label" not in r.details
    assert len(svc.ch.client._calls) == 2


def test_transition_self_frame_multi_source_takes_the_two_fastest_overall():
    at = datetime(2024, 1, 17, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[(["a", "b"], [50_000, 70_000], "e-s1", at, "", 4)],
                column_names=_TRANS_SELF_COLS,
            ),
            FakeQueryResult(
                result_rows=[(["a", "b"], [1_000, 90_000], "e-s2", at, "", 3)],
                column_names=_TRANS_SELF_COLS,
            ),
        ]
    )
    result = svc.find_transition_times("c1", ["s1", "s2"])
    assert result.status == "ok"
    r = result.results[0]
    # Next-fastest is s1's 50 s, not s2's own 90 s.
    assert r.observed_seconds == 1.0
    assert r.reference_seconds == 50.0
    assert r.count == 7
    assert r.event_id == "e-s2"


def test_transition_self_frame_candidate_cap_warns():
    at = datetime(2024, 1, 17, tzinfo=UTC)
    rows = [([f"h{i}", f"h{i + 1}"], [1_000, 90_000], f"e{i}", at, "", 5) for i in range(3)]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=rows, column_names=_TRANS_SELF_COLS),
        ]
    )
    result = svc.find_transition_times("c1", ["s1"], max_candidates=3)
    assert result.status == "ok"
    assert any("candidate cap" in w for w in result.warnings)
    assert svc.ch.client._all_parameters[1]["cap"] == 3


def test_transition_allowlist_suppresses_the_pair_in_both_frames():
    at = datetime(2024, 1, 17, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[(["a", "b"], [1_000, 90_000], "e1", at, "", 5)],
                column_names=_TRANS_SELF_COLS,
            ),
        ]
    )
    result = svc.find_transition_times("c1", ["s1"], allowlist={("artifact", "a → b")})
    assert result.status == "ok"
    assert result.results == []
    assert result.total_findings == 0


# ---------------------------------------------------------------------------
# time_of_day — detector (D12)
# ---------------------------------------------------------------------------

# Baseline frame query order: count, window totals, then one occupancy scan
# per field. Occupancy row layout: val, bucket, w_idx, cnt, first_ts,
# first_evt — one row per (value, time-of-day bucket, window), baseline rows
# under w_idx = -1. Self frame: count, then one scan per field with every row
# under w_idx = 0.

_HABIT_COLS = ["val", "bucket", "w_idx", "cnt", "first_ts", "first_evt"]


def _habit_responses(
    total: int, window_totals: tuple[int, int], rows: list[tuple]
) -> list[FakeQueryResult]:
    return [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[window_totals], column_names=["bl_total", "w0_total"]),
        FakeQueryResult(result_rows=rows, column_names=_HABIT_COLS),
    ]


def _habit_baseline_rows(val: str, buckets: dict[int, int]) -> list[tuple]:
    return [(val, b, -1, n, None, None) for b, n in buckets.items()]


def test_habit_parameter_validation():
    svc = _svc([])
    with pytest.raises(ValueError, match="bucket_minutes"):
        svc.find_time_of_day_habits("c1", ["s1"], fields=["attr:program"], bucket_minutes=7)
    with pytest.raises(ValueError, match="timezone"):
        svc.find_time_of_day_habits(
            "c1", ["s1"], fields=["attr:program"], timezone="Mars/Olympus_Mons"
        )
    with pytest.raises(ValueError, match="timezone"):
        svc.find_time_of_day_habits("c1", ["s1"], fields=["attr:program"], timezone="UTC'; --")
    assert svc.ch.client._calls == []


def test_habit_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_time_of_day_habits(
        "c1", ["s1"], fields=["attr:program"], windows=_seq_windows()
    )
    assert result.status == "no_data"
    assert result.detector == "time_of_day"


def test_habit_baseline_flags_an_occurrence_outside_the_learned_hours():
    at = datetime(2024, 1, 17, 3, 41, tzinfo=UTC)
    rows = [
        # The nightly backup: 48 baseline runs, all in the 02:00 bucket.
        *_habit_baseline_rows("backup", {2: 48}),
        # Suspect window: eight runs at 03:xx and two at 04:xx.
        ("backup", 3, 0, 8, at, "evt-3"),
        ("backup", 4, 0, 2, at + timedelta(hours=1), "evt-4"),
        # Still at 02:xx in the suspect window — inside the habit, no finding.
        ("backup", 2, 0, 2, at, "evt-2"),
    ]
    svc = _svc(_habit_responses(10_000, (8000, 2000), rows))
    result = svc.find_time_of_day_habits(
        "c1", ["s1"], fields=["attr:program"], windows=_seq_windows()
    )
    assert result.status == "ok"
    assert result.method == "habit"
    assert result.baseline_size == 8000
    by_bucket = {r.bucket: r for r in result.results}
    assert sorted(by_bucket) == [3, 4]
    r = by_bucket[4]
    assert r.field == "attr:program"
    assert r.value == "backup"
    assert r.count == 2
    assert r.baseline_count == 48
    assert r.bucket_minutes == 60
    assert r.timezone == "UTC"
    assert r.bucket_label == "04:00–05:00"
    assert r.habit_buckets == [2]
    assert r.nearest_habit_label == "02:00–03:00"
    assert r.distance_hours == 2.0
    assert r.score == 2.0
    assert r.event_id == "evt-4"
    assert r.first_seen is not None and r.first_seen.startswith("2024-01-17T04:41")
    assert r.details["window_label"] == "incident"
    assert r.details["allowlist_field"] == "attr:program"
    assert r.details["allowlist_value"] == "backup"
    # Ranked farthest-from-habit first.
    assert [f.bucket for f in result.results] == [4, 3]
    assert by_bucket[3].distance_hours == 1.0
    assert len(svc.ch.client._calls) == 3


def test_habit_distance_is_circular_and_buckets_follow_the_resolution():
    """23:xx is one hour from a 00:xx habit, not twenty-three; and a 30-minute
    resolution labels half-hour buckets and measures in half hours."""
    at = datetime(2024, 1, 17, 23, 10, tzinfo=UTC)
    rows = [
        *_habit_baseline_rows("cron", {0: 30, 1: 25}),
        ("cron", 47, 0, 1, at, "e1"),  # 23:30–00:00 at 30-min buckets
        ("cron", 24, 0, 1, at, "e2"),  # 12:00–12:30
    ]
    svc = _svc(_habit_responses(10_000, (8000, 2000), rows))
    result = svc.find_time_of_day_habits(
        "c1", ["s1"], fields=["attr:program"], windows=_seq_windows(), bucket_minutes=30
    )
    assert result.status == "ok"
    by_bucket = {r.bucket: r for r in result.results}
    assert by_bucket[47].bucket_label == "23:30–00:00"
    assert by_bucket[47].distance_hours == 0.5
    assert by_bucket[47].nearest_habit_label == "00:00–00:30"
    assert by_bucket[24].distance_hours == 11.5
    assert by_bucket[24].habit_buckets == [0, 1]


def test_habit_learning_floors_skip_thin_values_and_thin_buckets():
    at = datetime(2024, 1, 17, tzinfo=UTC)
    rows = [
        # Too few baseline occurrences to call anything a habit (floor 20).
        *_habit_baseline_rows("rare", {9: 10}),
        ("rare", 22, 0, 1, at, "e1"),
        # A bucket with fewer than min_bucket_count baseline hits is not habit:
        # 09:xx is habitual, 21:xx (2 hits) is not, so a 21:xx occurrence is a
        # finding measured from 09:xx — and so is the 22:xx one.
        *_habit_baseline_rows("job", {9: 40, 21: 2}),
        ("job", 21, 0, 3, at, "e2"),
        ("job", 22, 0, 1, at, "e3"),
        # Every bucket habitual: nothing can be outside.
        *_habit_baseline_rows("chatty", dict.fromkeys(range(24), 5)),
        ("chatty", 3, 0, 1, at, "e4"),
    ]
    svc = _svc(_habit_responses(10_000, (8000, 2000), rows))
    result = svc.find_time_of_day_habits(
        "c1", ["s1"], fields=["attr:program"], windows=_seq_windows()
    )
    assert result.status == "ok"
    assert sorted((r.value, r.bucket) for r in result.results) == [("job", 21), ("job", 22)]
    assert {r.distance_hours for r in result.results} == {12.0, 11.0}
    assert any("fewer than 20" in w for w in result.warnings)


def test_habit_baseline_without_learnable_values_is_insufficient():
    at = datetime(2024, 1, 17, tzinfo=UTC)
    rows = [*_habit_baseline_rows("rare", {9: 3}), ("rare", 22, 0, 1, at, "e1")]
    svc = _svc(_habit_responses(10_000, (8000, 2000), rows))
    result = svc.find_time_of_day_habits(
        "c1", ["s1"], fields=["attr:program"], windows=_seq_windows()
    )
    assert result.status == "insufficient_data"


def test_habit_sql_shape_binds_resolution_and_inlines_a_validated_timezone():
    at = datetime(2024, 1, 17, tzinfo=UTC)
    client = RecordingClient(
        _habit_responses(
            10_000,
            (8000, 2000),
            # Two-hour buckets: 02:00–04:00 is the habit, 04:00–06:00 the finding.
            [*_habit_baseline_rows("backup", {1: 48}), ("backup", 2, 0, 1, at, "e1")],
        )
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    result = svc.find_time_of_day_habits(
        "c1",
        ["s1"],
        fields=["attr:program"],
        windows=_seq_windows(),
        timezone="Europe/Berlin",
        bucket_minutes=120,
        max_candidates_per_field=7,
    )
    assert result.status == "ok"
    sql = client.full_queries[2]
    # The wall-clock minute in the analyst's zone, cut into the resolution.
    assert "toHour(ts, 'Europe/Berlin') * 60 + toMinute(ts, 'Europe/Berlin')" in sql
    assert "intDiv(" in sql and "{bm:UInt16}" in sql
    assert "GROUP BY val, bucket, w_idx" in sql
    # Candidate values are the highest-volume ones, capped.
    assert "LIMIT {cap:UInt32}" in sql
    params = client._all_parameters[2]
    assert params["bm"] == 120
    assert params["cap"] == 7
    assert params["fk"] == "program"
    assert params["b0"] == "2024-01-01 00:00:00.000"
    r = result.results[0]
    assert r.timezone == "Europe/Berlin"
    assert r.bucket_minutes == 120
    assert r.bucket_label == "04:00–06:00"
    assert r.details["timezone"] == "Europe/Berlin"


def test_habit_self_frame_measures_each_value_against_its_own_hours():
    """Without a baseline the habit is the value's own busy buckets across the
    scope, and a thin bucket away from them is the finding."""
    at = datetime(2024, 1, 17, 15, 2, tzinfo=UTC)
    rows = [
        ("backup", 2, 0, 50, None, None),
        ("backup", 3, 0, 8, None, None),
        # One manual run mid-afternoon: 12 h from the nearest habitual bucket.
        ("backup", 15, 0, 1, at, "e1"),
        # Two runs spilling past 04:00 — thin, one hour from 03:xx.
        ("backup", 4, 0, 2, at, "e2"),
        # A value below the learning floor is skipped.
        ("rare", 9, 0, 10, None, None),
        ("rare", 22, 0, 1, at, "e3"),
    ]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(10_000,)], column_names=["count()"]),
            FakeQueryResult(result_rows=rows, column_names=_HABIT_COLS),
        ]
    )
    result = svc.find_time_of_day_habits("c1", ["s1"], fields=["attr:program"])
    assert result.status == "ok"
    assert result.method == "self-habit"
    assert result.windows is None
    assert [(r.value, r.bucket, r.distance_hours) for r in result.results] == [
        ("backup", 15, 11.0),
        ("backup", 4, 1.0),
    ]
    r = result.results[0]
    assert r.habit_buckets == [2, 3]
    assert r.count == 1
    assert r.baseline_count == 61
    assert r.details["scope_occurrences"] == 61
    assert "window_label" not in r.details
    assert len(svc.ch.client._calls) == 2


def test_habit_auto_fields_run_through_the_recommender_and_overrides():
    """Auto mode scans the recommended categorical fields, minus any the
    timeline declared off, and says so."""
    at = datetime(2024, 1, 17, tzinfo=UTC)
    inventory = [("attr:host", 8, 950), ("attr:program", 12, 900), ("attr:pid", 950, 1000)]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("backup", 2, 0, 48, None, None), ("backup", 14, 0, 1, at, "e1")],
                column_names=_HABIT_COLS,
            ),
        ]
    )
    result = svc.find_time_of_day_habits(
        "c1",
        ["s1"],
        inventory=inventory,
        inventory_total=1000,
        field_overrides={"attr:host": False},
    )
    assert result.status == "ok"
    # One scan for the one field left after the override; pid is an identifier.
    assert len(svc.ch.client._calls) == 2
    assert svc.ch.client._all_parameters[1]["fk"] == "program"
    assert any("attr:host" in w for w in result.warnings)


def test_habit_allowlist_suppresses_the_value():
    at = datetime(2024, 1, 17, tzinfo=UTC)
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(1000,)], column_names=["count()"]),
            FakeQueryResult(
                result_rows=[("backup", 2, 0, 48, None, None), ("backup", 14, 0, 1, at, "e1")],
                column_names=_HABIT_COLS,
            ),
        ]
    )
    result = svc.find_time_of_day_habits(
        "c1", ["s1"], fields=["attr:program"], allowlist={("attr:program", "backup")}
    )
    assert result.status == "ok"
    assert result.results == []


# ---------------------------------------------------------------------------
# value_correlation — detector (D13)
# ---------------------------------------------------------------------------

# Baseline frame query order: count, window totals, then one scan per field
# pair. Row layout: a, b, ref (baseline count), then per suspect window
# (cnt, first_ts, first_evt). Self frame: count, timestamp range, slice
# totals, then one scan per pair whose rows are a, b, ref (scope total), then
# per slice (cnt, first_ts, first_evt).

_CORR_COLS = ["a", "b", "ref", "f0_cnt", "f0_first", "f0_evt"]


def _corr_responses(
    total: int, window_totals: tuple[int, int], pair_rows: list[list[tuple]]
) -> list[FakeQueryResult]:
    out = [
        FakeQueryResult(result_rows=[(total,)], column_names=["count()"]),
        FakeQueryResult(result_rows=[window_totals], column_names=["bl_total", "w0_total"]),
    ]
    out += [FakeQueryResult(result_rows=rows, column_names=_CORR_COLS) for rows in pair_rows]
    return out


_USER_HOST = ["attr:user", "attr:computer_name"]


def test_correlation_parameter_validation():
    svc = _svc([])
    with pytest.raises(ValueError, match="two fields"):
        svc.find_value_correlations("c1", ["s1"], fields=["attr:user"], windows=_seq_windows())
    with pytest.raises(ValueError, match="rule_confidence"):
        svc.find_value_correlations("c1", ["s1"], fields=_USER_HOST, rule_confidence=1.5)
    with pytest.raises(ValueError, match="min_ratio"):
        svc.find_value_correlations("c1", ["s1"], fields=_USER_HOST, min_ratio=1.0)
    assert svc.ch.client._calls == []


def test_correlation_no_data():
    svc = _svc([FakeQueryResult(result_rows=[(0,)], column_names=["count()"])])
    result = svc.find_value_correlations("c1", ["s1"], fields=_USER_HOST, windows=_seq_windows())
    assert result.status == "no_data"
    assert result.detector == "value_correlation"


def test_correlation_baseline_reports_a_broken_rule():
    """m.okonkwo ⇒ WKS-004 holds over 6000 baseline logons and breaks in the
    suspect window; b.moreau splits two homes and never forms a rule."""
    t_jump = datetime(2024, 1, 17, 6, 0, tzinfo=UTC)
    t_file = datetime(2024, 1, 17, 3, 0, tzinfo=UTC)
    rows = [
        ("m.okonkwo", "WKS-004", 6000, 5, datetime(2024, 1, 16, tzinfo=UTC), "e-home"),
        ("m.okonkwo", "JUMP-01", 0, 8, t_jump, "e-jump"),
        ("m.okonkwo", "FILE-01", 0, 4, t_file, "e-file"),
        ("b.moreau", "WKS-002", 3000, 200, datetime(2024, 1, 16, tzinfo=UTC), "e-b2"),
        ("b.moreau", "WKS-003", 2900, 190, datetime(2024, 1, 16, tzinfo=UTC), "e-b3"),
    ]
    svc = _svc(_corr_responses(20_000, (12_000, 8_000), [rows]))
    result = svc.find_value_correlations("c1", ["s1"], fields=_USER_HOST, windows=_seq_windows())
    assert result.status == "ok"
    assert result.method == "rule-g-test"
    assert result.baseline_size == 12_000
    assert len(result.results) == 1
    r = result.results[0]
    assert r.fields == ["attr:user", "attr:computer_name"]
    assert r.values == ["m.okonkwo", "WKS-004"]
    assert r.value == "m.okonkwo ⇒ WKS-004"
    assert r.confidence == 1.0
    assert r.support == 6000
    assert r.count == 17
    assert r.violations == 12
    assert r.baseline_violations == 0
    assert r.baseline_count == 6000
    assert abs(r.violation_rate - 12 / 17) < 1e-6
    assert r.top_violator == "JUMP-01"
    assert r.top_violator_count == 8
    assert r.q_value <= 0.05
    assert r.score == r.g_statistic > 0
    # The earliest violating occurrence is the representative event.
    assert r.event_id == "e-file"
    assert r.first_seen is not None and r.first_seen.startswith("2024-01-17T03:00")
    assert r.details["window_label"] == "incident"
    assert r.details["allowlist_field"] == "attr:user,attr:computer_name"
    assert r.details["allowlist_value"] == "m.okonkwo\x1fWKS-004"
    # count, window totals, one pair scan.
    assert len(svc.ch.client._calls) == 3
    params = svc.ch.client._all_parameters[2]
    assert params["fk0"] == "user" and params["fk1"] == "computer_name"
    assert params["cap"] == 5000


def test_correlation_mines_both_directions():
    """The reverse rule WKS-004 ⇒ m.okonkwo also holds and also breaks when a
    second account appears on that host in the window."""
    t = datetime(2024, 1, 17, tzinfo=UTC)
    rows = [
        ("m.okonkwo", "WKS-004", 6000, 300, t, "e1"),
        ("c.nakamura", "WKS-004", 0, 40, t, "e2"),
        ("c.nakamura", "WKS-009", 5000, 250, t, "e3"),
    ]
    svc = _svc(_corr_responses(20_000, (12_000, 8_000), [rows]))
    result = svc.find_value_correlations("c1", ["s1"], fields=_USER_HOST, windows=_seq_windows())
    assert result.status == "ok"
    rules = {(tuple(r.fields), r.value) for r in result.results}
    assert (("attr:computer_name", "attr:user"), "WKS-004 ⇒ m.okonkwo") in rules
    # c.nakamura ⇒ WKS-009 is broken too (40 of 290 window events elsewhere).
    assert (("attr:user", "attr:computer_name"), "c.nakamura ⇒ WKS-009") in rules
    # m.okonkwo ⇒ WKS-004 is intact: no violations in the window.
    assert (("attr:user", "attr:computer_name"), "m.okonkwo ⇒ WKS-004") not in rules


def test_correlation_floors_support_confidence_and_effect():
    t = datetime(2024, 1, 17, tzinfo=UTC)
    rows = [
        # Support 10 < 20: no rule however clean.
        ("thin", "H1", 10, 2, t, "e1"),
        ("thin", "H2", 0, 5, t, "e2"),
        # Confidence 0.9 < 0.95: no rule.
        ("split", "H1", 900, 50, t, "e3"),
        ("split", "H2", 100, 50, t, "e4"),
        # A real rule whose violation rate merely creeps from 4% to 6%: under
        # the 2x effect floor even where the G-test would pass.
        ("creep", "H1", 9600, 940, t, "e5"),
        ("creep", "H2", 400, 60, t, "e6"),
        # A real rule with no window violations: nothing to report.
        ("steady", "H1", 5000, 400, t, "e7"),
    ]
    svc = _svc(_corr_responses(50_000, (30_000, 20_000), [rows]))
    result = svc.find_value_correlations("c1", ["s1"], fields=_USER_HOST, windows=_seq_windows())
    assert result.status == "ok"
    assert [r.value for r in result.results if r.fields[0] == "attr:user"] == []


def test_correlation_pair_cap_and_auto_fields():
    """Six auto-picked fields make fifteen pairs; the cap scans the first
    three and says so."""
    t = datetime(2024, 1, 17, tzinfo=UTC)
    inventory = [(f"attr:f{i}", 10, 1000) for i in range(6)] + [("attr:pid", 990, 1000)]
    pair_rows = [[("x", "y", 100, 5, t, "e")] for _ in range(3)]
    svc = _svc(_corr_responses(1000, (700, 300), pair_rows))
    result = svc.find_value_correlations(
        "c1",
        ["s1"],
        windows=_seq_windows(),
        inventory=inventory,
        inventory_total=1000,
        max_pairs=3,
    )
    assert result.status == "ok"
    assert len(svc.ch.client._calls) == 5
    assert any("15 field pairs" in w and "first 3" in w for w in result.warnings)
    # An explicit two-field list is exactly one pair, no cap warning.
    svc = _svc(_corr_responses(1000, (700, 300), [[("x", "y", 100, 5, t, "e")]]))
    result = svc.find_value_correlations(
        "c1", ["s1"], fields=_USER_HOST, windows=_seq_windows(), max_pairs=3
    )
    assert not any("pair cap" in w for w in result.warnings)


def test_correlation_sql_shape():
    t = datetime(2024, 1, 17, tzinfo=UTC)
    client = RecordingClient(
        _corr_responses(20_000, (12_000, 8_000), [[("u", "h", 6000, 5, t, "e")]])
    )
    svc = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    svc.ch = FakeClickHouseStore(client)
    svc.find_value_correlations("c1", ["s1"], fields=_USER_HOST, windows=_seq_windows())
    sql = client.full_queries[2]
    assert "attributes[{fk0:String}]" in sql and "attributes[{fk1:String}]" in sql
    assert "GROUP BY a, b" in sql
    assert "LIMIT {cap:UInt32}" in sql
    assert "countIf(" in sql and "argMinIf(event_id" in sql
    params = client._all_parameters[2]
    assert params["b0"] == "2024-01-01 00:00:00.000"
    assert params["w0s"] == "2024-01-16 00:00:00.000"


def test_correlation_self_frame_tests_each_slice_against_the_rest():
    """Without a baseline: rules over the whole scope, one leave-one-out
    G-test per (rule, slice)."""
    k = 4
    span = (datetime(2024, 1, 1, tzinfo=UTC), datetime(2024, 1, 5, tzinfo=UTC))
    t = datetime(2024, 1, 4, 3, 0, tzinfo=UTC)
    cols = ["a", "b", "ref"] + [f"f{i}_{c}" for i in range(k) for c in ("cnt", "first", "evt")]
    # m.okonkwo: 6000 home logons spread over four slices; 12 stray logons,
    # all in slice 3 → the rule breaks in slice 3 against slices 0–2.
    rows = [
        (
            "m.okonkwo",
            "WKS-004",
            6000,
            1500,
            None,
            None,
            1500,
            None,
            None,
            1500,
            None,
            None,
            1500,
            None,
            None,
        ),
        ("m.okonkwo", "JUMP-01", 12, 0, None, None, 0, None, None, 0, None, None, 12, t, "e-j"),
    ]
    svc = _svc(
        [
            FakeQueryResult(result_rows=[(6012,)], column_names=["count()"]),
            FakeQueryResult(result_rows=[span], column_names=["min_ts", "max_ts"]),
            FakeQueryResult(result_rows=[(i, 1503) for i in range(k)], column_names=["slice", "n"]),
            FakeQueryResult(result_rows=rows, column_names=cols),
        ]
    )
    result = svc.find_value_correlations("c1", ["s1"], fields=_USER_HOST, self_slices=k)
    assert result.status == "ok", result.warnings
    assert result.method == "self-rule-g-test"
    assert result.windows is None and result.slices is not None
    assert result.slices["k"] == k
    assert [r.value for r in result.results] == ["m.okonkwo ⇒ WKS-004"]
    r = result.results[0]
    assert r.count == 1512
    assert r.violations == 12
    # The complement: the other three slices, with no violations at all.
    assert r.baseline_count == 4500
    assert r.baseline_violations == 0
    assert r.details["slice_index"] == 3
    assert r.details["rest_slices"] == k - 1
    assert r.event_id == "e-j"
    assert "baseline_size" not in r.details
    assert len(svc.ch.client._calls) == 4


def test_correlation_allowlist_suppresses_the_rule():
    t = datetime(2024, 1, 17, tzinfo=UTC)
    rows = [
        ("m.okonkwo", "WKS-004", 6000, 5, t, "e1"),
        ("m.okonkwo", "JUMP-01", 0, 8, t, "e2"),
    ]
    svc = _svc(_corr_responses(20_000, (12_000, 8_000), [rows]))
    result = svc.find_value_correlations(
        "c1",
        ["s1"],
        fields=_USER_HOST,
        windows=_seq_windows(),
        allowlist={("attr:user,attr:computer_name", "m.okonkwo\x1fWKS-004")},
    )
    assert result.status == "ok"
    assert result.results == []
