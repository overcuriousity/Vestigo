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
    "vector_id",
)


def resolve_column_token(token: str) -> tuple[str | None, str | None]:
    """Classify a field token as a top-level column or an attribute key.

    Returns ``(column_name, None)`` for a recognized top-level column — the
    match is case/whitespace-insensitive, and an optional ``attr:`` prefix
    always means "attribute" even if the stripped name happens to match a
    column name. Returns ``(None, attribute_key)`` otherwise, with any
    ``attr:`` prefix stripped.
    """
    if token.startswith("attr:"):
        return None, token[5:]
    normalized = token.strip().lower()
    if normalized in TOP_LEVEL_EVENT_COLUMNS:
        return normalized, None
    return None, token
