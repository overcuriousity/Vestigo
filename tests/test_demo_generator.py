"""The demo-case generator: determinism and signal presence.

Runs without ClickHouse — it checks the fabricated rows, not what a detector
makes of them (that is ``tests/test_demo_detector_coverage_clickhouse.py``).
"""

from __future__ import annotations

from vestigo.demo import build, metadata, scenario
from vestigo.demo.sources import linux, netflow, proxy, windows


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


def test_proxy_beacon_is_regular_and_only_in_the_suspect_window():
    from datetime import datetime as _dt

    beacons = [r for r in proxy.proxy_rows() if r["host"] == scenario.C2_HOST]
    assert len(beacons) > 300
    stamps = sorted(_dt.fromisoformat(r["datetime"]) for r in beacons)
    gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:], strict=False)]
    inner = [g for g in gaps if g < 3600]
    assert 280 < sum(inner) / len(inner) < 320, "beacon interval should sit near 300s"
    assert max(inner) - min(inner) < 120, "jitter stays tight enough to look periodic"
    assert stamps[0] >= scenario.BASELINE_END


def test_exfil_uploads_dwarf_the_baseline_and_shift_the_destination_mix():
    rows = list(proxy.proxy_rows())
    baseline_max = max(
        int(r["bytes_out"]) for r in rows if r["datetime"] < scenario.BASELINE_END.isoformat()
    )
    exfil = [r for r in rows if r["host"] == scenario.EXFIL_HOST]
    assert exfil
    assert max(int(r["bytes_out"]) for r in exfil) > 20 * baseline_max

    late = [r for r in rows if r["datetime"] >= scenario.PHASES[-1].start.isoformat()]
    share = sum(1 for r in late if r["host"] == scenario.EXFIL_HOST) / len(late)
    assert share > 0.02, "the destination mix must actually move, not just add a tail"


def test_proxy_carries_exactly_one_odd_user_agent():
    odd = {r["user_agent"] for r in proxy.proxy_rows() if "«" in r["user_agent"]}
    assert len(odd) == 1


def test_proxy_volume_and_ordering():
    rows = list(proxy.proxy_rows())
    assert 65_000 < len(rows) < 78_000
    assert [r["datetime"] for r in rows] == sorted(r["datetime"] for r in rows)


def test_netflow_volume_and_long_sessions():
    rows = list(netflow.netflow_rows())
    assert 25_000 < len(rows) < 36_000
    durations = [float(r["duration"]) for r in rows]
    assert max(durations) > 20 * (sum(durations) / len(durations))


def test_netflow_mirrors_every_beacon_request():
    beacons = sum(1 for r in proxy.proxy_rows() if r["host"] == scenario.C2_HOST)
    mirrored = sum(1 for r in netflow.netflow_rows() if r["dst_ip"] == netflow.C2_IP)
    assert mirrored == beacons


def test_netflow_denies_spike_on_the_file_server_during_lateral_movement():
    rows = [
        r
        for r in netflow.netflow_rows()
        if r["action"] == "deny" and r["dst_ip"] == netflow.FILE_SERVER_IP
    ]
    lateral = scenario.PHASES[2]
    inside = [
        r for r in rows if lateral.start.isoformat() <= r["datetime"] < lateral.end.isoformat()
    ]
    assert len(inside) > 100


def test_annotations_read_as_analyst_notes():
    assert 20 <= len(metadata.ANNOTATIONS) <= 30
    banned = ("detector", "value novelty", "z-score", "proportion shift", "demo", "vestigo")
    for annotation in metadata.ANNOTATIONS:
        lowered = annotation.note.lower()
        assert not any(term in lowered for term in banned), annotation.note
        assert annotation.tags
        assert scenario.SCENARIO_START <= annotation.after <= scenario.SCENARIO_END


def test_tag_vocabulary_is_closed():
    used = {tag for a in metadata.ANNOTATIONS for tag in a.tags}
    assert used <= set(metadata.TAGS)
    assert used == set(metadata.TAGS), "every documented tag should actually be used"


def test_every_source_carries_notes():
    keys = {a.source_key for a in metadata.ANNOTATIONS}
    assert keys == {"windows", "linux", "proxy", "netflow"}


def test_sigma_rules_are_valid_yaml_with_the_expected_titles():
    import yaml

    titles = set()
    for title, text in metadata.SIGMA_RULES:
        parsed = yaml.safe_load(text)
        assert parsed["title"] == title
        assert parsed["detection"] and parsed["logsource"]
        titles.add(title)
    assert len(titles) == 4


def test_baseline_windows_match_the_phases_and_avoid_the_baseline():
    windows_json = metadata.baseline_windows()
    assert len(windows_json) == 4
    assert [w["label"] for w in windows_json] == [p.label for p in scenario.PHASES]
    assert all(w["start"] >= scenario.BASELINE_END.isoformat() for w in windows_json)


def test_views_carry_the_full_frontend_payload():
    expected_keys = set(metadata._view_payload().keys())
    assert len(metadata.VIEWS) == 5
    for view in metadata.VIEWS:
        assert set(view.payload) == expected_keys


def test_story_has_a_narrative_arc():
    from vestigo.stories.schemas import BLOCK_KINDS

    kinds = {block.kind for block in metadata.STORY_BLOCKS}
    assert kinds <= set(BLOCK_KINDS), "story blocks must use real block kinds"
    assert kinds == set(BLOCK_KINDS), "the demo story must show every block kind"
    assert (metadata.STORY_BLOCKS[0].text or "").startswith("## ")
    assert len(metadata.STORY_BLOCKS) >= 8
    body = " ".join(block.text or "" for block in metadata.STORY_BLOCKS).lower()
    assert "recommend" in body
    assert "unrelated" in body, "the benign findings must be called out as benign"


def test_story_embeds_name_referents_that_exist():
    view_names = {view.name for view in metadata.VIEWS}
    chart_names = {chart.name for chart in metadata.CHARTS}
    timeline_names = {name for name, _description, _keys in build.TIMELINES}
    for block in metadata.STORY_BLOCKS:
        if block.kind == "view_ref":
            assert block.view in view_names
        if block.kind == "chart_ref":
            assert block.chart in chart_names
        if block.kind in ("view_ref", "chart_ref"):
            assert block.timeline in timeline_names
        if block.kind == "event_ref":
            assert block.event is not None
            assert block.event.source_key in {key for key, *_rest in build.SOURCES}
            assert block.caption, "a frozen event needs a caption saying why it is there"


def test_saved_chart_configs_parse_as_chart_specs():
    """A config in the wrong shape draws nothing, silently, everywhere."""
    from vestigo.stories.export import _stored_chart_to_spec

    assert metadata.CHARTS
    for chart in metadata.CHARTS:
        assert chart.config["v"] == 1
        spec = _stored_chart_to_spec(chart.config)
        assert spec.chart_type == chart.config["chartType"]
