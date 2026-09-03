"""Live-ClickHouse test for the value-combo totals contract.

``total_findings`` is the exact number of rare combinations across the whole
scope — after allowlist and normal-event suppression, before the display
cap — and the page is the true top-``limit``. A fake client cannot prove
that: the count comes from a window function over the post-``HAVING``
groups, and the page from the ``ORDER BY``, both of which only ClickHouse
evaluates. Same fixture pattern as ``test_novelty_batched_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vestigo.db.anomaly_stats import (
    AnalysisWindows,
    StatisticalAnomalyService,
    TimeWindow,
)
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-combototal-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-combototal"

BASELINE_START = datetime(2026, 1, 1, tzinfo=UTC)
SUSPECT_START = datetime(2026, 1, 10, tzinfo=UTC)
SUSPECT_END = datetime(2026, 1, 11, tzinfo=UTC)

# 120 singleton (user, host) pairs — every one a finding at rarity_floor=1 —
# spread over the baseline days, plus a common pair repeated 10 times that
# must never be flagged. In the suspect window: 30 pairs absent from the
# baseline, each hit once, and one baseline-present pair (not a finding).
N_RARE = 120
N_COMMON = 10
N_SUSPECT_NEW = 30


def _event(i: int, ts: str, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="c" * 64,
        parser_name="test-combototal",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:combototal",
        attributes=attrs,
    )


def _fixture_events() -> list[Event]:
    events: list[Event] = []
    i = 0

    def add(ts: str, attrs: dict[str, str]) -> None:
        nonlocal i
        events.append(_event(i, ts, attrs))
        i += 1

    for n in range(N_RARE):
        add(
            f"2026-01-0{2 + n % 5}T{n % 24:02d}:{n % 60:02d}:00+00:00",
            {"user": f"u{n}", "host": f"h{n}"},
        )
    for n in range(N_COMMON):
        add(f"2026-01-03T12:{n:02d}:00+00:00", {"user": "svc", "host": "web-1"})
    for n in range(N_SUSPECT_NEW):
        add(f"2026-01-10T{n % 24:02d}:{n:02d}:00+00:00", {"user": f"new{n}", "host": f"nh{n}"})
    add("2026-01-10T23:59:00+00:00", {"user": "svc", "host": "web-1"})
    return events


@pytest.fixture(scope="module")
def svc():
    store = ClickHouseStore()
    store.init_schema()
    store.insert_events(_fixture_events())
    service = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    service.ch = store
    yield service
    store.delete_source_events(CASE_ID, SOURCE_ID)


FIELDS = ["attr:user", "attr:host"]


def _run(svc, **kw):
    return svc.find_value_combos(CASE_ID, [SOURCE_ID], fields=FIELDS, rarity_floor=1, **kw)


def test_total_is_the_corpus_count_and_the_page_is_the_limit(svc):
    result = _run(svc, limit=50)
    assert result.status == "ok"
    assert len(result.results) == 50
    # Every singleton pair in the scope: the 120 baseline ones plus the 30
    # suspect-window ones (self-baseline mode reads the whole scope).
    assert result.total_findings == N_RARE + N_SUSPECT_NEW
    assert result.total_findings_exact is True
    assert all(f.count == 1 for f in result.results)


def test_raising_the_limit_reaches_every_finding(svc):
    result = _run(svc, limit=500)
    assert len(result.results) == N_RARE + N_SUSPECT_NEW
    assert result.total_findings == N_RARE + N_SUSPECT_NEW
    assert not any(f.values == ["svc", "web-1"] for f in result.results)


def test_allowlist_is_counted_out_of_the_total(svc):
    allow = {("attr:user,attr:host", f"u{n}\x1fh{n}") for n in range(3)}
    result = _run(svc, limit=500, allowlist=allow)
    assert result.total_findings == N_RARE + N_SUSPECT_NEW - 3
    assert result.total_findings_exact is True
    assert not any(f.values[0] in {"u0", "u1", "u2"} for f in result.results)


def test_normal_marked_representatives_are_counted_out_of_the_total(svc):
    first = _run(svc, limit=500)
    drop = {f.event_id for f in first.results[:2] if f.event_id}
    assert len(drop) == 2
    result = _run(svc, limit=500, exclude_event_ids=drop)
    assert result.total_findings == N_RARE + N_SUSPECT_NEW - 2
    assert result.total_findings_exact is True
    assert not any(f.event_id in drop for f in result.results)


def test_temporal_total_counts_new_combinations_in_the_suspect_window(svc):
    windows = AnalysisWindows(
        baseline=TimeWindow("baseline", BASELINE_START, SUSPECT_START),
        suspects=(TimeWindow("suspect", SUSPECT_START, SUSPECT_END),),
    )
    result = _run(svc, limit=10, windows=windows)
    assert result.method == "temporal"
    assert len(result.results) == 10
    assert result.total_findings == N_SUSPECT_NEW
    assert result.total_findings_exact is True
    # The page is the rarest by window share, and every one is a new pair.
    assert all(f.values[0].startswith("new") for f in result.results)
    assert result.results == sorted(result.results, key=lambda f: f.score, reverse=True)
