"""The demo-case generator: determinism and signal presence.

Runs without ClickHouse — it checks the fabricated rows, not what a detector
makes of them (that is ``tests/test_demo_detector_coverage_clickhouse.py``).
"""

from __future__ import annotations

from tools.demo_case import scenario
from tools.demo_case.sources import linux, windows


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


def test_windows_volume_and_shape():
    rows = list(windows.windows_rows())
    assert 85_000 < len(rows) < 95_000
    assert set(rows[0]) == set(windows.WINDOWS_HEADER)
    assert [r["datetime"] for r in rows] == sorted(r["datetime"] for r in rows)


def test_spray_concentrates_failed_logons_in_the_recon_window():
    recon = scenario.PHASES[0]
    failures = [r for r in windows.windows_rows() if r["event_id"] == "4625"]
    inside = [
        r for r in failures if recon.start.isoformat() <= r["datetime"] < recon.end.isoformat()
    ]
    outside_per_day = (len(failures) - len(inside)) / 23
    assert len(inside) > 8 * outside_per_day, "spray must dwarf the daily baseline"


def test_persistence_service_name_is_new():
    rows = list(windows.windows_rows())
    services = {r["service_name"] for r in rows if r["event_id"] == "7045" and r["service_name"]}
    assert windows.PERSISTENCE_SERVICE in services
    baseline_services = {
        r["service_name"]
        for r in rows
        if r["event_id"] == "7045" and r["datetime"] < scenario.BASELINE_END.isoformat()
    }
    assert windows.PERSISTENCE_SERVICE not in baseline_services


def test_encoded_powershell_appears_only_in_the_suspect_window():
    encoded = [r for r in windows.windows_rows() if "-enc " in r["command_line"]]
    assert encoded
    assert all(r["datetime"] >= scenario.BASELINE_END.isoformat() for r in encoded)


def test_staged_archive_filename_carries_the_homoglyph():
    staged = [r for r in windows.windows_rows() if "о" in r["command_line"]]
    assert len(staged) == 1
    assert staged[0]["datetime"] >= scenario.PHASES[2].start.isoformat()


def test_contractor_gains_new_host_pairs_only_during_the_intrusion():
    rows = list(windows.windows_rows())
    def hosts(before: bool) -> set[str]:
        return {
            r["computer_name"]
            for r in rows
            if r["user"] == scenario.COMPROMISED_USER
            and (r["datetime"] < scenario.BASELINE_END.isoformat()) is before
        }

    assert hosts(True) == {scenario.COMPROMISED_HOME_WORKSTATION}
    assert len(hosts(False) - hosts(True)) >= 3


def test_linux_volume_and_out_of_order_records_only_on_the_skewed_host():
    rows = list(linux.linux_rows())
    assert 55_000 < len(rows) < 65_000

    skewed = [r for r in rows if r["hostname"] == scenario.SKEWED_HOST]
    out_of_order = sum(
        1 for a, b in zip(skewed, skewed[1:], strict=False) if b["timestamp"] < a["timestamp"]
    )
    assert 20 < out_of_order < 100

    others = [r for r in rows if r["hostname"] != scenario.SKEWED_HOST]
    assert all(a["timestamp"] <= b["timestamp"] for a, b in zip(others, others[1:], strict=False))


def test_linux_messages_cluster_into_templates_with_a_rare_late_one():
    rows = list(linux.linux_rows())
    shapes = {r["message"].split()[0] for r in rows}
    assert 8 < len(shapes) < 60
    rare = [r for r in rows if "unattended-upgrade" in r["message"]]
    assert rare
    assert all(r["timestamp"] >= scenario.PHASES[2].start.isoformat() for r in rare)


def test_backup_cadence_shifts_after_the_foothold():
    from datetime import datetime as _dt

    runs = [
        _dt.fromisoformat(r["timestamp"])
        for r in linux.linux_rows()
        if r["hostname"] == scenario.BACKUP_HOST and "backup run started" in r["message"]
    ]
    early = [r for r in runs if r < scenario.PHASES[1].start]
    late = [r for r in runs if r >= scenario.PHASES[1].start]
    assert early and late
    assert {r.hour for r in early} == {2}
    assert {r.hour for r in late} == {3}


def test_file_server_sudo_mix_shifts_during_the_intrusion():
    rows = [
        r
        for r in linux.linux_rows()
        if r["hostname"] == scenario.FILE_SERVER and r["program"] == "sudo"
    ]
    staging = {"tar", "find", "rsync"}

    def share(before: bool) -> float:
        window = [r for r in rows if (r["timestamp"] < scenario.BASELINE_END.isoformat()) is before]
        hits = sum(1 for r in window if any(c in r["message"] for c in staging))
        return hits / len(window)

    assert share(False) > 3 * share(True)
