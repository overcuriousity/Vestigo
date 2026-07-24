"""Unit tests for the per-request tool guard in ``agent/runtime.py``.

The guard sits between the agent and the MCP toolset and enforces two things
inside a single model request: identical calls are collapsed to a
back-reference, and one request's total tool-return bytes cannot run away past
a ceiling. Both were provoked by the 2026-07-23 overflow, where one assistant
turn issued three byte-identical ``search_events`` calls.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from vestigo.agent.runtime import _RequestGuardToolset
from vestigo.agent.window import WindowStats


class _FakeWrapped:
    """Stand-in MCP toolset: records the calls that reach it and returns a
    payload of a caller-chosen size so the ceiling can be exercised."""

    def __init__(self, payload_bytes: int = 10):
        self.calls: list[tuple[str, dict]] = []
        self.payload_bytes = payload_bytes

    async def call_tool(self, name, tool_args, ctx, tool):
        self.calls.append((name, dict(tool_args)))
        return {"blob": "x" * self.payload_bytes}


def _ctx(run_step: int, tool_call_id: str) -> SimpleNamespace:
    return SimpleNamespace(run_step=run_step, tool_call_id=tool_call_id)


def _call(guard, name, args, *, run_step, call_id):
    return asyncio.run(guard.call_tool(name, args, _ctx(run_step, call_id), tool=None))


def test_identical_call_in_one_request_is_deduped():
    wrapped = _FakeWrapped()
    stats = WindowStats()
    guard = _RequestGuardToolset(wrapped, byte_ceiling=0, stats=stats)

    first = _call(guard, "search_events", {"filters": {}}, run_step=1, call_id="tc1")
    second = _call(guard, "search_events", {"filters": {}}, run_step=1, call_id="tc2")

    assert "blob" in first
    assert second["duplicate_of"] == "tc1"
    assert "note" in second
    # The wrapped toolset ran exactly once — the duplicate never hit the tool.
    assert len(wrapped.calls) == 1
    assert stats.duplicate_calls == 1
    assert stats.reduced is True


def test_argument_order_does_not_defeat_dedupe():
    wrapped = _FakeWrapped()
    guard = _RequestGuardToolset(wrapped, byte_ceiling=0, stats=WindowStats())

    _call(guard, "search_events", {"a": 1, "b": 2}, run_step=1, call_id="tc1")
    second = _call(guard, "search_events", {"b": 2, "a": 1}, run_step=1, call_id="tc2")

    assert second["duplicate_of"] == "tc1"
    assert len(wrapped.calls) == 1


def test_different_arguments_are_not_deduped():
    wrapped = _FakeWrapped()
    guard = _RequestGuardToolset(wrapped, byte_ceiling=0, stats=WindowStats())

    _call(guard, "search_events", {"filters": {"src_ip": "a"}}, run_step=1, call_id="tc1")
    second = _call(guard, "search_events", {"filters": {"src_ip": "b"}}, run_step=1, call_id="tc2")

    assert "blob" in second
    assert len(wrapped.calls) == 2


def test_dedupe_resets_when_the_request_advances():
    """The cache is per model request (run_step). The same call in the next
    request must run again — the conversation moved on."""
    wrapped = _FakeWrapped()
    stats = WindowStats()
    guard = _RequestGuardToolset(wrapped, byte_ceiling=0, stats=stats)

    _call(guard, "search_events", {"filters": {}}, run_step=1, call_id="tc1")
    again = _call(guard, "search_events", {"filters": {}}, run_step=2, call_id="tc2")

    assert "blob" in again
    assert len(wrapped.calls) == 2
    assert stats.duplicate_calls == 0


def test_first_return_is_never_capped_however_large():
    # A lone oversized payload is the sliding window's truncate job, not the
    # guard's — the ceiling is about a running sum across calls.
    wrapped = _FakeWrapped(payload_bytes=10_000)
    stats = WindowStats()
    guard = _RequestGuardToolset(wrapped, byte_ceiling=100, stats=stats)

    first = _call(guard, "search_events", {"q": 1}, run_step=1, call_id="tc1")

    assert "blob" in first
    assert stats.results_capped == 0


def test_returns_are_capped_once_the_request_total_runs_away():
    wrapped = _FakeWrapped(payload_bytes=80)
    stats = WindowStats()
    guard = _RequestGuardToolset(wrapped, byte_ceiling=100, stats=stats)

    first = _call(guard, "search_events", {"q": 1}, run_step=1, call_id="tc1")
    second = _call(guard, "search_events", {"q": 2}, run_step=1, call_id="tc2")

    assert "blob" in first
    assert second["reduced"] is True
    assert "get_event" in second["note"]
    assert stats.results_capped == 1
    assert stats.reduced is True


def test_ceiling_disabled_when_no_budget():
    wrapped = _FakeWrapped(payload_bytes=10_000)
    stats = WindowStats()
    guard = _RequestGuardToolset(wrapped, byte_ceiling=0, stats=stats)

    for i in range(5):
        out = _call(guard, "search_events", {"q": i}, run_step=1, call_id=f"tc{i}")
        assert "blob" in out
    assert stats.results_capped == 0


def test_a_rejected_call_is_not_cached():
    """A call that raises never produces a result — the model's corrected
    retry with the same args must re-run, not dedupe against a phantom."""

    class _Flaky:
        def __init__(self):
            self.n = 0

        async def call_tool(self, name, tool_args, ctx, tool):
            self.n += 1
            if self.n == 1:
                raise ValueError("bad args")
            return {"ok": True}

    wrapped = _Flaky()
    guard = _RequestGuardToolset(wrapped, byte_ceiling=0, stats=WindowStats())

    with pytest.raises(ValueError, match="bad args"):
        _call(guard, "search_events", {"filters": {}}, run_step=1, call_id="tc1")
    retry = _call(guard, "search_events", {"filters": {}}, run_step=1, call_id="tc2")

    assert retry == {"ok": True}
    assert wrapped.n == 2
