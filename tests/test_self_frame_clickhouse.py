"""The self frame of the four formerly baseline-only detectors (ROADMAP D18).

A baseline improves a detector's precision; it never decides whether the
detector can run. Each fabricated signal here is one the issue #366 spec names,
placed on a timeline with **no** baseline definition, and the assertion is that
the detector's self frame finds it — and does not find the control beside it.
Real ClickHouse, since the claims are about the SQL.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest

from tests.conftest import insert_generated_events
from vestigo.db import anomaly_stats
from vestigo.db.anomaly_stats import StatisticalAnomalyService
from vestigo.db.clickhouse import ClickHouseStore

pytestmark = pytest.mark.clickhouse

_CASE = f"selfframe-{uuid.uuid4().hex[:8]}"
_MAIN = "src-main"
_STOPPED = "src-stopped"
_CYCLE = "src-cycle"

_T0 = datetime(2026, 2, 1, tzinfo=UTC)
_BASE = "toDateTime64('2026-02-01 00:00:00', 3)"
_DAY = 86_400
_SPAN = 3 * _DAY  # the main source covers three days


def _src_main(s: ClickHouseStore) -> None:
    """Every self-frame signal on one three-day source, keyed by `attr:svc`.

    Row ranges (by ``number``) and what they are:

    * 0..863      ``beacon``   — every 300 s ± 8 s across the whole span.
    * 1000..3999  ``noise``    — 3000 uniformly random moments (a Poisson
                                 process on the same span); the control.
    * 4000..4599  ``onoff``    — 300 s beacon that runs 200 beats a day, then
                                 pauses ~7 h; 3 days.
    * 5000..5019  ``burst``    — 20 events 5 s apart: 95 s of even spacing,
                                 under the 300 s span floor.
    * 6000..10319 ``hole``     — 60 s heartbeat ± 1 s across the whole span
                                 with a ten-beat hole at beat 1000.
    * 11000..12439 ``stops``   — 60 s heartbeat for one day, then nothing
                                 while the source keeps logging.
    * 20000..20499 ``user=eve`` burst — 500 events inside one hour on day 2;
                   the other users spread over the span (`attr:user`).
    * 21000..24999 ``late``    — a user who starts on day 3 only.
    * `attr:bytes` is ~uniform 0..999 everywhere except the ``eve`` hour,
      where it is 5000..5999 — a numeric drift confined to one slice.
    * 13000..15999 ``seq`` rows 2 s apart carry `attr:act`, cycling A→B→C
      with one stray X→Y→Z insertion at rows 14500..14502 (one rare
      ordering in this source); every other row has no `act`.
    """
    n = 25_000
    ts = f"""multiIf(
        number < 864, {_BASE} + number * 300 + (toInt64(cityHash64(number) % 17) - 8),
        number >= 1000 AND number < 4000, {_BASE} + toInt64(cityHash64(number * 3) % {_SPAN}),
        number >= 4000 AND number < 4600,
            {_BASE} + intDiv(number - 4000, 200) * {_DAY} + ((number - 4000) % 200) * 300,
        number >= 5000 AND number < 5020, {_BASE} + {_DAY} + 3600 + (number - 5000) * 5,
        number >= 6000 AND number < 10320,
            {_BASE} + ((number - 6000) + if(number - 6000 >= 1000, 10, 0)) * 60
            + (toInt64(cityHash64(number) % 3) - 1),
        number >= 11000 AND number < 12440, {_BASE} + (number - 11000) * 60,
        number >= 13000 AND number < 16000, {_BASE} + {_DAY} + 40000 + (number - 13000) * 2,
        number >= 20000 AND number < 20500, {_BASE} + {_DAY} + 7200 + (number - 20000) * 7,
        number >= 21000 AND number < 25000,
            {_BASE} + 2 * {_DAY} + toInt64(cityHash64(number * 5) % {_DAY}),
        {_BASE} + toInt64(cityHash64(number * 11) % {_SPAN})
    )"""
    svc = """multiIf(
        number < 864, 'beacon',
        number >= 1000 AND number < 4000, 'noise',
        number >= 4000 AND number < 4600, 'onoff',
        number >= 5000 AND number < 5020, 'burst',
        number >= 6000 AND number < 10320, 'hole',
        number >= 11000 AND number < 12440, 'stops',
        'other'
    )"""
    user = """multiIf(
        number >= 20000 AND number < 20500, 'eve',
        number >= 21000 AND number < 25000, 'late',
        cityHash64(number) % 2 = 0, 'alice', 'bob'
    )"""
    bytes_ = """if(number >= 20000 AND number < 20500,
        5000 + cityHash64(number * 13) % 1000, cityHash64(number * 13) % 1000)"""
    act = """multiIf(
        number = 14500, 'X', number = 14501, 'Y', number = 14502, 'Z',
        number >= 13000 AND number < 16000, arrayElement(['A', 'B', 'C'], toUInt32(number % 3) + 1),
        ''
    )"""
    insert_generated_events(
        s,
        case_id=_CASE,
        source_id=_MAIN,
        n=n,
        timestamp=ts,
        attributes=(
            f"map('svc', {svc}, 'user', {user}, 'bytes', toString({bytes_}), 'act', {act})"
        ),
    )


def _src_stopped(s: ClickHouseStore) -> None:
    """A 60 s heartbeat whose *source* ends with it: not a silence."""
    insert_generated_events(
        s,
        case_id=_CASE,
        source_id=_STOPPED,
        n=1440,
        timestamp=f"{_BASE} + number * 60",
        attributes="map('svc', 'hb2', 'user', 'carol', 'act', 'A')",
    )


def _src_cycle(s: ClickHouseStore) -> None:
    """X→Y→Z repeated 100 times: common here, so rare-in-main is not rare case-wide."""
    insert_generated_events(
        s,
        case_id=_CASE,
        source_id=_CYCLE,
        n=300,
        timestamp=f"{_BASE} + number * 10",
        attributes="map('svc', 'cycle', 'user', 'dave', 'act', "
        "arrayElement(['X', 'Y', 'Z'], toUInt32(number % 3) + 1))",
    )


@pytest.fixture(scope="module")
def store():
    s = ClickHouseStore()
    s.init_schema()
    _src_main(s)
    _src_stopped(s)
    _src_cycle(s)
    yield s
    for sid in (_MAIN, _STOPPED, _CYCLE):
        s.delete_source_events(_CASE, sid)


@pytest.fixture
def svc(store):
    return StatisticalAnomalyService(clickhouse=store)


def _by_value(result, direction: str | None = None) -> dict[str, list]:
    out: dict[str, list] = {}
    for f in result.results:
        if direction is not None and f.direction != direction:
            continue
        out.setdefault(f.value, []).append(f)
    return out


# ---------------------------------------------------------------------------
# Interval cadence — self-cadence
# ---------------------------------------------------------------------------


def test_self_cadence_finds_the_whole_timeline_beacon_and_not_the_poisson_control(svc):
    result = svc.find_interval_periodicity(_CASE, [_MAIN], fields=["attr:svc"], limit=100)
    assert result.status == "ok", result.warnings
    assert result.method == "self-cadence"
    regular = _by_value(result, "new_regularity")
    assert "beacon" in regular
    beacon = regular["beacon"][0]
    assert beacon.details["method"] == "self-cadence"
    assert beacon.details["paused_intervals"] == 0
    assert beacon.details["retained_intervals"] == 863
    assert 290 < beacon.details["median_interval"] < 310
    assert beacon.window_cv is not None and beacon.window_cv <= 0.3
    assert "baseline_count" not in beacon.details
    # Random arrivals are not regular, and 95 s of even spacing is not a beacon.
    assert "noise" not in regular
    assert "burst" not in regular


def test_self_cadence_excludes_pauses_from_the_onoff_beacon(svc):
    result = svc.find_interval_periodicity(_CASE, [_MAIN], fields=["attr:svc"], limit=100)
    regular = _by_value(result, "new_regularity")
    assert "onoff" in regular
    onoff = regular["onoff"][0]
    # Two overnight pauses over three days, each far longer than 10 medians.
    assert onoff.details["paused_intervals"] == 2
    assert onoff.details["retained_intervals"] == 597
    assert onoff.details["pause_ratio"] == 10.0


def test_self_cadence_finds_the_internal_hole_with_its_bounds(svc):
    result = svc.find_interval_periodicity(_CASE, [_MAIN], fields=["attr:svc"], limit=100)
    missed = _by_value(result, "missed")
    assert "hole" in missed
    hole = missed["hole"][0]
    d = hole.details
    assert d["trailing"] is False
    # Ten beats skipped: the gap is 11 minutes (± the one-second jitter).
    assert 640 <= d["longest_gap_seconds"] <= 680
    assert 9.5 <= d["expected_arrivals_missed"] <= 10.5
    gap_start = datetime.fromisoformat(d["gap_start"])
    assert abs((gap_start - (_T0 + timedelta(seconds=999 * 60))).total_seconds()) <= 2
    assert hole.event_id is not None
    # The robust CV clamp: IQR = 0 on a one-second jitter, and that jitter is
    # not itself a silence.
    assert d["robust_cv"] == 0.05
    assert len(missed["hole"]) == 1


def test_self_cadence_trailing_silence_needs_the_source_to_keep_logging(svc):
    result = svc.find_interval_periodicity(_CASE, [_MAIN, _STOPPED], fields=["attr:svc"], limit=100)
    missed = _by_value(result, "missed")
    assert "stops" in missed
    stops = missed["stops"][0]
    assert stops.details["trailing"] is True
    assert stops.details["gap_source_id"] == _MAIN
    assert stops.details["longest_gap_seconds"] > _DAY
    # hb2's source stops with it: coverage ending is not a silence.
    assert "hb2" not in missed


# ---------------------------------------------------------------------------
# Proportion shift — self-g-test
# ---------------------------------------------------------------------------


def test_self_g_test_flags_the_one_hour_burst_in_its_slice(svc):
    result = svc.find_proportion_shifts(_CASE, [_MAIN], fields=["attr:user"], limit=100)
    assert result.status == "ok", result.warnings
    assert result.method == "self-g-test"
    assert result.slices is not None and result.slices["k"] == 24
    up = _by_value(result, "up")
    assert "eve" in up
    eve = up["eve"][0]
    assert eve.details["method"] == "self-g-test"
    assert "baseline_count" not in eve.details and "baseline_total" not in eve.details
    assert eve.details["rest_count"] == 0  # absent from the complement, still tested
    assert eve.details["slice_count"] == 24
    assert eve.details["window_label"].startswith("slice ")
    start = datetime.fromisoformat(eve.details["window_start"])
    end = datetime.fromisoformat(eve.details["window_end"])
    assert start <= _T0 + timedelta(days=1, hours=2) < end
    assert eve.event_id is not None and eve.first_seen is not None


def test_self_g_test_active_span_rule_keeps_a_late_starter_quiet_before_its_life(svc):
    result = svc.find_proportion_shifts(_CASE, [_MAIN], fields=["attr:user"], limit=200)
    late_down = _by_value(result, "down").get("late", [])
    day3 = _T0 + timedelta(days=2)
    for f in late_down:
        # Any "down" for `late` must sit inside its own life (day 3), never before it.
        assert datetime.fromisoformat(f.details["window_end"]) > day3
    # And the steady users are not a wall of findings.
    assert not _by_value(result).get("alice")
    assert not _by_value(result).get("bob")


def test_self_g_test_measures_a_bounded_value_against_its_own_life(svc):
    """`late` is uniform across day 3, so it is unremarkable *for itself*.

    The complement is the value's own lifetime minus the slice under test,
    not the whole scope. A whole-scope denominator would divide `late`'s rate
    by the third of the timeline it lived through and report it "up" in every
    one of its own slices — the arithmetic alone clears min_ratio, with no
    change in behaviour to point at. Churning identifiers are exactly what
    this detector is pointed at, so that shape is a systematic false positive
    rather than an edge case.
    """
    result = svc.find_proportion_shifts(_CASE, [_MAIN], fields=["attr:user"], limit=200)
    assert result.status == "ok", result.warnings
    assert not _by_value(result, "up").get("late")


def test_self_g_test_reports_the_complement_frame_it_measured_against(svc):
    """An analyst reading a rate ratio must know what the denominator was.

    `eve` is confined to a single slice and keeps the whole-scope complement;
    a value spanning several slices is measured against its own life. Both
    are on the finding, because the two answer different questions.
    """
    result = svc.find_proportion_shifts(_CASE, [_MAIN], fields=["attr:user"], limit=200)
    eve = _by_value(result, "up")["eve"][0]
    assert eve.details["value_active_slices"] == 1
    assert eve.details["rest_frame"] == "whole-scope"
    assert eve.details["rest_count"] == 0
    for findings in _by_value(result).values():
        for f in findings:
            if f.details["value_active_slices"] > 1:
                assert f.details["rest_frame"] == "active-span"
                assert f.details["rest_slices"] == f.details["value_active_slices"] - 1


# ---------------------------------------------------------------------------
# Distribution drift — self-drift
# ---------------------------------------------------------------------------


def test_self_drift_numeric_shift_confined_to_one_slice(svc):
    result = svc.find_distribution_drift(_CASE, [_MAIN], fields=["attr:bytes"], limit=100)
    assert result.status == "ok", result.warnings
    assert result.method == "self-drift"
    ks = [f for f in result.results if f.test == "ks"]
    assert ks, result.warnings
    top = ks[0]
    assert top.direction == "up"
    assert top.details["method"] == "self-drift"
    assert "baseline_median" not in top.details
    assert top.details["rest_median"] < top.details["window_median"]
    start = datetime.fromisoformat(top.details["window_start"])
    end = datetime.fromisoformat(top.details["window_end"])
    assert start <= _T0 + timedelta(days=1, hours=2) < end


def test_self_drift_categorical_mix_change_names_its_contributors(svc):
    result = svc.find_distribution_drift(_CASE, [_MAIN], fields=["attr:user"], limit=100)
    assert result.status == "ok", result.warnings
    g = [f for f in result.results if f.test == "g-test-k"]
    assert g
    top = g[0]
    assert top.direction == "mixed"
    contributors = {c["value"] for c in top.details["top_contributors"]}
    assert contributors & {"eve", "late"}
    assert all("rest_share" in c for c in top.details["top_contributors"])


def test_self_drift_bounds_its_per_slice_scans_and_says_what_it_skipped(svc, monkeypatch):
    """The numeric branch costs one whole-scope scan per (field, slice).

    Unbounded that is _MAX_AUTO_SCAN_FIELDS x stat_self_slices full passes in
    a single request, and the analysis cache cannot help the first run on a
    timeline — the one most likely to time out. The budget is spent
    round-robin so a narrowed run loses slice resolution rather than whole
    fields, and what it did not reach is disclosed rather than read as
    "nothing there".
    """
    monkeypatch.setattr(anomaly_stats, "_MAX_SELF_KS_QUERIES", 3)
    result = svc.find_distribution_drift(_CASE, [_MAIN], fields=["attr:bytes"], limit=100)
    assert result.status == "ok", result.warnings
    budget_note = [w for w in result.warnings if "scan budget" in w]
    assert budget_note, result.warnings
    assert "3 ran" in budget_note[0]
    assert "not evidence of nothing" in budget_note[0]
    ks = [f for f in result.results if f.test == "ks"]
    assert len(ks) <= 3


def test_self_drift_skips_and_warns_on_thin_slices(svc):
    # 20 evenly spaced `burst` rows are the only `svc` values with a numeric
    # look? No — `svc` is categorical; use min_samples high enough that the
    # thinnest slices of `attr:bytes` fall under it.
    result = svc.find_distribution_drift(
        _CASE, [_MAIN], fields=["attr:bytes"], limit=100, min_samples=100_000
    )
    assert any("skipped" in w for w in result.warnings)
    assert result.results == []


# ---------------------------------------------------------------------------
# Event sequences — rare-ngram
# ---------------------------------------------------------------------------


def test_rare_ngram_finds_the_stray_ordering_and_not_the_cycle(svc):
    result = svc.find_sequence_novelty(_CASE, [_MAIN], series_field="attr:act", ngram=3, limit=100)
    assert result.status == "ok", result.warnings
    assert result.method == "rare-ngram"
    values = {f.value for f in result.results}
    assert any("X" in v for v in values), values
    assert "A → B → C" not in values
    f = next(f for f in result.results if "X" in f.value)
    assert f.details["rarity_floor"] == 3
    assert f.details["scope_ngram_total"] > 1000
    assert f.count <= 3
    assert f.details["allowlist_field"] == "attr:act"
    assert "window_label" not in f.details


def test_rare_ngram_sums_counts_case_wide_before_applying_the_floor(svc):
    single = svc.find_sequence_novelty(_CASE, [_MAIN], series_field="attr:act", ngram=3, limit=100)
    assert "X → Y → Z" in {f.value for f in single.results}
    both = svc.find_sequence_novelty(
        _CASE, [_MAIN, _CYCLE], series_field="attr:act", ngram=3, limit=100
    )
    assert "X → Y → Z" not in {f.value for f in both.results}


def test_rare_ngram_honors_the_gap_bound(svc):
    """With a 1 s gap bound the 2 s-spaced sequence rows never form a sequence."""
    result = svc.find_sequence_novelty(
        _CASE, [_MAIN], series_field="attr:act", ngram=3, limit=100, max_gap_seconds=1
    )
    assert result.status == "insufficient_data"
    assert any("no complete sequences" in w for w in result.warnings)
