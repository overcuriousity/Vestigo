"""Shared ClickHouse scan guardrails.

Single home for the SETTINGS clause every whole-corpus scan (GROUP BY over up
to hundreds of millions of rows) must carry: spill large aggregation states to
disk instead of ballooning RAM, cap the query's memory hard (fail one query,
not the server), and bound thread fan-out so several concurrent scans don't
oversubscribe the box. The three limits are ``TS_*`` tunables (see
``core/config.py``) so an operator can size them to the ClickHouse host rather
than the frozen defaults from the session-27 300M-row incident (docs/PROGRESS.md).

The clause is a string constant, built once at import from the process
settings, because it is interpolated into f-string SQL literals throughout the
detectors — a live function call there would embed the function repr, not the
clause.
"""

from tracesignal.core.config import get_settings


def _build_heavy_scan_settings() -> str:
    s = get_settings()
    return (
        f"SETTINGS max_threads = {s.stat_scan_max_threads}, "
        f"max_bytes_before_external_group_by = {s.stat_scan_external_group_by_bytes}, "
        f"max_memory_usage = {s.stat_scan_max_memory_bytes}"
    )


HEAVY_SCAN_SETTINGS = _build_heavy_scan_settings()
