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
    try:
        store = ClickHouseStore()
        store.init_schema()
    except Exception:
        pytest.skip("ClickHouse unavailable")
    return store


@pytest_asyncio.fixture(scope="module")
async def demo(tmp_path_factory, ch_store):
    """Seed the demo case once for the whole module, then drop its partitions."""
    from vestigo.db.postgres import PostgresStore

    db_path = tmp_path_factory.mktemp("demo-coverage") / "coverage.db"
    store = PostgresStore(url=f"sqlite+aiosqlite:///{db_path}")
    await store.init_schema()
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
