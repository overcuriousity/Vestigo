"""Unit tests for ClickHouseStore SQL construction — no live ClickHouse needed."""

import pytest

from tracesignal.db.clickhouse import (
    ClickHouseStore,
    _partition_expr,
    _validate_partition_id,
)
from tracesignal.db.postgres import generate_id


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows
        self.column_names = []


class _RecordingClient:
    """Records every query/command with its parameters."""

    def __init__(self):
        self.queries: list[tuple[str, dict | None]] = []
        self.commands: list[str] = []

    def query(self, query, parameters=None):
        self.queries.append((query, parameters))
        return _FakeResult([(42,)])

    def command(self, cmd):
        self.commands.append(cmd)


@pytest.fixture()
def store():
    s = ClickHouseStore.__new__(ClickHouseStore)
    s.database = "tracesignal"
    s.client = _RecordingClient()
    return s


class TestValidatePartitionId:
    def test_accepts_generated_ids(self):
        for base in ("some source.csv", "Case Name #1", "täst"):
            value = generate_id(base)
            assert _validate_partition_id(value, "id") == value

    @pytest.mark.parametrize(
        "value",
        [
            "x'); DROP TABLE events; --",
            "a'b",
            "a b",
            "tuple('x','y')",
            "",
            "a,b",
        ],
    )
    def test_rejects_unsafe_values(self, value):
        with pytest.raises(ValueError, match="unsafe"):
            _validate_partition_id(value, "id")

    def test_partition_expr_shape(self):
        assert _partition_expr("case-1", "src_2") == "tuple('case-1', 'src_2')"

    def test_partition_expr_rejects_injection(self):
        with pytest.raises(ValueError):
            _partition_expr("case-1", "x') FROM evil; --")


class TestCountEventsBinds:
    def test_no_filters(self, store):
        assert store.count_events() == 42
        query, parameters = store.client.queries[0]
        assert "WHERE" not in query
        assert parameters == {}

    def test_case_and_source_are_bound(self, store):
        store.count_events(case_id="c'1", source_id="s1")
        query, parameters = store.client.queries[0]
        assert "{case_id:String}" in query
        assert "{source_id:String}" in query
        # The raw value never appears in the SQL text.
        assert "c'1" not in query
        assert parameters == {"case_id": "c'1", "source_id": "s1"}

    def test_source_ids_in_list_is_bound(self, store):
        store.count_events(case_id="c1", source_ids=["a", "b'; DROP", "c"])
        query, parameters = store.client.queries[0]
        assert "source_id IN ({s0:String}, {s1:String}, {s2:String})" in query
        assert "DROP" not in query
        assert parameters == {"case_id": "c1", "s0": "a", "s1": "b'; DROP", "s2": "c"}

    def test_empty_source_ids_short_circuits(self, store):
        assert store.count_events(source_ids=[]) == 0
        assert store.client.queries == []


class _FailingClient(_RecordingClient):
    def __init__(self, message: str):
        super().__init__()
        self.message = message

    def command(self, cmd):
        super().command(cmd)
        raise RuntimeError(self.message)


class TestDeleteSourceEventsErrors:
    def test_generic_failure_raises(self, store):
        store.client = _FailingClient("Code: 210. Connection refused")
        with pytest.raises(RuntimeError, match="Connection refused"):
            store.delete_source_events("case-1", "src-1")

    def test_missing_table_is_benign_noop(self, store):
        store.client = _FailingClient("Code: 60. DB::Exception: UNKNOWN_TABLE")
        store.delete_source_events("case-1", "src-1")  # must not raise

    def test_timeline_delete_attempts_all_and_aggregates(self, store):
        attempted: list[str] = []

        def flaky_command(cmd):
            source_id = cmd.split("'")[3]
            attempted.append(source_id)
            if source_id in ("bad-1", "bad-2"):
                raise RuntimeError("boom")

        store.client = type("C", (), {"command": staticmethod(flaky_command)})()
        with pytest.raises(RuntimeError, match="bad-1, bad-2"):
            store.delete_timeline_events("case-1", ["ok-1", "bad-1", "ok-2", "bad-2"])
        assert attempted == ["ok-1", "bad-1", "ok-2", "bad-2"]
