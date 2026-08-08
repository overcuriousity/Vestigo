"""Guard: the agent imports private helpers out of the events router.

``agent/tools.py`` builds its MCP server by importing seven module-private
helpers from ``api/routers/events.py``. Nothing else enforces that contract, so
a refactor of that router can break the agent with no failing test anywhere —
the import lives inside a function body, so even collection stays green until
someone actually builds an MCP server. This is that test.
"""

from __future__ import annotations

import inspect


def test_events_router_still_exports_the_helpers_the_agent_imports():
    from vestigo.api.routers.events import (
        _get_query_service,
        _get_similarity_service,
        _persist_detector_run,
        _run_stat_detector,
        _serialize_stat_result,
        _validate_field_modes,
        _validate_regex,
    )

    for helper in (
        _get_query_service,
        _get_similarity_service,
        _persist_detector_run,
        _run_stat_detector,
        _serialize_stat_result,
        _validate_field_modes,
        _validate_regex,
    ):
        assert callable(helper)


def test_run_stat_detector_keeps_the_keyword_arguments_the_agent_passes():
    from vestigo.api.routers.events import _run_stat_detector

    params = inspect.signature(_run_stat_detector).parameters
    for name in (
        "detector",
        "fields",
        "series_field",
        "z_threshold",
        "baseline_id",
        "limit",
        "min_skew_seconds",
        "fdr_q",
        "min_ratio",
        "ngram_size",
        "min_support",
        "start",
        "end",
        "group_field",
        "max_gap_seconds",
        "field_mappings",
        "source_offsets",
    ):
        assert name in params, f"_run_stat_detector lost the {name} kwarg the agent passes"
