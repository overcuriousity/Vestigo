"""The configuration we ship must report a truthful `risk`.

Issue #302's acceptance criterion, as a test rather than an assertion in a
comment: parse the numbers out of the file the compose stack actually mounts,
run them through the same pure functions the startup probe uses, and require
that scans plus caches fit under the ceiling.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from pathlib import Path

from vestigo.db._scan import (
    _COUNTED_CACHES,
    _resolve_scan_memory_budget,
    resolve_cache_bytes,
    resolve_clickhouse_ceiling,
)

MEMORY_XML = Path(__file__).resolve().parents[1] / "deploy" / "clickhouse" / "memory.xml"

# `mem_limit: 12g` in docker-compose.yml — the container the file is sized for.
REFERENCE_CONTAINER_LIMIT = 12 * 1024**3
# ClickHouse 26.6 defaults for the caches memory.xml does not pin.
CLICKHOUSE_DEFAULT_CACHES = {
    "index_mark_cache_size": 5 * 1024**3,
    "primary_index_cache_size": 5 * 1024**3,
    "index_uncompressed_cache_size": 0,
}


def _pinned() -> dict[str, float]:
    """The values memory.xml sets, ignoring its comment nodes.

    Float throughout: `max_server_memory_usage_to_ram_ratio` is 0.8, and reading
    it as an int truncates it to 0 — the same trap `server_resource_facts`
    documents on the probe side.
    """
    root = ET.parse(MEMORY_XML).getroot()
    return {child.tag: float(child.text or 0) for child in root if isinstance(child.tag, str)}


def _shipped_facts() -> dict[str, float]:
    """memory.xml, as the probe would see it after ClickHouse applied it."""
    facts: dict[str, float] = {
        "cgroup_memory_total": float(REFERENCE_CONTAINER_LIMIT),
        "os_memory_total": float(32 * 1024**3),
        **{k: float(v) for k, v in CLICKHOUSE_DEFAULT_CACHES.items()},
    }
    facts.update(_pinned())
    return facts


def test_shipped_memory_xml_pins_every_cache_we_count():
    """A cache left at its default is 5 GiB under a 9.5 GiB ceiling."""
    pinned = set(_pinned())
    for name in _COUNTED_CACHES:
        assert name in pinned, f"memory.xml leaves {name} at its ClickHouse default"


def test_shipped_memory_xml_keeps_the_uncompressed_caches_off():
    """They are *not* counted against the ceiling, because `use_uncompressed_cache`
    is off by default and they are never populated. Pinning them to 0 is what
    keeps that premise true here across a ClickHouse upgrade."""
    pinned = _pinned()
    for name in ("uncompressed_cache_size", "index_uncompressed_cache_size"):
        assert pinned.get(name) == 0, f"{name} is not counted, so it must be off"


def test_shipped_defaults_fit_under_their_own_ceiling():
    """Scans plus caches, against the ceiling the same file pins."""
    facts = _shipped_facts()
    ceiling, bounded = resolve_clickhouse_ceiling(facts)
    caches, _ = resolve_cache_bytes(facts)

    assert bounded, "the shipped file pins an explicit ceiling"
    per_query = _resolve_scan_memory_budget(0, 0.8, ceiling, concurrency=2, caches=caches)
    total = per_query * 2

    assert total + caches <= ceiling, (
        f"scans {total / 1024**3:.1f} GiB + caches {caches / 1024**3:.1f} GiB "
        f"exceed the {ceiling / 1024**3:.1f} GiB ceiling the file pins"
    )
    # Real headroom for merges, not a rounding sliver.
    assert (ceiling - total - caches) >= 1024**3
