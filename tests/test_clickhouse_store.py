"""Unit tests for ClickHouseStore SQL construction — no live ClickHouse needed."""

from pathlib import Path
from types import SimpleNamespace

import pyarrow as pa
import pytest

from tracesignal.db._arrow_schema import EVENT_ARROW_SCHEMA
from tracesignal.db.clickhouse import (
    _EVENT_COLUMNS,
    _EVENTS_TABLE_DDL,
    ClickHouseStore,
    _events_to_record_batch,
    _partition_expr,
    _validate_partition_id,
)
from tracesignal.db.postgres import generate_id
from tracesignal.models.event import Event


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows
        self.column_names = []


class _RecordingClient:
    """Records every query/command with its parameters."""

    def __init__(self):
        self.queries: list[tuple[str, dict | None]] = []
        self.commands: list[str] = []
        self.arrow_inserts: list[tuple[str, pa.Table]] = []

    def query(self, query, parameters=None):
        self.queries.append((query, parameters))
        return _FakeResult([(42,)])

    def command(self, cmd):
        self.commands.append(cmd)

    def insert_arrow(self, table, arrow_table, **kwargs):
        self.arrow_inserts.append((table, arrow_table))
        return SimpleNamespace(written_rows=arrow_table.num_rows)


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


class TestEventsSchema:
    def test_timestamp_is_non_nullable_sort_key(self):
        # A Nullable sort-key column (allow_nullable_key) disables
        # ClickHouse's read-in-order optimization — every grid page would
        # full-sort the partition. Undated events use the storage sentinel.
        assert "timestamp DateTime64(3)" in _EVENTS_TABLE_DDL
        assert "Nullable" not in _EVENTS_TABLE_DDL
        assert "allow_nullable_key" not in _EVENTS_TABLE_DDL
        assert "ORDER BY (case_id, source_id, timestamp, event_id)" in _EVENTS_TABLE_DDL

    def test_init_schema_rejects_legacy_nullable_table(self, store):
        class _LegacyClient(_RecordingClient):
            def query(self, query, parameters=None):
                super().query(query, parameters)
                if "system.columns" in query:
                    return _FakeResult([("Nullable(DateTime64(3))",)])
                return _FakeResult([(42,)])

        store.client = _LegacyClient()
        with pytest.raises(RuntimeError, match="one-time timestamp-sentinel migration"):
            store.init_schema()
        # The guard must fire before the ready-flag caches success.
        assert not getattr(store, "_schema_ready", False)

    def test_init_schema_accepts_migrated_table(self, store):
        class _MigratedClient(_RecordingClient):
            def query(self, query, parameters=None):
                super().query(query, parameters)
                if "system.columns" in query:
                    return _FakeResult([("DateTime64(3)",)])
                return _FakeResult([(42,)])

        store.client = _MigratedClient()
        store.init_schema()
        assert store._schema_ready is True

    def test_init_schema_accepts_fresh_install(self, store):
        class _EmptyClient(_RecordingClient):
            def query(self, query, parameters=None):
                super().query(query, parameters)
                if "system.columns" in query:
                    return _FakeResult([])
                return _FakeResult([(42,)])

        store.client = _EmptyClient()
        store.init_schema()
        assert store._schema_ready is True


class _SearchBlobClient(_RecordingClient):
    """Fake with controllable search_blob column/index/mutation state."""

    def __init__(
        self,
        has_column: bool,
        mutations: list[tuple[int, str]] | None = None,
        has_index: bool | None = None,
    ):
        super().__init__()
        self.has_column = has_column
        # Defaults to has_column so existing column-only callers keep working.
        self.has_index = has_column if has_index is None else has_index
        self.mutations = mutations if mutations is not None else []

    def query(self, query, parameters=None):
        self.queries.append((query, parameters))
        if "system.columns" in query and "search_blob" in query:
            return _FakeResult([(1 if self.has_column else 0,)])
        if "system.data_skipping_indices" in query:
            return _FakeResult([(1 if self.has_index else 0,)])
        if "system.mutations" in query:
            return _FakeResult(self.mutations)
        return _FakeResult([(42,)])


class TestSearchBlob:
    def test_ddl_contains_blob_column_and_index(self):
        from tracesignal.db.clickhouse import _SEARCH_BLOB_COLUMN_DDL, _SEARCH_BLOB_INDEX_DDL

        assert "search_blob String MATERIALIZED lowerUTF8" in _SEARCH_BLOB_COLUMN_DDL
        assert "CODEC(ZSTD(3))" in _SEARCH_BLOB_COLUMN_DDL
        assert "ngrambf_v1(3, 65536, 4, 0)" in _SEARCH_BLOB_INDEX_DDL
        # The dead tokenbf message index is gone from fresh-install DDL.
        assert "message_idx" not in _EVENTS_TABLE_DDL
        assert "{search_blob_column}" in _EVENTS_TABLE_DDL
        assert "{search_blob_index}" in _EVENTS_TABLE_DDL
        # Blob covers every broad-search field, in add_broad_text_search order.
        for col in (
            "message",
            "display_name",
            "artifact",
            "artifact_long",
            "timestamp_desc",
            "source_file",
            "arrayStringConcat(tags",
            "mapValues(attributes)",
        ):
            assert col in _SEARCH_BLOB_COLUMN_DDL

    def test_ensure_upgrades_missing_column(self, store):
        store.client = _SearchBlobClient(has_column=False)
        store._ensure_search_blob()
        cmds = store.client.commands
        assert any("ADD COLUMN IF NOT EXISTS search_blob" in c for c in cmds)
        assert any("ADD INDEX IF NOT EXISTS search_blob_idx" in c for c in cmds)
        assert any("DROP INDEX IF EXISTS message_idx" in c for c in cmds)
        # Materialization is asynchronous — startup never blocks on backfill.
        assert any("MATERIALIZE COLUMN search_blob SETTINGS mutations_sync = 0" in c for c in cmds)
        assert any(
            "MATERIALIZE INDEX search_blob_idx SETTINGS mutations_sync = 0" in c for c in cmds
        )

    def test_ensure_noop_when_column_present(self, store):
        store.client = _SearchBlobClient(has_column=True)
        store._ensure_search_blob()
        assert store.client.commands == []

    def test_ensure_resumes_when_column_present_but_index_missing(self, store):
        # Regression: a crash between ADD COLUMN and ADD INDEX must not
        # permanently strand the table without the index — a column-only
        # guard would short-circuit here forever.
        store.client = _SearchBlobClient(has_column=True, has_index=False)
        store._ensure_search_blob()
        cmds = store.client.commands
        assert any("ADD COLUMN IF NOT EXISTS search_blob" in c for c in cmds)
        assert any("ADD INDEX IF NOT EXISTS search_blob_idx" in c for c in cmds)
        assert any("DROP INDEX IF EXISTS message_idx" in c for c in cmds)
        assert any("MATERIALIZE COLUMN search_blob SETTINGS mutations_sync = 0" in c for c in cmds)
        assert any(
            "MATERIALIZE INDEX search_blob_idx SETTINGS mutations_sync = 0" in c for c in cmds
        )

    def test_ready_true_when_no_pending_mutations(self, store):
        store.client = _SearchBlobClient(has_column=True, mutations=[])
        assert store.search_blob_ready() is True

    def test_ready_false_while_mutation_pending_with_recheck_ttl(self, store):
        store.client = _SearchBlobClient(has_column=True, mutations=[(0, "")])
        assert store.search_blob_ready() is False
        queries_after_first = len(store.client.queries)
        # Within the recheck TTL the negative result is served from cache.
        assert store.search_blob_ready() is False
        assert len(store.client.queries) == queries_after_first

    def test_ready_true_is_cached_forever(self, store):
        store.client = _SearchBlobClient(has_column=True, mutations=[])
        assert store.search_blob_ready() is True
        queries_after_first = len(store.client.queries)
        assert store.search_blob_ready() is True
        assert len(store.client.queries) == queries_after_first

    def test_ready_false_when_column_missing(self, store):
        store.client = _SearchBlobClient(has_column=False)
        assert store.search_blob_ready() is False

    def test_failed_mutation_counts_as_ready(self, store):
        # Fast path stays correct on unmaterialized parts (blob computed on
        # read); a failed mutation only means old parts stay unindexed.
        store.client = _SearchBlobClient(has_column=True, mutations=[(1, "disk full")])
        assert store.search_blob_ready() is True


def _make_event(i: int, **overrides) -> Event:
    kwargs: dict = {
        "case_id": "case-1",
        "source_id": "src-1",
        "source_file": Path("evidence.log"),
        "byte_offset": i * 100,
        "content_hash": f"{i:064d}",
        "file_hash": "f" * 64,
        "parser_name": "test",
        "parser_version": "1.0.0",
        "raw_line": f"raw {i}",
        "message": f"event {i}",
        "timestamp": "2026-01-01T10:00:00+00:00",
        "timestamp_desc": "Test Time",
        "artifact": "test:artifact",
        "tags": ["t1"],
        "attributes": {"key": "value", "empty": "", "none": None},
    }
    kwargs.update(overrides)
    return Event(**kwargs)


class TestArrowInsert:
    def test_schema_columns_match_event_columns(self):
        assert EVENT_ARROW_SCHEMA.names == _EVENT_COLUMNS

    def test_schema_types_mirror_ddl(self):
        # Every DDL column must have the Arrow dtype insert_arrow needs:
        # numerics exact, DateTime64(3) as ms-UTC timestamps, UUID/FixedString
        # as strings (server-side cast), Array/Map as list/map.
        expected = {
            "byte_offset": pa.uint64(),
            "line_number": pa.uint64(),
            "ingest_time": pa.timestamp("ms", tz="UTC"),
            "timestamp": pa.timestamp("ms", tz="UTC"),
            "tags": pa.list_(pa.string()),
            "attributes": pa.map_(pa.string(), pa.string()),
        }
        for name in EVENT_ARROW_SCHEMA.names:
            assert EVENT_ARROW_SCHEMA.field(name).type == expected.get(name, pa.string())

    def test_record_batch_matches_clickhouse_rows(self):
        events = [_make_event(1), _make_event(2, timestamp=None, tags=[], attributes={})]
        batch = _events_to_record_batch(events)
        assert batch.schema == EVENT_ARROW_SCHEMA
        assert batch.num_rows == 2
        rows = batch.to_pylist()
        for row, event in zip(rows, events, strict=True):
            expected = event.to_clickhouse_row()
            assert row["event_id"] == str(event.event_id)
            assert row["byte_offset"] == expected["byte_offset"]
            assert row["content_hash"] == expected["content_hash"]
            assert row["message"] == expected["message"]
            assert row["timestamp"] == expected["timestamp"]
            assert row["tags"] == expected["tags"]
            # Arrow maps round-trip as key/value tuple lists.
            assert dict(row["attributes"]) == expected["attributes"]

    def test_sentinel_and_empty_attribute_encoding(self):
        # to_clickhouse_row rules must survive the Arrow encoding: no nulls in
        # timestamp (sentinel year 2299) and empty/None attributes dropped.
        batch = _events_to_record_batch([_make_event(1, timestamp=None)])
        row = batch.to_pylist()[0]
        assert row["timestamp"] is not None
        assert row["timestamp"].year == 2299
        assert dict(row["attributes"]) == {"key": "value"}

    def test_insert_events_goes_through_insert_arrow(self, store):
        events = [_make_event(1), _make_event(2)]
        assert store.insert_events(events) == 2
        (table, arrow_table), *rest = store.client.arrow_inserts
        assert not rest
        assert table == "tracesignal.events"
        assert arrow_table.num_rows == 2
        assert arrow_table.schema == EVENT_ARROW_SCHEMA

    def test_insert_events_empty_is_noop(self, store):
        assert store.insert_events([]) == 0
        assert store.client.arrow_inserts == []

    def test_insert_events_arrow_passthrough(self, store):
        batch = _events_to_record_batch([_make_event(7)])
        assert store.insert_events_arrow(batch) == 1
        assert store.client.arrow_inserts[0][0] == "tracesignal.events"


class TestDeleteSourceEventsErrors:
    def test_generic_failure_raises(self, store):
        store.client = _FailingClient("Code: 210. Connection refused")
        with pytest.raises(RuntimeError, match="Connection refused"):
            store.delete_source_events("case-1", "src-1")

    def test_missing_table_is_benign_noop(self, store):
        store.client = _FailingClient("Code: 60. DB::Exception: UNKNOWN_TABLE")
        store.delete_source_events("case-1", "src-1")  # must not raise


class TestParseUrl:
    """URL forms accepted by ClickHouseStore._parse_url."""

    def test_plain_http(self):
        assert ClickHouseStore._parse_url("http://localhost:8123") == (
            "localhost",
            8123,
            False,
            None,
            None,
        )

    def test_http_default_port(self):
        assert ClickHouseStore._parse_url("http://ch.internal") == (
            "ch.internal",
            8123,
            False,
            None,
            None,
        )

    def test_https_secure_and_default_port(self):
        assert ClickHouseStore._parse_url("https://ch.internal") == (
            "ch.internal",
            8443,
            True,
            None,
            None,
        )

    def test_https_explicit_port(self):
        host, port, secure, _, _ = ClickHouseStore._parse_url("https://ch.internal:9443")
        assert (host, port, secure) == ("ch.internal", 9443, True)

    def test_credentials_in_url(self):
        assert ClickHouseStore._parse_url("http://alice:s3cret@ch:8123") == (
            "ch",
            8123,
            False,
            "alice",
            "s3cret",
        )

    def test_bare_host_port(self):
        assert ClickHouseStore._parse_url("ch.internal:8124") == (
            "ch.internal",
            8124,
            False,
            None,
            None,
        )

    def test_trailing_slash_and_path(self):
        host, port, secure, _, _ = ClickHouseStore._parse_url("http://ch:8123/")
        assert (host, port, secure) == ("ch", 8123, False)
