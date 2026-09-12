"""The foreground lane is independent of the heavy gate, and says so when full (#300)."""

from __future__ import annotations

import pytest

from tests.conftest import as_admin
from vestigo.db import _scan, queries


def _hold(gate, n: int) -> int:
    taken = 0
    while n and gate.acquire(blocking=False):
        taken += 1
        n -= 1
    return taken


@pytest.fixture
def timeline(client, admin_bootstrap) -> tuple[str, str]:
    """A case and an (empty) timeline the admin can read.

    Empty is enough: the histogram still runs its ClickHouse query through
    the gate, and admission — not the answer — is what these tests are about.
    """
    as_admin(client, admin_bootstrap)
    case = client.post("/api/cases/", json={"name": "gate-case"}).json()["case"]
    tl = client.post(f"/api/cases/{case['id']}/timelines", json={"name": "tl"}).json()["timeline"]
    return case["id"], tl["id"]


def test_histogram_does_not_wait_for_the_heavy_gate(client, timeline):
    case_id, timeline_id = timeline
    taken = _hold(_scan.HEAVY_SCAN_GATE, _scan._GATE_CONCURRENCY)
    assert taken == _scan._GATE_CONCURRENCY, "heavy gate must be fully held for this test"
    try:
        res = client.get(f"/api/cases/{case_id}/timelines/{timeline_id}/histogram")
        assert res.status_code == 200, res.text
        assert "buckets" in res.json()
    finally:
        for _ in range(taken):
            _scan.HEAVY_SCAN_GATE.release()


def test_histogram_answers_busy_when_the_foreground_lane_is_full(client, timeline, monkeypatch):
    case_id, timeline_id = timeline
    monkeypatch.setattr(queries, "FOREGROUND_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.02)
    taken = _hold(_scan.FOREGROUND_SCAN_GATE, _scan._FOREGROUND_CONCURRENCY)
    assert taken == _scan._FOREGROUND_CONCURRENCY
    try:
        res = client.get(f"/api/cases/{case_id}/timelines/{timeline_id}/histogram")
        assert res.status_code == 503, res.text
        assert res.headers["retry-after"] == "5"
        assert res.json()["queued_ahead"] == 0
    finally:
        for _ in range(taken):
            _scan.FOREGROUND_SCAN_GATE.release()


@pytest.fixture
def uncached_timeline(client, admin_bootstrap, monkeypatch) -> tuple[str, str]:
    """A timeline over one ingested source whose field-stats cache row is a miss.

    The upload's background ingest fills the cache; bumping the version the
    read path expects turns that row into a miss, so the fields endpoint has
    a whole-source scan to run under the heavy gate.
    """
    from vestigo.db import field_stats

    as_admin(client, admin_bootstrap)
    case = client.post("/api/cases/", json={"name": "fill-case"}).json()["case"]
    content = b'{"message":"login ok","timestamp":"2026-01-01T00:00:00+00:00"}\n'
    res = client.post(
        f"/api/cases/{case['id']}/sources",
        files={"file": ("events.jsonl", content, "application/x-ndjson")},
    )
    assert res.status_code == 200, res.text
    source_id = res.json()["source_id"]
    tl = client.post(
        f"/api/cases/{case['id']}/timelines", json={"name": "tl", "source_ids": [source_id]}
    ).json()["timeline"]
    # An integer: the column is one, and the fill writes the value it reads.
    monkeypatch.setattr(
        field_stats, "EFFECTIVE_STATS_VERSION", field_stats.EFFECTIVE_STATS_VERSION + 1000
    )
    return case["id"], tl["id"]


def test_field_stats_fill_answers_busy_when_the_heavy_gate_is_full(
    client, uncached_timeline, monkeypatch
):
    """A request filling the cache says "busy" past its bound instead of parking.

    The fill is a heavy scan, but the Explorer's column picker is waiting on
    it: held for the length of every admitted sweep the request would hold
    the analyst's screen with no bound, no queue depth and no release on
    disconnect. Under the bounded heavy wait it answers the same 503 a full
    chart lane does, which the UI retries.
    """
    from vestigo.api import scan_exec

    case_id, timeline_id = uncached_timeline
    monkeypatch.setattr(scan_exec, "FIELD_STATS_WAIT_SECONDS", 0.1)
    monkeypatch.setattr(_scan, "_ACQUIRE_POLL_SECONDS", 0.02)
    taken = _hold(_scan.HEAVY_SCAN_GATE, _scan._GATE_CONCURRENCY)
    assert taken == _scan._GATE_CONCURRENCY
    try:
        res = client.get(f"/api/cases/{case_id}/timelines/{timeline_id}/fields")
        assert res.status_code == 503, res.text
        assert res.headers["retry-after"] == "5"
        # Not asserted to be 0: the upload's column-recommendation job reads
        # the same cache, sees the same miss and is parked on the held gate —
        # unbounded, as a job should be — and may be counted ahead.
        assert isinstance(res.json()["queued_ahead"], int)
    finally:
        for _ in range(taken):
            _scan.HEAVY_SCAN_GATE.release()


def test_field_stats_fill_completes_through_the_scan_runner(client, uncached_timeline):
    """With a slot free the same request fills the cache and answers normally."""
    case_id, timeline_id = uncached_timeline

    res = client.get(f"/api/cases/{case_id}/timelines/{timeline_id}/fields")

    assert res.status_code == 200, res.text
    assert "message" in res.json()["top_level"]
