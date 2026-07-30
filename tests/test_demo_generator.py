"""The demo-case generator: determinism and signal presence.

Runs without ClickHouse — it checks the fabricated rows, not what a detector
makes of them (that is ``tests/test_demo_detector_coverage_clickhouse.py``).
"""

from __future__ import annotations

from tools.demo_case import scenario


def test_window_is_fixed():
    assert scenario.SCENARIO_START.isoformat() == "2026-05-01T00:00:00+00:00"
    assert scenario.SCENARIO_END.isoformat() == "2026-05-30T23:59:59+00:00"
    assert scenario.BASELINE_END.isoformat() == "2026-05-24T00:00:00+00:00"


def test_phases_are_contiguous_and_inside_the_suspect_window():
    assert [p.key for p in scenario.PHASES] == ["recon", "foothold", "lateral", "exfil"]
    assert scenario.PHASES[0].start >= scenario.BASELINE_END
    for earlier, later in zip(scenario.PHASES, scenario.PHASES[1:], strict=False):
        assert earlier.end <= later.start
    assert scenario.PHASES[-1].end <= scenario.SCENARIO_END


def test_streams_are_independent_and_reproducible():
    first = [scenario.rng("proxy").random() for _ in range(5)]
    second = [scenario.rng("proxy").random() for _ in range(5)]
    assert first == second
    assert first != [scenario.rng("netflow").random() for _ in range(5)]


def test_walk_stays_in_range_and_is_sorted():
    r = scenario.rng("walk-test")
    stamps = list(scenario.walk(scenario.SCENARIO_START, scenario.BASELINE_END, 100, r))
    assert 1500 < len(stamps) < 2500
    assert stamps == sorted(stamps)
    assert all(scenario.SCENARIO_START <= s < scenario.BASELINE_END for s in stamps)


def test_walk_is_weekday_weighted():
    r = scenario.rng("walk-weekday")
    stamps = list(scenario.walk(scenario.SCENARIO_START, scenario.BASELINE_END, 200, r))
    weekend = sum(1 for s in stamps if s.weekday() >= 5)
    assert weekend / len(stamps) < 0.15, "weekends must be visibly quieter than weekdays"
