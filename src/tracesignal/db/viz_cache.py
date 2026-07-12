"""Process-local cache for baseline-compare viz layers (roadmap M24c).

In baseline compare mode the comparison layer drops every filter and keeps
only the timeline scope plus the primary's explicit time window — its scan
result is therefore identical across every filtered primary render, yet was
recomputed (a full-timeline scan) on each one. This LRU memoizes those
baseline-layer results.

Freshness lives in the cache **key**, not in a TTL: callers fold a
per-source fingerprint (``source_field_stats.computed_at`` + ``events_total``,
which move on exactly the two source-mutation events — ingest and enrichment
apply) into every key, so a mutated source changes the key and stale entries
simply stop being addressed until LRU eviction reclaims them.

Process-local by design: a multi-worker deployment warms one cache per
worker — wasteful but never incorrect (the fingerprint is in the key). Sized
in entries (``TS_VIZ_BASELINE_CACHE_ENTRIES``, 0 disables); entries are
small, bounded aggregates (≤ ~500 buckets/terms/bins), not raw events.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Hashable
from typing import Any

from tracesignal.core.config import get_settings


class LruCache:
    """Thread-safe LRU keyed on hashable tuples.

    The compare methods run inside FastAPI's threadpool (via
    ``run_in_threadpool``), so a ``threading.Lock`` — not an asyncio one —
    guards the map, mirroring ``HEAVY_SCAN_GATE``'s reasoning. ``compute``
    runs *outside* the lock: a rare concurrent duplicate compute is cheaper
    than serializing multi-second scans behind a mutex.
    """

    def __init__(self, maxsize: int) -> None:
        self.maxsize = maxsize
        self._entries: OrderedDict[Hashable, Any] = OrderedDict()
        self._lock = threading.Lock()

    def get_or_compute(self, key: Hashable, compute: Callable[[], Any]) -> Any:
        if self.maxsize <= 0:
            return compute()
        with self._lock:
            if key in self._entries:
                self._entries.move_to_end(key)
                return self._entries[key]
        value = compute()
        with self._lock:
            self._entries[key] = value
            self._entries.move_to_end(key)
            while len(self._entries) > self.maxsize:
                self._entries.popitem(last=False)
        return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)


_baseline_cache: LruCache | None = None
_baseline_cache_lock = threading.Lock()


def baseline_cache() -> LruCache:
    """The shared baseline-layer cache, sized from settings on first use."""
    global _baseline_cache
    if _baseline_cache is None:
        with _baseline_cache_lock:
            if _baseline_cache is None:
                _baseline_cache = LruCache(get_settings().viz_baseline_cache_entries)
    return _baseline_cache


def reset_baseline_cache() -> None:
    """Drop the cache instance (tests / settings changes)."""
    global _baseline_cache
    with _baseline_cache_lock:
        _baseline_cache = None
