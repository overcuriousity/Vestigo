"""Shared allowlist of `events` table columns usable directly in SQL.

Both the events-filter query builder (`queries.py`) and the statistical
anomaly detectors (`anomaly_stats.py`) route a field token to either a
top-level column or an `attributes` Map lookup. Keeping one shared list
prevents the two from drifting — e.g. `parser_version` silently routing to
an always-empty attribute lookup in one module while resolving to the real
column in the other, which then makes a detector query "succeed" with zero
findings instead of surfacing the same values the events view sees for the
same field.
"""

from __future__ import annotations

from typing import Any

SYNTHETIC_COLUMN_EXPRESSIONS: dict[str, str] = {
    # W6: template_hash is a real column, but exposed under a token that
    # doesn't share its literal name — toString() so it round-trips through
    # the same String-typed filter/facet plumbing every other top-level
    # column token does (decimal string, JS-precision-safe).
    "template_id": "toString(template_hash)",
}


TOP_LEVEL_EVENT_COLUMNS = frozenset(
    {
        "message",
        "timestamp",
        "timestamp_desc",
        "artifact",
        "artifact_long",
        "display_name",
        "parser_name",
        "parser_version",
        "source_file",
        "source_id",
        "content_hash",
        "file_hash",
    }
)


# Top-level columns that aren't `String`-typed — every other member of
# `TOP_LEVEL_EVENT_COLUMNS` is a plain string column, but `timestamp` is
# `Nullable(DateTime64(3))`. Callers building string-comparison SQL (e.g.
# `col != ''`) around a resolved column must cast these first, or ClickHouse
# raises a type error instead of returning results.
TOP_LEVEL_NON_STRING_COLUMNS = frozenset({"timestamp"})


# Full per-event column projection, shared by `queries.py` (paginated query
# + export) and `anomaly_stats.py` (representative-event hydration) so a
# schema change only has to be made in one place — a column added to one but
# not the other would silently be missing from anomaly-hydrated events only.
EVENT_SELECT_COLUMNS: tuple[str, ...] = (
    "event_id",
    "case_id",
    "source_id",
    "source_file",
    "byte_offset",
    "line_number",
    "content_hash",
    "file_hash",
    "parser_name",
    "parser_version",
    "ingest_time",
    "message",
    "timestamp",
    "timestamp_desc",
    "artifact",
    "artifact_long",
    "display_name",
    "tags",
    "attributes",
    "embedding_model",
    "embedding_config_hash",
)


# The `FixedString(64)` columns. clickhouse-connect returns them as raw
# `bytes`, NUL-padded to the full 64 when the stored value is shorter (an
# unset `embedding_config_hash` is 64 NULs, not b""). Every read path has to
# decode them: FastAPI's encoder turns the bytes into a string with the NUL
# padding intact, and the export serializers (`json.dumps(..., default=str)`,
# the CSV writer) stringify via `repr`, which wraps the hex in `b'...'`. A
# hash that doesn't compare equal to the real SHA-256 defeats the point of
# storing it.
FIXED_STRING_COLUMNS: tuple[str, ...] = (
    "content_hash",
    "file_hash",
    "embedding_config_hash",
)


def decode_fixed_string(value: Any) -> Any:
    """Decode one `FixedString(64)` cell to a plain hex string.

    See :data:`FIXED_STRING_COLUMNS` for why. Non-`bytes` values are returned
    unchanged, so this is idempotent and safe to apply to a cell whose type
    depends on which column a field token resolved to — a value inventory over
    `content_hash` yields `bytes`, over any other field a `str`.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace").rstrip("\x00")
    return value


def decode_fixed_string_columns(row: dict[str, Any]) -> dict[str, Any]:
    """Decode `FixedString(64)` columns in *row* to plain hex strings, in place.

    See :data:`FIXED_STRING_COLUMNS`. Non-`bytes` values are left alone, so
    this is idempotent and safe on rows from a path that already decoded.
    """
    for key in FIXED_STRING_COLUMNS:
        value = row.get(key)
        if isinstance(value, bytes):
            row[key] = decode_fixed_string(value)
    return row


def resolve_column_token(token: str) -> tuple[str | None, str | None]:
    """Classify a field token as a top-level column or an attribute key.

    Returns ``(column_name, None)`` for a recognized top-level column — the
    match is case/whitespace-insensitive, and an optional ``attr:`` prefix
    always means "attribute" even if the stripped name happens to match a
    column name. Returns ``(None, attribute_key)`` otherwise, with any
    ``attr:`` prefix stripped.

    A token in :data:`SYNTHETIC_COLUMN_EXPRESSIONS` returns that expression
    instead of the bare token — for a real column exposed under a different
    name (e.g. ``template_id`` → ``toString(template_hash)``).
    """
    if token.startswith("attr:"):
        return None, token[5:]
    normalized = token.strip().lower()
    if normalized in SYNTHETIC_COLUMN_EXPRESSIONS:
        return SYNTHETIC_COLUMN_EXPRESSIONS[normalized], None
    if normalized in TOP_LEVEL_EVENT_COLUMNS:
        return normalized, None
    return None, token
