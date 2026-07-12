"""Unit tests for the baseline-compare layer LRU (db/viz_cache.py, M24c)."""

from __future__ import annotations

import threading

from tracesignal.db.viz_cache import LruCache, baseline_cache, reset_baseline_cache


def test_get_or_compute_caches_and_evicts_lru():
    cache = LruCache(maxsize=2)
    calls: list[str] = []

    def make(key: str):
        def compute():
            calls.append(key)
            return f"v-{key}"

        return compute

    assert cache.get_or_compute("a", make("a")) == "v-a"
    assert cache.get_or_compute("a", make("a")) == "v-a"  # hit
    assert calls == ["a"]

    cache.get_or_compute("b", make("b"))
    cache.get_or_compute("a", make("a"))  # refresh a's recency
    cache.get_or_compute("c", make("c"))  # evicts b (LRU)
    assert calls == ["a", "b", "c"]
    cache.get_or_compute("b", make("b"))  # recompute after eviction
    assert calls == ["a", "b", "c", "b"]
    assert len(cache) == 2


def test_maxsize_zero_disables_caching():
    cache = LruCache(maxsize=0)
    calls: list[int] = []

    def compute():
        calls.append(1)
        return "x"

    assert cache.get_or_compute("k", compute) == "x"
    assert cache.get_or_compute("k", compute) == "x"
    assert len(calls) == 2
    assert len(cache) == 0


def test_thread_safety_smoke():
    cache = LruCache(maxsize=8)
    errors: list[Exception] = []

    def worker(i: int) -> None:
        try:
            for n in range(200):
                key = ("k", n % 16)
                assert cache.get_or_compute(key, lambda k=key: k) == key
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    assert len(cache) <= 8


def test_baseline_cache_singleton_and_reset():
    reset_baseline_cache()
    first = baseline_cache()
    assert baseline_cache() is first
    reset_baseline_cache()
    assert baseline_cache() is not first
    reset_baseline_cache()
