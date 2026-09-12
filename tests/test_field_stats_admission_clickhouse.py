"""Field-stats cache misses are computed under the heavy scan gate.

``compute_source_field_stats`` issues whole-source scans at the heavy per-query
cap — ``uniqExact`` per attribute key, ``LIMIT n BY`` top values. The heavy cap
is sized as the budget ÷ (gate size + 2) on the premise that no more than the
gate's worth of such queries run at once. ``ensure_source_field_stats`` filled
every miss with ``asyncio.gather`` and no slot, so a case with many uncached
sources (a fresh upgrade that bumps ``STATS_VERSION``, an import) stacked one
full cap per source on top of whatever sweeps were already admitted — the
stacking the gate exists to prevent (session-52).
"""

from __future__ import annotations

import threading
import time
import uuid
from pathlib import Path

import pytest
import pytest_asyncio

from vestigo.db import _scan
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.db.field_stats import ensure_source_field_stats
from vestigo.db.postgres import PostgresStore
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-fsgate-{uuid.uuid4().hex[:8]}"
SRC_A, SRC_B = "fsgate-src-a", "fsgate-src-b"


class _InFlight:
    """Forward to the real client, recording the most queries in flight at once."""

    def __init__(self, inner) -> None:
        self._inner = inner
        self._lock = threading.Lock()
        self._now = 0
        self.peak = 0

    def query(self, *args, **kwargs):
        with self._lock:
            self._now += 1
            self.peak = max(self.peak, self._now)
        try:
            # Long enough that two computes running unadmitted must overlap.
            time.sleep(0.2)
            return self._inner.query(*args, **kwargs)
        finally:
            with self._lock:
                self._now -= 1

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _event(source_id: str, i: int) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=source_id,
        source_file=Path(f"{source_id}.csv"),
        byte_offset=i * 100,
        content_hash=f"ch-{source_id}-{i}",
        file_hash=f"fh-{source_id}",
        parser_name="test",
        parser_version="1",
        raw_line=f"line {i}",
        message=f"event {i}",
        timestamp=f"2026-01-01T10:{i:02d}:00Z",
        timestamp_desc="Test Time",
        artifact="test:artifact",
        attributes={"src_ip": f"10.0.0.{i}"},
    )


@pytest.fixture(scope="module")
def ch_store():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events([_event(SRC_A, 1), _event(SRC_A, 2), _event(SRC_B, 3)])
    yield store
    for sid in (SRC_A, SRC_B):
        store.delete_source_events(CASE_ID, sid)


@pytest_asyncio.fixture()
async def pg_store(pg_database):
    s = PostgresStore(url=pg_database)
    yield s
    await s.engine.dispose()


async def test_misses_queue_for_a_heavy_slot(ch_store, pg_store, monkeypatch):
    tracked = ClickHouseStore()
    tracker = _InFlight(tracked.client)
    tracked.client = tracker
    # `gated_heavy_scan` looks the gate up on `_scan` at call time, so this is
    # the one binding a smaller gate has to replace.
    monkeypatch.setattr(_scan, "HEAVY_SCAN_GATE", threading.BoundedSemaphore(1))

    stats = await ensure_source_field_stats(pg_store, tracked, CASE_ID, [SRC_A, SRC_B])

    assert (stats[SRC_A][0], stats[SRC_B][0]) == (2, 1)
    assert tracker.peak == 1
