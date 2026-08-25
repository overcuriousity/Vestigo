"""The value-inventory export endpoint (#295).

`db/queries.py` owns the aggregation (covered against live ClickHouse in
``test_field_inventory_clickhouse.py``); what is pinned here is the file the
analyst actually receives — which columns, in which order, with which
separator — and the completeness contract it shares with the events export:
a stream that yields fewer values than the pre-flight counted must break the
download rather than hand over a silently short inventory.
"""

from __future__ import annotations

import threading

import pytest
import pytest_asyncio
from fastapi import HTTPException

from tests.conftest import _fake_user
from tests.test_events_router import _collect, _seed_export_timeline
from vestigo.api import deps
from vestigo.api.routers import events
from vestigo.db.postgres import Case, PostgresStore
from vestigo.db.queries import QueryRequestTooLargeError


@pytest_asyncio.fixture()
async def patched_store(pg_database, monkeypatch):
    """A private PostgreSQL database, wired into deps.get_store() — same
    pattern as tests/test_events_router.py, whose export tests these extend."""
    store = PostgresStore(url=pg_database)
    monkeypatch.setattr(deps, "_store", store)
    yield store
    await store.engine.dispose()


_ROWS = [
    {
        "value": "10.0.0.4",
        "count": 3,
        "first_seen": "2026-03-01T00:00:00+00:00",
        "last_seen": "2026-03-05T12:00:00+00:00",
    },
    {
        "value": "10.0.0.9;with-separator",
        "count": 1,
        "first_seen": None,
        "last_seen": None,
    },
]


class _FakeInventoryService:
    """Decouples the pre-flight distinct count from what the stream yields, so
    a shortfall (the integrity failure the hard-fail guards) can be forced."""

    def __init__(self, count_val: int, rows: list[dict] | None = None) -> None:
        self._count = count_val
        self._rows = _ROWS if rows is None else rows
        self.last_order_by: str | None = None

    def count_field_inventory(self, query, field_token):
        return self._count

    def iter_field_inventory(self, query, field_token, *, order_by="count_desc", **kwargs):
        self.last_order_by = order_by
        yield from self._rows


def _request(**kwargs) -> events.FieldInventoryRequest:
    kwargs.setdefault("field", "attr:src_ip")
    return events.FieldInventoryRequest(**kwargs)


async def _export(store, monkeypatch, service, body):
    await _seed_export_timeline(store)
    monkeypatch.setattr(events, "_get_query_service", lambda: service)
    resp = await events.export_field_inventory(
        "c1", "t1", body, case=Case(id="c1"), user=_fake_user()
    )
    chunks: list[str] = []
    await _collect(resp, chunks)
    # Starlette runs the response's background task after the body is sent;
    # calling the endpoint directly means doing that by hand.
    if resp.background is not None:
        await resp.background()
    return resp, "".join(chunks)


@pytest.mark.asyncio
async def test_default_columns(patched_store, monkeypatch):
    """value, first seen, last seen — the three the analyst asked for, plus the
    count the default most-frequent-first ordering sorts by."""
    _, body = await _export(patched_store, monkeypatch, _FakeInventoryService(2), _request())

    lines = body.splitlines()
    assert lines[0] == "value,first_seen,last_seen,count"
    assert lines[1] == "10.0.0.4,2026-03-01T00:00:00+00:00,2026-03-05T12:00:00+00:00,3"


@pytest.mark.asyncio
async def test_count_stays_opt_in_when_the_file_is_not_sorted_by_it(patched_store, monkeypatch):
    _, body = await _export(
        patched_store, monkeypatch, _FakeInventoryService(2), _request(order_by="value_asc")
    )

    assert body.splitlines()[0] == "value,first_seen,last_seen"


@pytest.mark.asyncio
async def test_columns_are_selectable_and_keep_the_requested_order(patched_store, monkeypatch):
    _, body = await _export(
        patched_store,
        monkeypatch,
        _FakeInventoryService(2),
        _request(columns=["count", "value"], order_by="value_asc"),
    )

    assert body.splitlines()[0] == "count,value"
    assert body.splitlines()[1] == "3,10.0.0.4"


@pytest.mark.asyncio
async def test_a_value_with_no_known_time_exports_empty_times(patched_store, monkeypatch):
    """Never a fabricated year-2299 timestamp — the cell is simply empty."""
    _, body = await _export(
        patched_store, monkeypatch, _FakeInventoryService(2), _request(order_by="value_asc")
    )

    assert body.splitlines()[2] == "10.0.0.9;with-separator,,"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("separator", "expected", "ext"),
    [
        ("semicolon", "10.0.0.4;2026-03-01T00:00:00+00:00", "csv"),
        ("tab", "10.0.0.4\t2026-03-01T00:00:00+00:00", "tsv"),
        ("pipe", "10.0.0.4|2026-03-01T00:00:00+00:00", "csv"),
    ],
)
async def test_separator_is_configurable(patched_store, monkeypatch, separator, expected, ext):
    resp, body = await _export(
        patched_store,
        monkeypatch,
        _FakeInventoryService(2),
        _request(columns=["value", "first_seen"], separator=separator, order_by="value_asc"),
    )

    assert body.splitlines()[1] == expected
    assert f".{ext}" in resp.headers["content-disposition"]


@pytest.mark.asyncio
async def test_a_value_containing_the_separator_is_quoted(patched_store, monkeypatch):
    """The inventory of a field whose values contain the chosen separator must
    still parse — quoting is the writer's job, not the analyst's."""
    _, body = await _export(
        patched_store,
        monkeypatch,
        _FakeInventoryService(2),
        _request(columns=["value"], separator="semicolon", order_by="value_asc"),
    )

    assert body.splitlines()[2] == '"10.0.0.9;with-separator"'


@pytest.mark.asyncio
async def test_ordering_column_is_forced_into_the_file(patched_store, monkeypatch):
    """A file sorted by a column it does not contain cannot be read as sorted."""
    service = _FakeInventoryService(2)
    _, body = await _export(
        patched_store,
        monkeypatch,
        service,
        _request(columns=["value"], order_by="count_desc"),
    )

    assert body.splitlines()[0] == "value,count"
    assert service.last_order_by == "count_desc"


@pytest.mark.asyncio
async def test_unknown_ordering_is_rejected(patched_store, monkeypatch):
    await _seed_export_timeline(patched_store)
    monkeypatch.setattr(events, "_get_query_service", lambda: _FakeInventoryService(2))

    with pytest.raises(HTTPException) as exc:
        await events.export_field_inventory(
            "c1",
            "t1",
            _request(order_by="count_sideways"),
            case=Case(id="c1"),
            user=_fake_user(),
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("columns", [[], ["nonsense"], ["value", "value"]])
async def test_invalid_column_sets_are_rejected(patched_store, monkeypatch, columns):
    await _seed_export_timeline(patched_store)
    monkeypatch.setattr(events, "_get_query_service", lambda: _FakeInventoryService(2))

    with pytest.raises(HTTPException) as exc:
        await events.export_field_inventory(
            "c1", "t1", _request(columns=columns), case=Case(id="c1"), user=_fake_user()
        )
    assert exc.value.status_code == 400


@pytest.mark.asyncio
async def test_hard_fails_on_shortfall(patched_store, monkeypatch):
    """Fewer values streamed than the pre-flight counted: mark the trailer
    incomplete and break the download rather than hand over a short inventory."""
    await _seed_export_timeline(patched_store)
    monkeypatch.setattr(events, "_get_query_service", lambda: _FakeInventoryService(5))
    resp = await events.export_field_inventory(
        "c1", "t1", _request(), case=Case(id="c1"), user=_fake_user()
    )

    chunks: list[str] = []
    with pytest.raises(events.ExportIncompleteError):
        await _collect(resp, chunks)
    joined = "".join(chunks)
    assert "expected=5" in joined
    assert "complete=false" in joined


@pytest.mark.asyncio
async def test_complete_stream_declares_completeness(patched_store, monkeypatch):
    _, body = await _export(patched_store, monkeypatch, _FakeInventoryService(2), _request())

    assert "# vestigo_export complete=true rows=2 expected=2" in body


@pytest.mark.asyncio
async def test_too_large_filter_surfaces_before_streaming(patched_store, monkeypatch):
    """Once the response headers flush, no exception handler runs — the
    pre-flight count is the last point a query failure can pick a status code."""
    await _seed_export_timeline(patched_store)

    class _TooLarge(_FakeInventoryService):
        def count_field_inventory(self, query, field_token):
            raise QueryRequestTooLargeError("the filter is too large")

        def iter_field_inventory(self, *a, **k):
            raise AssertionError("streaming must not start")

    monkeypatch.setattr(events, "_get_query_service", lambda: _TooLarge(2))
    with pytest.raises(QueryRequestTooLargeError):
        await events.export_field_inventory(
            "c1", "t1", _request(), case=Case(id="c1"), user=_fake_user()
        )


@pytest.mark.asyncio
async def test_export_is_audited(patched_store, monkeypatch):
    await _export(patched_store, monkeypatch, _FakeInventoryService(2), _request())

    actions = [a.action for a in await patched_store.query_audit(case_id="c1")]
    assert "events.export.field_inventory" in actions
    assert "events.export.field_inventory.result" in actions


# ── The single streamed-export slot ─────────────────────────────────────────


@pytest.fixture()
def export_gate(monkeypatch):
    """A one-slot stand-in bound where the endpoint reads it.

    `db/_scan.py` is imported by value into the router, so patching the
    module's own attribute would not reach the running code.
    """
    gate = threading.BoundedSemaphore(1)
    monkeypatch.setattr(events, "EXPORT_SCAN_GATE", gate)
    return gate


def _free(gate) -> bool:
    if not gate.acquire(blocking=False):
        return False
    gate.release()
    return True


@pytest.mark.asyncio
async def test_a_completed_export_hands_the_slot_back(patched_store, monkeypatch, export_gate):
    await _export(patched_store, monkeypatch, _FakeInventoryService(2), _request())

    assert _free(export_gate)


@pytest.mark.asyncio
async def test_a_busy_slot_is_refused_before_a_byte_is_sent(
    patched_store, monkeypatch, export_gate
):
    """A queued export must fail as a status code, not as a truncated file.

    The slot is held for the whole client-paced drain, so an analyst who
    backgrounds a large download holds it for as long as they like. Waiting on
    it inside the generator would mean waiting after the headers are already
    gone; taken up front, a wait that runs out is a clean 503 — and bounded,
    because the drain occupies an anyio worker thread the rest of the app
    shares.
    """
    from vestigo.core.config import set_runtime_overrides

    await _seed_export_timeline(patched_store)
    monkeypatch.setattr(events, "_get_query_service", lambda: _FakeInventoryService(2))
    assert export_gate.acquire(blocking=False), "somebody else is exporting"

    try:
        set_runtime_overrides({"export_scan_queue_wait_seconds": 0})
        with pytest.raises(HTTPException) as excinfo:
            await events.export_field_inventory(
                "c1", "t1", _request(), case=Case(id="c1"), user=_fake_user()
            )
    finally:
        set_runtime_overrides({})
        export_gate.release()

    assert excinfo.value.status_code == 503
    assert excinfo.value.headers["Retry-After"]
    assert _free(export_gate), "a refusal must not consume the slot it could not take"


@pytest.mark.asyncio
async def test_an_abandoned_export_hands_the_slot_back(patched_store, monkeypatch, export_gate):
    """Starlette closes the generator when the analyst cancels the download.

    That unwinds the `finally`, and it has to, or the one slot every other
    export queues behind stays taken for the life of the process.
    """
    await _seed_export_timeline(patched_store)
    monkeypatch.setattr(events, "_get_query_service", lambda: _FakeInventoryService(2))
    resp = await events.export_field_inventory(
        "c1", "t1", _request(), case=Case(id="c1"), user=_fake_user()
    )
    assert not _free(export_gate), "held for the drain"

    await resp.body_iterator.__anext__()
    await resp.body_iterator.aclose()

    assert _free(export_gate)


@pytest.mark.asyncio
async def test_a_refused_export_records_no_audit_row(patched_store, monkeypatch, export_gate):
    """A 503 produced no file, so the trail must not say an export ran."""
    from vestigo.core.config import set_runtime_overrides

    await _seed_export_timeline(patched_store)
    monkeypatch.setattr(events, "_get_query_service", lambda: _FakeInventoryService(2))
    assert export_gate.acquire(blocking=False)

    try:
        set_runtime_overrides({"export_scan_queue_wait_seconds": 0})
        with pytest.raises(HTTPException):
            await events.export_field_inventory(
                "c1", "t1", _request(), case=Case(id="c1"), user=_fake_user()
            )
    finally:
        set_runtime_overrides({})
        export_gate.release()

    rows = await patched_store.query_audit(case_id="c1")
    assert not [r for r in rows if r.action.startswith("events.export.field_inventory")]
