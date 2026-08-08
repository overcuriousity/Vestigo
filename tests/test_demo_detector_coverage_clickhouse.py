"""Every shipped analysis tool finds something in the demo case.

This is the demo case's contract. If a detector is retuned until the demo goes
quiet, that is a real regression in the onboarding experience, and this is
where it surfaces — do not weaken an assertion here to make a threshold change
pass; strengthen the fabricated signal in ``vestigo/demo/sources/`` instead.

Semantic similarity (``docs/ANOMALY_DETECTION.md`` §11) is absent on purpose:
it needs embeddings, which the demo deliberately does not require.

Skipped (visibly, via the clickhouse marker) when the dev compose stack is
absent.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from vestigo.db.anomaly_stats import AnalysisWindows, StatisticalAnomalyService, TimeWindow
from vestigo.db.clickhouse import ClickHouseStore
from vestigo.demo import scenario
from vestigo.demo.build import build_demo_case

pytestmark = pytest.mark.clickhouse

#: The sequence detectors default to ``series_field="artifact"``, which is
#: degenerate here: each demo source carries exactly one artifact value, so
#: every n-gram is ``(EVT, EVT, EVT)``. An analyst picks a field with something
#: to say, and so does this test — host order is what lateral movement changes.
_SERIES_FIELD = {
    "find_sequence_novelty": {"series_field": "attr:computer_name"},
    "find_sequence_motifs": {"series_field": "attr:event_id"},
}

#: Detectors that score a baseline against suspect windows.
_WINDOWED = (
    "find_value_novelty",
    "find_value_combos",
    "find_frequency_anomalies",
    "find_range_violations",
    "find_charset_novelty",
    "find_entropy_outliers",
    "find_proportion_shifts",
    "find_interval_periodicity",
    "find_distribution_drift",
    "find_sequence_novelty",
)

#: Detectors with no baseline/suspect split at all.
_WINDOWLESS = ("find_order_violations", "find_sequence_motifs", "list_log_templates")


@pytest.fixture(scope="module")
def ch_store():
    store = ClickHouseStore()
    store.init_schema()
    return store


@pytest_asyncio.fixture(scope="module")
async def demo(module_pg_database, ch_store):
    """Seed the demo case once for the whole module, then drop its partitions."""
    from vestigo.db.postgres import PostgresStore

    store = PostgresStore(url=module_pg_database)
    owner = await store.create_user(user_id="u_coverage", username="coverage")
    result = await build_demo_case(store, ch_store, owner_id=owner.id)
    sources = [s.id for s in await store.list_sources(result.case_id)]
    windows = AnalysisWindows(
        baseline=TimeWindow("baseline", scenario.SCENARIO_START, scenario.BASELINE_END),
        suspects=tuple(TimeWindow(p.label, p.start, p.end) for p in scenario.PHASES),
    )
    yield result.case_id, sources, windows
    for source_id in sources:
        ch_store.delete_source_events(result.case_id, source_id)
    await store.engine.dispose()


def _findings(result):
    """The findings inside a detector result.

    Statistical detectors return ``StatAnomalyResult.results``; template
    clustering returns ``LogTemplatesResult.templates``. Raising on anything
    else is deliberate — a silent fallback to the result object itself makes
    every assertion below pass vacuously, since the dataclass is always truthy.
    """
    for attribute in ("results", "templates"):
        if hasattr(result, attribute):
            return getattr(result, attribute)
    raise AssertionError(f"unrecognized detector result type: {type(result).__name__}")


@pytest.mark.parametrize("method", _WINDOWED)
def test_windowed_detector_finds_something(demo, ch_store, method):
    case_id, sources, windows = demo
    service = StatisticalAnomalyService(ch_store)
    result = getattr(service, method)(
        case_id, sources, windows=windows, **_SERIES_FIELD.get(method, {})
    )
    assert result.status == "ok", f"{method}: {result.status}"
    assert _findings(result), f"{method} found nothing in the demo case"


@pytest.mark.parametrize("method", _WINDOWLESS)
def test_windowless_detector_finds_something(demo, ch_store, method):
    case_id, sources, _windows = demo
    service = StatisticalAnomalyService(ch_store)
    result = getattr(service, method)(case_id, sources, **_SERIES_FIELD.get(method, {}))
    assert _findings(result), f"{method} found nothing in the demo case"


def test_every_shipped_statistical_detector_is_covered():
    """Adding a detector without adding it here should fail loudly."""
    public = {
        name
        for name in dir(StatisticalAnomalyService)
        if name.startswith("find_") and not name.startswith("find_fields")
    }
    covered = set(_WINDOWED) | set(_WINDOWLESS)
    missing = public - covered - {"find_motif_occurrences"}
    assert not missing, f"detectors with no demo coverage: {sorted(missing)}"


def test_sigma_rules_match_real_demo_events(demo, ch_store):
    """The four case-scoped rules must compile and hit.

    Compiles each rule the way the runner does and counts matching rows
    directly: a shipped rule that matches nothing is a broken demo, and the
    only way to know is to run it against the data.
    """
    from vestigo.demo import metadata
    from vestigo.sigma.backend import compile_rule
    from vestigo.sigma.rules import parse_rule_yaml

    case_id, sources, _windows = demo
    for title, yaml_text in metadata.SIGMA_RULES:
        parsed, error = parse_rule_yaml(yaml_text)
        assert parsed is not None, f"{title}: {error}"
        compiled = compile_rule(parsed, None, {})
        assert compiled.sql, f"{title}: {compiled.error}"

        in_clause, params = ch_store.string_in_clause("src", sources)
        rows = ch_store.client.query(
            f"SELECT count() FROM {ch_store.database}.events"
            " WHERE case_id = {cid:String}"
            f" AND source_id IN ({in_clause})"
            f" AND ({compiled.sql})",
            parameters={"cid": case_id, **params},
        ).result_rows
        assert rows[0][0] > 0, f"Sigma rule {title!r} matched no events in the demo case"


def test_gate_does_not_skip_a_method_the_demo_case_proves_applicable(demo, ch_store):
    """The gate's thresholds must be satisfied by data known to produce findings.

    Every other test in this file asserts that some analysis tool finds
    something in the demo case. That makes this file the right place to assert
    the gate never *stops offering* one of them: if a precondition would skip a
    method here, the precondition is wrong, because the method demonstrably
    works on this data.

    Builds ``PlanInputs`` directly from the demo's sources rather than through
    ``_collect_plan_inputs``: this module has no timeline and does not patch
    ``deps.get_store``, and the gate's inputs are what is under test, not the
    router plumbing around them.
    """
    from vestigo.core.config import get_settings
    from vestigo.db._buckets import query_timestamp_range
    from vestigo.db.analysis_plan import (
        PlanInputs,
        build_plan,
        numeric_tokens_from_stats,
        series_distinct_from_stats,
    )
    from vestigo.db.field_stats import compute_source_field_stats, merged_inventory

    case_id, source_ids, _windows = demo
    cfg = get_settings()

    stats = {
        source_id: compute_source_field_stats(ch_store, case_id, source_id)
        for source_id in source_ids
    }
    inventory, events_total = merged_inventory(stats)
    in_clause, params = ch_store.string_in_clause("src", source_ids)
    min_ts, max_ts = query_timestamp_range(
        ch_store.client,
        ch_store.database,
        f"case_id = {{cid:String}} AND source_id IN ({in_clause})",
        {"cid": case_id, **params},
    )
    numeric = numeric_tokens_from_stats(stats, cfg.analysis_gate_min_numeric_ratio)
    inputs = PlanInputs(
        inventory=inventory,
        numeric_tokens=numeric.tokens,
        numeric_tokens_examined=numeric.examined,
        series_distinct=series_distinct_from_stats(
            stats, "artifact", next((d for token, d, _c in inventory if token == "artifact"), 0)
        ),
        events_total=events_total,
        span_seconds=(max_ts - min_ts).total_seconds() if min_ts and max_ts else 0.0,
        frame="self",
        has_active_baseline=False,
    )
    plans = {p.method: p for p in build_plan(inputs, cfg)}

    for method, plan in plans.items():
        # The two-window methods legitimately need setup in the self frame —
        # there is no second window to test against until an analyst declares
        # one. Every other method must be offered on data this file proves
        # they find things in.
        if method in {"proportion_shift", "value_distribution_drift"}:
            assert plan.status == "needs_setup", f"{method}: {plan.status} ({plan.reason})"
            continue
        assert plan.status == "applicable", (
            f"gate skipped {method} on the demo case ({plan.reason}: {plan.reason_facts}), "
            "but this file asserts that method finds something here"
        )
