"""Tests for tracesignal.db._buckets — shared bucket-grid helpers."""

from __future__ import annotations

from datetime import UTC, datetime

from tracesignal.db._buckets import aligned_bucket_starts, bucket_interval_seconds


def test_aligned_bucket_starts_includes_trailing_partial_bucket():
    """Regression: the grid must include the bucket *containing* max_ts.

    ClickHouse's toStartOfInterval puts an event at max_ts into the bucket
    starting at floor-aligned max_ts — a grid stopping before that start
    silently dropped every event in the trailing partial bucket (the newest
    data) from zero-filled charts.
    """
    min_ts = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    max_ts = datetime(2024, 1, 1, 2, 30, tzinfo=UTC)
    starts = aligned_bucket_starts(min_ts, max_ts, 3600)
    assert starts == [
        "2024-01-01T00:00:00+00:00",
        "2024-01-01T01:00:00+00:00",
        "2024-01-01T02:00:00+00:00",  # contains max_ts (02:30)
    ]


def test_aligned_bucket_starts_max_on_boundary_gets_own_bucket():
    """max_ts exactly on a bucket boundary is itself a bucket start —
    toStartOfInterval(max_ts) == max_ts, so that bucket must exist."""
    min_ts = datetime(2024, 1, 1, 0, 0, tzinfo=UTC)
    max_ts = datetime(2024, 1, 1, 2, 0, tzinfo=UTC)
    starts = aligned_bucket_starts(min_ts, max_ts, 3600)
    assert starts[-1] == "2024-01-01T02:00:00+00:00"
    assert len(starts) == 3


def test_aligned_bucket_starts_point_range_yields_one_bucket():
    ts = datetime(2024, 1, 1, 0, 30, tzinfo=UTC)
    starts = aligned_bucket_starts(ts, ts, 3600)
    assert starts == ["2024-01-01T00:00:00+00:00"]


def test_aligned_bucket_starts_covers_every_event_bucket():
    """Every toStartOfInterval-aligned epoch between min and max appears."""
    min_ts = datetime(2026, 4, 1, tzinfo=UTC)
    max_ts = datetime(2026, 4, 5, 3, 57, tzinfo=UTC)
    interval = bucket_interval_seconds(min_ts, max_ts, 60)
    starts = aligned_bucket_starts(min_ts, max_ts, interval)
    aligned_max = int(max_ts.timestamp() // interval) * interval
    assert starts[-1] == datetime.fromtimestamp(aligned_max, tz=UTC).isoformat()


def test_bucket_interval_seconds_floors_at_one():
    ts = datetime(2024, 1, 1, tzinfo=UTC)
    assert bucket_interval_seconds(ts, ts, 60) == 1
