"""Tests for tracesignal.db._dt — shared UTC datetime normalization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from tracesignal.db._dt import ensure_utc, to_clickhouse_utc


def test_ensure_utc_attaches_utc_to_naive_datetime():
    naive = datetime(2026, 6, 25, 7, 30, 1)
    assert ensure_utc(naive) == naive.replace(tzinfo=UTC)


def test_ensure_utc_leaves_aware_non_utc_datetime_unchanged():
    """ensure_utc is an attach, not a convert — an already-aware, non-UTC
    datetime passes through with its original offset intact. Callers that
    need true UTC wall-clock digits must additionally `.astimezone(UTC)`."""
    plus_two = timezone(timedelta(hours=2))
    aware = datetime(2026, 6, 25, 9, 30, 1, tzinfo=plus_two)
    assert ensure_utc(aware) is aware


def test_to_clickhouse_utc_converts_non_utc_offset():
    """A `+02:00` window bound must land at the correct UTC instant, not
    the wall-clock digits with the offset silently dropped — regression
    test for F8 (value_novelty/frequency detectors disagreeing on where a
    temporal window boundary falls)."""
    plus_two = timezone(timedelta(hours=2))
    aware = datetime(2026, 6, 25, 14, 0, 0, tzinfo=plus_two)
    assert to_clickhouse_utc(aware) == "2026-06-25 12:00:00"


def test_to_clickhouse_utc_naive_datetime_treated_as_utc():
    naive = datetime(2026, 6, 25, 12, 0, 0)
    assert to_clickhouse_utc(naive) == "2026-06-25 12:00:00"


def test_to_clickhouse_utc_precise_keeps_millisecond_precision():
    naive = datetime(2026, 6, 25, 12, 0, 0, 123456)
    assert to_clickhouse_utc(naive, precise=True) == "2026-06-25 12:00:00.123"


def test_to_clickhouse_utc_precise_converts_non_utc_offset():
    plus_two = timezone(timedelta(hours=2))
    aware = datetime(2026, 6, 25, 14, 0, 0, 500000, tzinfo=plus_two)
    assert to_clickhouse_utc(aware, precise=True) == "2026-06-25 12:00:00.500"
