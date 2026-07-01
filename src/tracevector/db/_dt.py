"""Shared UTC-datetime normalization for ClickHouse rows.

The `events` table's `timestamp`/`ingest_time` columns carry no explicit
timezone component, so clickhouse-connect returns naive `datetime` objects.
Left as-is, a bare "YYYY-MM-DDTHH:MM:SS" string is ambiguous to JS's `Date`
parser (browsers treat it as local time), silently shifting the
displayed/compared timestamp by the browser's UTC offset. This has already
been independently re-fixed at several call sites — centralize it here.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any


def ensure_utc(value: datetime) -> datetime:
    """Attach UTC to a naive datetime; return already-aware datetimes unchanged."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value


def ensure_utc_iso(value: Any) -> Any:
    """Attach UTC and serialize a datetime to an ISO 8601 string.

    Tolerates values that are already strings or ``None`` (passed through
    unchanged) and anything without ``.isoformat()`` (stringified instead of
    raising).
    """
    if value is None or isinstance(value, str):
        return value
    if not hasattr(value, "isoformat"):
        return str(value)
    return ensure_utc(value).isoformat()
