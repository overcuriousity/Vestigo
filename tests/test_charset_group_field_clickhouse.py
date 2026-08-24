"""Live-ClickHouse test for the grouped charset detector (D14 `group_field`).

The mock-based tests in ``test_anomaly_stats.py`` assert the SQL *text*. The
constructs this feature introduced only fail at execution time, and a wrong one
fails as a query error or — worse — as silently zero findings:

* ``{sets:Array(Array(String))}[greatest(gidx, 1)]`` — indexing an
  array-of-array query parameter inside an ``if`` whose branches ClickHouse
  evaluates unconditionally, including at index 0 and on an empty array.
* ``LIMIT {plim} BY grp LIMIT {tlim}`` — per-group budget and global ceiling in
  one query, which is order-sensitive syntax.
* the ``skip`` array that carries wide-alphabet groups out of the scan, and the
  ``has_fb`` routing that sends the rest to the fallback.

Requires the dev compose stack (skipped when ClickHouse is unreachable), same
pattern as ``test_novelty_batched_clickhouse.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest

from vestigo.db.anomaly_stats import (
    _MAX_CHARSET_SIZE,
    AnalysisWindows,
    StatisticalAnomalyService,
    TimeWindow,
)
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.models.event import Event

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-charsetgrp-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-charsetgrp"

BASELINE_START = datetime(2026, 3, 1, tzinfo=UTC)
SUSPECT_START = datetime(2026, 3, 10, tzinfo=UTC)
SUSPECT_END = datetime(2026, 3, 11, tzinfo=UTC)
SUSPECT_TS = "2026-03-10T06:00:00+00:00"

# A Cyrillic "а" (U+0430) among Latin letters — the homoglyph case the detector
# exists for, invisible to anything that compares values rather than characters.
HOMOGLYPH = "а"


def _event(i: int, ts: str, attrs: dict[str, str]) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("evidence.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="c" * 64,
        parser_name="test-charsetgrp",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"event {i}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:charsetgrp",
        attributes=attrs,
    )


def _fixture_events() -> list[Event]:
    """Four hosts covering every branch of the reference-selection table.

    On ``attr:user``:

    * ``web-1`` — plenty of baseline values, plain ASCII. Scored against its own
      alphabet; a homoglyph in its suspect window must be flagged.
    * ``ru-1`` — legitimately Cyrillic in the *baseline*, so the same character
      is normal for it. This is what per-group scoping is for: under one merged
      alphabet neither host would flag.
    * ``new-1`` — absent from the baseline window entirely, so it is scored
      against the fallback. Its suspect value carries a NUL, which appears
      nowhere outside the suspect windows: the fallback is a *merged* reference
      by design, so a character some other host uses legitimately would not be
      novel against it, and testing with one would be testing the wrong thing.

    ``prose-1`` lives on its own field ``attr:cmd``. Its wide alphabet has to
    stay out of ``attr:user``'s fallback, which merges everything outside the
    suspect windows and would otherwise blow through the same ceiling — making
    the whole field unevaluable instead of just that group.
    """
    events: list[Event] = []
    i = 0

    def add(ts: str, attrs: dict[str, str]) -> None:
        nonlocal i
        events.append(_event(i, ts, attrs))
        i += 1

    # Baseline window. 30 distinct values each, comfortably over the 20-value
    # floor, and each character well over the rarity floor of 3.
    for n in range(30):
        add(
            f"2026-03-02T10:{n // 60:02d}:{n % 60:02d}+00:00", {"user": f"user{n}", "host": "web-1"}
        )
        add(
            f"2026-03-03T10:{n // 60:02d}:{n % 60:02d}+00:00",
            {"user": f"пolьzovаtel{n}", "host": "ru-1"},
        )
        # attr:cmd baseline for web-1 — a normal alphabet, so `attr:cmd` has a
        # learnable fallback and prose-1 being dropped is a real choice rather
        # than the only option left.
        add(
            f"2026-03-02T11:{n // 60:02d}:{n % 60:02d}+00:00",
            {"cmd": f"ls -l /d{n}", "host": "web-1"},
        )

    # prose-1: one value per distinct CJK character, so its learned alphabet
    # blows past the ceiling while every value stays a legitimate one.
    for n in range(_MAX_CHARSET_SIZE + 50):
        add("2026-03-04T00:00:00+00:00", {"cmd": chr(0x4E00 + n) + f"{n}", "host": "prose-1"})

    # Outside every window: what the temporal fallback is learned from. Plain
    # ASCII, over the floor, and free of both the homoglyph and the NUL so a
    # fallback-scored value carrying one is genuinely novel against it.
    for n in range(30):
        add(
            f"2026-03-06T10:{n // 60:02d}:{n % 60:02d}+00:00",
            {"user": f"outside{n}", "host": "web-1"},
        )
        add(
            f"2026-03-06T11:{n // 60:02d}:{n % 60:02d}+00:00",
            {"cmd": f"cat /e{n}", "host": "web-1"},
        )

    # Suspect window.
    add(SUSPECT_TS, {"user": f"al{HOMOGLYPH}ice", "host": "web-1"})  # flag: novel for web-1
    add(SUSPECT_TS, {"user": f"пol{HOMOGLYPH}", "host": "ru-1"})  # normal for ru-1
    add(SUSPECT_TS, {"user": "r\x00ot", "host": "new-1"})  # flag: via fallback
    add(SUSPECT_TS, {"cmd": "一丁丂", "host": "prose-1"})  # dropped, not scored
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


def _windows() -> AnalysisWindows:
    return AnalysisWindows(
        baseline=TimeWindow("baseline", BASELINE_START, SUSPECT_START),
        suspects=(TimeWindow("suspect", SUSPECT_START, SUSPECT_END),),
    )


def _by_group(result) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in result.results:
        out.setdefault(str(f.details.get("group_value")), []).append(f)
    return out


def test_grouped_temporal_scores_each_host_against_its_own_alphabet(svc):
    """The whole point of grouping: a character that is normal for one host and
    novel for another resolves per host instead of merging into a reference
    alphabet that flags neither."""
    result = svc.find_charset_novelty(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:user"],
        windows=_windows(),
        group_field="attr:host",
    )
    assert result.status == "ok"
    groups = _by_group(result)

    # web-1: Cyrillic never appeared in its baseline → flagged against its own.
    assert "web-1" in groups
    web = groups["web-1"][0]
    assert HOMOGLYPH in web.novel_chars
    assert web.details["group_basis"] == "baseline-window"
    assert web.details["group_baseline_distinct_values"] >= 30

    # ru-1: the same character *is* its baseline alphabet → not flagged.
    assert "ru-1" not in groups


def test_group_absent_from_baseline_is_scored_by_the_fallback(svc):
    """A host that first appears inside the suspect window is the interesting
    one, so it is scored against a reference learned outside those windows
    rather than skipped."""
    result = svc.find_charset_novelty(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:user"],
        windows=_windows(),
        group_field="attr:host",
    )
    groups = _by_group(result)
    assert "new-1" in groups
    new = groups["new-1"][0]
    assert "\x00" in new.novel_chars
    assert new.details["group_basis"] == "outside-suspect-windows"
    # It contributed no evidence of its own — that is *why* a fallback scored it.
    assert new.details["group_baseline_distinct_values"] == 0
    assert any("no baseline-window values" in w and "new-1" in w for w in result.warnings)


def test_wide_alphabet_group_is_dropped_not_fallback_scored(svc):
    """The wide-alphabet guard is about the detector's premise, not its evidence
    budget: no reference makes 'novel character' meaningful for free text in a
    large script, so the group produces no findings at all.

    Note the fallback for this field is also refused — it is learned from a
    superset of prose-1's own values, so it inherits the same wide alphabet.
    That is why the run reports the field's fallback failure by its real reason
    rather than the value-count one.
    """
    result = svc.find_charset_novelty(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:cmd"],
        windows=_windows(),
        group_field="attr:host",
    )
    assert "prose-1" not in _by_group(result)
    assert any(
        "not evaluated" in w and "prose-1" in w and "novel character carries no signal" in w
        for w in result.warnings
    )
    # The fallback failed on width, not on value count — saying "too few
    # distinct values" here would send an analyst to widen a baseline that was
    # never the problem.
    assert not any("too few distinct values" in w for w in result.warnings)


def test_grouped_self_baseline_runs_and_scopes_per_host(svc):
    """Self-baseline (rare-chars) mode takes the same grouped SQL path — no
    windows means a different `detect_clause`, and the array-parameter routing
    has to hold up under both."""
    result = svc.find_charset_novelty(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:user"],
        group_field="attr:host",
    )
    assert result.status in {"ok", "insufficient_data"}
    for f in result.results:
        assert f.details["group_field"] == "attr:host"
        assert f.details["group_basis"] in {"scope", "scope-merged"}
        # prose-1 fails the premise in either mode.
        assert f.details["group_value"] != "prose-1"


def test_grouped_scan_survives_every_group_lacking_a_reference(svc):
    """`grps`/`sets` are empty when no group clears the guards, so the
    `indexOf` → `sets[greatest(gidx, 1)]` routing has to hold up on empty
    array parameters rather than raising."""
    # A baseline window with no events at all: every group is 'absent', so
    # `evaluated` is empty and every row routes through the fallback.
    windows = AnalysisWindows(
        baseline=TimeWindow(
            "baseline", datetime(2026, 2, 1, tzinfo=UTC), datetime(2026, 2, 2, tzinfo=UTC)
        ),
        suspects=(TimeWindow("suspect", SUSPECT_START, SUSPECT_END),),
    )
    result = svc.find_charset_novelty(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:user"],
        windows=windows,
        group_field="attr:host",
    )
    # The query ran (no exception) and whatever it found is fallback-scored.
    assert result.status in {"ok", "insufficient_data"}
    for f in result.results:
        assert f.details["group_basis"] == "outside-suspect-windows"


def test_per_group_finding_budget_is_preserved(svc):
    """`LIMIT plim BY grp` means one noisy group cannot consume another's
    budget — the syntax that enforces it only parses at execution time."""
    result = svc.find_charset_novelty(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:user"],
        windows=_windows(),
        group_field="attr:host",
        per_field_limit=1,
        limit=50,
    )
    counts = {g: len(fs) for g, fs in _by_group(result).items()}
    assert counts, "expected at least one finding"
    assert all(n <= 1 for n in counts.values()), counts
