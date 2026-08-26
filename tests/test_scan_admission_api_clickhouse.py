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
