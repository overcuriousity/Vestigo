"""Shared ClickHouse scan guardrails.

Single home for the SETTINGS clause every whole-corpus scan (GROUP BY over up
to hundreds of millions of rows) must carry: spill large aggregation states to
disk instead of ballooning RAM, cap the query's memory hard (fail one query,
not the server), and bound thread fan-out so several concurrent scans don't
oversubscribe the box. Values match the field-inventory scan that first needed
them (see the session-27 300M-row incident in docs/PROGRESS.md).
"""

HEAVY_SCAN_SETTINGS = (
    "SETTINGS max_threads = 8, "
    "max_bytes_before_external_group_by = 4000000000, "
    "max_memory_usage = 12000000000"
)
