"""A disconnect kills the ClickHouse query the scan is running (#300)."""

from __future__ import annotations

import time

import pytest

from vestigo.api import scan_exec
from vestigo.db import _scan
from vestigo.db.clickhouse import ClickHouseStore


class _Request:
    def __init__(self, after: float):
        self.t0 = time.monotonic()
        self.after = after

    async def is_disconnected(self) -> bool:
        return time.monotonic() - self.t0 > self.after


@pytest.mark.asyncio
async def test_disconnect_kills_the_running_query(monkeypatch):
    store = ClickHouseStore()
    monkeypatch.setattr(scan_exec, "current_request", lambda: _Request(after=1.0))
    monkeypatch.setattr(scan_exec, "_POLL_SECONDS", 0.2)
    monkeypatch.setattr(scan_exec, "_client", lambda: store.client)
    tags: list[str] = []

    def work():
        ctx = _scan.scan_context()
        assert ctx is not None
        tags.append(_scan.scan_log_comment(ctx.token))
        # Same clause every real scan carries, so the tag rides log_comment.
        store.client.query(
            f"SELECT sleepEachRow(1) FROM numbers(60) {_scan.heavy_scan_settings()}, max_block_size = 1"
        )

    t0 = time.monotonic()
    with pytest.raises(scan_exec.ScanCancelledResponse):
        await scan_exec.run_scan(work)
    assert time.monotonic() - t0 < 15
    # Give the ASYNC kill a moment, then the process must be gone.
    rows = [(1,)]
    for _ in range(50):
        rows = store.client.query(
            "SELECT count() FROM system.processes WHERE Settings['log_comment'] = {t:String}",
            parameters={"t": tags[0]},
        ).result_rows
        if rows[0][0] == 0:
            break
        time.sleep(0.1)
    assert rows[0][0] == 0
