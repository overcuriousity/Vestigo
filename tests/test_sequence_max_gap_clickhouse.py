"""Live-ClickHouse test for the D14 ``max_gap_seconds`` sequence bound.

``_ngram_inner_sql`` grows two extra window levels when a gap bound is set: a
``dateDiff`` over ``lagInFrame(ets, 1)``, then a running ``sum`` of over-gap
boundaries that the n-gram assembly partitions by. Whether that produces the
right segments depends on runtime behaviour the mock tests cannot see:

* ``lagInFrame(ets, 1)`` must yield NULL on each partition's first row, which
  holds because ``timestamp`` is ``Nullable(DateTime64(3))``. If that ever
  changes, the lag returns the epoch instead, the first row reads as an
  over-gap boundary, and the comment in ``anomaly_stats.py`` becomes wrong.
  This test fails if it does.
* ``sum(if(gap_s > n, 1, 0)) OVER ord`` must stay a monotone per-partition
  counter for the assembly window to partition by.

Requires the dev compose stack (skipped when ClickHouse is unreachable), same
pattern as ``test_novelty_batched_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
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

CASE_ID = f"tc-seqgap-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-seqgap"

BASELINE_START = datetime(2026, 5, 1, tzinfo=UTC)
SUSPECT_START = datetime(2026, 5, 10, tzinfo=UTC)
SUSPECT_END = datetime(2026, 5, 11, tzinfo=UTC)

GAP = 300  # 5 minutes


def _event(i: int, ts: datetime, proc: str) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="a" * 64,
        parser_name="test-seqgap",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts.isoformat(),
        timestamp_desc="Test Time",
        artifact="test:seqgap",
        attributes={"proc": proc},
    )


def _fixture_events() -> list[Event]:
    """A baseline of tight A→B→C runs, and a suspect window whose only A→B→C
    spans a long quiet gap.

    Without a gap bound the suspect A→B→C is an n-gram like any other and is
    *not* novel (the baseline has it). With the bound it never assembles, so the
    suspect window's n-grams are the fragments around the gap instead — which is
    exactly the "quiet source manufactures a sequence from unrelated events"
    failure the bound exists to prevent.
    """
    events: list[Event] = []
    i = 0

    def add(ts: datetime, proc: str) -> None:
        nonlocal i
        events.append(_event(i, ts, proc))
        i += 1

    # Baseline: 20 tight A→B→C runs, 10s apart within a run.
    for run in range(20):
        base = BASELINE_START + timedelta(hours=run)
        for offset, proc in ((0, "A"), (10, "B"), (20, "C")):
            add(base + timedelta(seconds=offset), proc)

    # Suspect window: A, then a 1-hour silence, then B and C. Under no bound
    # this reads as the familiar A→B→C; under a 5-minute bound the A is
    # stranded in its own segment.
    add(SUSPECT_START + timedelta(minutes=1), "A")
    add(SUSPECT_START + timedelta(minutes=61), "B")
    add(SUSPECT_START + timedelta(minutes=61, seconds=10), "C")
    # …plus a tight D→E→F run, novel in both modes, so the suspect window has a
    # finding either way and an empty result never passes vacuously.
    tight = SUSPECT_START + timedelta(hours=5)
    for offset, proc in ((0, "D"), (10, "E"), (20, "F")):
        add(tight + timedelta(seconds=offset), proc)
    return events


@pytest.fixture(scope="module")
def svc():
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse not reachable — start the dev compose stack")
    store.insert_events(_fixture_events())
    service = StatisticalAnomalyService.__new__(StatisticalAnomalyService)
    service.ch = store
    yield service
    store.delete_source_events(CASE_ID, SOURCE_ID)


def _windows() -> AnalysisWindows:
    return AnalysisWindows(
        baseline=TimeWindow("baseline", BASELINE_START, SUSPECT_START),
        suspects=(TimeWindow("suspect", SUSPECT_START, SUSPECT_END),),
    )


def _grams(result) -> set[tuple[str, ...]]:
    return {tuple(f.values) for f in result.results}


def _novelty(svc, max_gap):
    return svc.find_sequence_novelty(
        CASE_ID,
        [SOURCE_ID],
        series_field="attr:proc",
        ngram=3,
        windows=_windows(),
        max_gap_seconds=max_gap,
    )


def test_gap_bound_breaks_a_sequence_across_a_quiet_gap(svc):
    """Unbounded, the suspect window's isolated events get stitched to the tight
    run hours later and reported as novel sequences that never happened. The
    bound removes exactly those and keeps the real one."""
    unbounded = _grams(_novelty(svc, None))
    bounded = _grams(_novelty(svc, GAP))

    # Both modes see the genuinely novel tight run.
    assert ("D", "E", "F") in unbounded
    assert ("D", "E", "F") in bounded

    # B→C→D and C→D→E only exist because the window's B, C and the D five hours
    # later are adjacent *in record order* with nothing between them. They are
    # the manufactured findings the bound is for.
    stitched = {("B", "C", "D"), ("C", "D", "E")}
    assert stitched <= unbounded, unbounded
    assert not (stitched & bounded), bounded
    assert bounded == {("D", "E", "F")}


def test_gap_bound_larger_than_the_data_is_equivalent_to_no_bound(svc):
    """A bound no gap can exceed must return exactly what the unbounded query
    does — the segment counter stays 0 for every row, so the assembly window
    partitions identically."""
    unbounded = _grams(_novelty(svc, None))
    huge = _grams(_novelty(svc, 365 * 24 * 3600))
    assert unbounded == huge


def test_first_row_of_a_partition_is_not_stranded_in_its_own_segment(svc):
    """`lagInFrame(ets, 1)` yields NULL on a partition's first row, so `if(NULL
    > n, 1, 0)` takes the else branch and segment numbering starts at 0. If
    `timestamp` ever stopped being Nullable the lag would return the epoch, the
    first row would read as an over-gap boundary and be cut off from the events
    that follow it — losing the first n-gram of every partition."""
    bounded = _grams(_novelty(svc, GAP))
    # D is the first event of its tight run; if it were stranded, D→E→F could
    # never assemble and the only novel finding would vanish.
    assert ("D", "E", "F") in bounded


def test_motif_mining_honours_the_same_bound(svc):
    """Both sequence detectors must agree on what a sequence is, so the bound
    reaches sequence_motif's two passes as well."""
    unbounded = svc.find_sequence_motifs(
        CASE_ID, [SOURCE_ID], series_field="attr:proc", ngram=3, min_support=2
    )
    bounded = svc.find_sequence_motifs(
        CASE_ID,
        [SOURCE_ID],
        series_field="attr:proc",
        ngram=3,
        min_support=2,
        max_gap_seconds=GAP,
    )
    # A→B→C recurs 20 times in the baseline, tightly, so it survives the bound.
    assert ("A", "B", "C") in _grams(unbounded)
    assert ("A", "B", "C") in _grams(bounded)
    # Cross-run grams (C→A→B, B→C→A) only exist because consecutive runs are an
    # hour apart with nothing between them; the bound removes exactly those.
    cross = {("C", "A", "B"), ("B", "C", "A")}
    assert not (cross & _grams(bounded)), _grams(bounded)
