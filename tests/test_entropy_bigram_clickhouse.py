"""Live-ClickHouse test for the D11 bigram entropy method.

The mock tests in ``test_anomaly_stats.py`` assert SQL *text*. What only
execution can prove is the claim D11 exists for: a name built from ordinary
characters in an unusual order is invisible to Shannon character entropy and
visible to a learned character-pair table. The fixture makes that as sharp as
it goes — the "generated" names are baseline hostnames **reversed**, so their
character multiset, and therefore their Shannon entropy, is *identical* to a
value the baseline contains. Only the order differs, which is the entire point.

It also covers the constructs that fail only at execution time: ``ngrams``,
``CAST((keys, values), 'Map(String, Float64)')` inside a ``WITH``,
``arrayAvg(arrayMap(...))`` over that alias, and the
``sum(...) OVER (PARTITION BY ...)`` head totals.

Requires the dev compose stack (skipped when ClickHouse is unreachable), same
pattern as ``test_charset_group_field_clickhouse.py``.
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

CASE_ID = f"tc-entbigram-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-entbigram"

BASELINE_START = datetime(2026, 3, 1, tzinfo=UTC)
SUSPECT_START = datetime(2026, 3, 10, tzinfo=UTC)
SUSPECT_END = datetime(2026, 3, 11, tzinfo=UTC)
SUSPECT_TS = "2026-03-10T06:00:00+00:00"

# Ordinary hostnames: English words, so their character pairs recur heavily.
WORDS = [
    "mailserver",
    "webproxy",
    "fileshare",
    "printhost",
    "database",
    "backupnode",
    "loginportal",
    "reportserver",
    "storagearray",
    "meetingroom",
    "accounting",
    "marketing",
    "engineering",
    "operations",
    "helpdesk",
    "warehouse",
    "frontdesk",
    "training",
    "logistics",
    "reception",
    "salesfloor",
    "designlab",
    "monitoring",
    "security",
    "planning",
    "recruiting",
    "shipping",
    "purchasing",
    "inventory",
    "scheduling",
]

# The generated names: three baseline words reversed. Same characters, same
# Shannon entropy, different order.
GENERATED = ["".join(reversed(w)) for w in ("mailserver", "warehouse", "scheduling")]


def _event(i: int, ts: str, host: str) -> Event:
    return Event(
        case_id=CASE_ID,
        source_id=SOURCE_ID,
        source_file=Path("dns.log"),
        byte_offset=i * 100,
        content_hash=f"{i:064d}",
        file_hash="e" * 64,
        parser_name="test-entbigram",
        parser_version="1.0.0",
        raw_line=f"raw {i}",
        message=f"lookup {host}",
        timestamp=ts,
        timestamp_desc="Test Time",
        artifact="test:entbigram",
        attributes={"host": host},
    )


def _fixture_events() -> list[Event]:
    events: list[Event] = []
    i = 0

    def add(ts: str, host: str) -> None:
        nonlocal i
        events.append(_event(i, ts, host))
        i += 1

    # Baseline window: ordinary hostnames only, comfortably over the 20-value floor.
    for n, word in enumerate(WORDS):
        add(f"2026-03-02T10:{n // 60:02d}:{n % 60:02d}+00:00", f"{word}.corp")
    # Suspect window: the same ordinary traffic, plus the generated names.
    for n, word in enumerate(WORDS[:10]):
        add(SUSPECT_TS, f"{word}.corp")
    for name in GENERATED:
        add(SUSPECT_TS, f"{name}.com")
    # One multi-byte hostname, so the byte-pair path is exercised on non-Latin input.
    add(SUSPECT_TS, "研究室サーバ.corp")
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


def _flagged(result) -> set[str]:
    return {f.value for f in result.results}


def _bigram(svc, **kwargs):
    return svc.find_entropy_outliers(
        CASE_ID,
        [SOURCE_ID],
        fields=["attr:host"],
        entropy_method="bigram",
        **kwargs,
    )


def test_bigram_flags_reordered_names_that_shannon_entropy_misses(svc):
    """The whole reason D11 exists, asserted in both directions at once."""
    shannon = svc.find_entropy_outliers(
        CASE_ID, [SOURCE_ID], fields=["attr:host"], windows=_windows()
    )
    bigram = _bigram(svc, windows=_windows())

    assert bigram.method == "temporal-bigram"
    flagged = _flagged(bigram)
    shannon_flagged = _flagged(shannon)
    for name in GENERATED:
        host = f"{name}.com"
        assert host in flagged, f"bigram missed {host}"
        assert host not in shannon_flagged, (
            f"shannon unexpectedly caught {host} — the fixture no longer demonstrates the gap"
        )


def test_bigram_does_not_flag_ordinary_baseline_hostnames(svc):
    result = _bigram(svc, windows=_windows())
    ordinary = {f"{w}.corp" for w in WORDS}
    assert not (_flagged(result) & ordinary)


def test_bigram_finding_carries_its_explanation(svc):
    result = _bigram(svc, windows=_windows())
    target = f"{GENERATED[0]}.com"
    f = next(r for r in result.results if r.value == target)
    assert f.mode == "bigram"
    assert 0.0 <= f.mean_prob < f.prob_thresh
    assert 0.0 < f.score <= 1.0
    # The Shannon fields stay empty: this finding is not a measurement in bits.
    assert f.entropy is None and f.direction is None and f.lower is None
    assert len(f.rare_pairs) == 5
    assert all(set(p) == {"pair", "prob"} for p in f.rare_pairs)
    # Sorted rarest-first, and every one of them is genuinely rare.
    probs = [p["prob"] for p in f.rare_pairs]
    assert probs == sorted(probs)
    assert probs[0] <= f.prob_thresh
    assert f.details["window_label"] == "suspect"
    assert f.details["table_pairs"] > 0
    assert f.details["casefold"] == "ascii-lower"
    assert f.details["allowlist_value"] == target


def test_bigram_self_baseline_mode_runs(svc):
    """No windows: learn and score over the same population, like Shannon's
    self-baseline mode. A handful of generated names is a negligible share of
    the pair mass, so they still fall below the threshold."""
    result = _bigram(svc)
    assert result.method == "bigram"
    assert f"{GENERATED[0]}.com" in _flagged(result)


def test_a_stricter_threshold_narrows_the_findings(svc):
    """`prob_thresh` is the knob it claims to be."""
    loose = _bigram(svc, windows=_windows(), prob_thresh=0.5)
    strict = _bigram(svc, windows=_windows(), prob_thresh=1e-9)
    assert len(strict.results) < len(loose.results)


def test_non_latin_values_do_not_break_the_scan(svc):
    """Byte-pair bigrams over multi-byte values must run, and every displayed
    pair must survive the round trip as a string."""
    result = _bigram(svc, windows=_windows())
    assert result.status == "ok"
    for f in result.results:
        for p in f.rare_pairs or []:
            assert isinstance(p["pair"], str)


def test_allowlisting_a_value_suppresses_it(svc):
    """A bigram finding carries the same (field, value) allowlist key a Shannon
    finding does, so an analyst's judgement about a *value* survives a method
    switch."""
    target = f"{GENERATED[0]}.com"
    result = _bigram(svc, windows=_windows(), allowlist={("attr:host", target)})
    assert target not in _flagged(result)
