"""ClickHouse event query builder and result mapping."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from tracevector.db._buckets import bucket_interval_seconds, query_timestamp_range
from tracevector.db._columns import EVENT_SELECT_COLUMNS, resolve_column_token
from tracevector.db._dt import ensure_utc, ensure_utc_iso, to_clickhouse_utc
from tracevector.db.clickhouse import ClickHouseStore
from tracevector.db.field_recommend import (
    recommend_fields,
    recommend_fields_across_sources,
    timeline_cohesion_summary,
    timeline_universal_cohesion,
)

# `timestamp` is `Nullable(DateTime64(3))` — unparsable/missing datetimes at
# ingest genuinely produce NULL rows. ClickHouse sorts NULL after every real
# value in `ORDER BY timestamp {ASC|DESC}` (empirically verified: NULLS LAST
# regardless of direction), but a tuple predicate like `(timestamp, event_id)
# > (:ts, :id)` evaluates to NULL — not true/false — whenever the `timestamp`
# column itself is NULL, so those rows are silently unreachable by keyset
# pagination. Cursors and predicates instead treat a NULL timestamp as this
# sentinel: the maximum value DateTime64(3) can represent, guaranteed later
# than any real forensic log timestamp, so NULL-timestamp rows sort/seek
# exactly where they already land in ORDER BY (last).
_NULL_TIMESTAMP_SENTINEL = datetime(2299, 12, 31, 23, 59, 59, 999000, tzinfo=UTC)
_NULL_TIMESTAMP_SENTINEL_ISO = _NULL_TIMESTAMP_SENTINEL.isoformat()

# The minimum possible UUID sorts before every real event_id under native
# UUID comparison — used as the synthetic "any event at this timestamp"
# lower/upper bound for jump-to-time, which only knows a target time and not
# a specific anchor event (see `_parse_cursor`'s empty-event_id case in
# events.py). `toString(event_id) > ""` served the same purpose before the
# cursor predicate compared native UUIDs instead of strings.
_MIN_EVENT_ID = "00000000-0000-0000-0000-000000000000"


@dataclass
class TagFilter:
    """A unified tag match, pushed into ClickHouse as one OR'd predicate.

    A tag value can come from either of two independent tagging systems: a
    user annotation tag (Postgres) or a parser-derived ``Event.tags`` array
    entry (ClickHouse). ``postgres_event_ids`` is pre-resolved by the caller
    (a Postgres lookup can't be expressed inside a ClickHouse WHERE clause);
    ``tag_values`` is matched natively via ``hasAny(tags, ...)``. The two are
    OR'd together to reproduce "matches either system" without a second
    ClickHouse round trip to resolve parser-tag matches into Python first.
    """

    tag_values: list[str]
    postgres_event_ids: list[str]


@dataclass
class EventQuery:
    """Query parameters for the event viewer."""

    case_id: str
    source_ids: list[str] | None = None
    q: str | None = None
    artifact: str | None = None
    artifacts: list[str] | None = None
    source_id: str | None = None
    tag: str | None = None
    exclude_tag: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    field_filters: dict[str, str] = field(default_factory=dict)
    field_exclusions: dict[str, list[str]] = field(default_factory=dict)
    # Optional event_id allowlist (e.g. resolved from an annotation filter).
    # None means "no restriction"; an empty list matches zero events.
    event_ids: list[str] | None = None
    # Optional event_id denylist (e.g. resolved from an excluded tag filter).
    # None means "no restriction"; entries here are subtracted from the result.
    exclude_event_ids: list[str] | None = None
    # Unified tag include/exclude filters — distinct from event_ids/exclude_event_ids
    # because they carry OR-between-two-systems semantics internally, ANDed
    # alongside every other restriction (see TagFilter).
    tags_include: TagFilter | None = None
    tags_exclude: TagFilter | None = None
    limit: int = 50
    offset: int = 0
    order: Literal["asc", "desc"] = "desc"
    # Keyset cursors for bidirectional pagination — mutually exclusive with
    # each other and with `offset`. `after` seeks further in the requested
    # `order` direction (scroll down); `before` seeks backwards (scroll up).
    # Both are (timestamp, event_id) tuples matching the table's sort key.
    after: tuple[datetime, str] | None = None
    before: tuple[datetime, str] | None = None


def _iter_attr_items(attrs: Any) -> Iterator[tuple[str, Any]]:
    """Yield ``(key, value)`` from a ClickHouse Map column.

    clickhouse-connect returns Map columns as a ``dict``, but tolerate a list of
    pairs as well so the caller never has to care about the driver shape.
    """
    if isinstance(attrs, dict):
        yield from attrs.items()
    elif isinstance(attrs, (list, tuple)):
        for item in attrs:
            if isinstance(item, (list, tuple)) and len(item) == 2:
                yield item[0], item[1]


@dataclass
class EventPage:
    """Paginated event query result."""

    # `None` on keyset-cursor pages — the (expensive) COUNT(*) only runs on
    # the initial, uncursored fetch; later pages rely on `has_more_*` instead.
    total: int | None
    offset: int
    limit: int
    events: list[dict[str, Any]]
    has_more_after: bool = False
    has_more_before: bool = False
    # (timestamp, event_id) of the first/last row of this page — echoed back
    # so the caller can request the adjacent page without inspecting rows.
    next_cursor: tuple[str, str] | None = None
    prev_cursor: tuple[str, str] | None = None


# Top-level columns surfaced as choosable display columns in the UI.
# Separate from TOP_LEVEL_EVENT_COLUMNS (which is for filter routing).
TOP_LEVEL_DISPLAY_COLUMNS = [
    "timestamp",
    "source_id",
    "artifact",
    "artifact_long",
    "display_name",
    "message",
    "timestamp_desc",
    "tags",
    "_annotations",
]


def _escape_like(value: str) -> str:
    """Escape ClickHouse LIKE/ILIKE metacharacters in a literal search value.

    Without this, a literal ``%`` or ``_`` in the analyst's search text is
    interpreted as a wildcard, silently matching more than the literal
    substring they typed.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _normalize_event_row(row: dict[str, Any]) -> dict[str, Any]:
    """Attach an explicit UTC offset to timestamps and stringify `event_id`.

    The `events` table's `timestamp`/`ingest_time` columns have no explicit
    timezone component, so clickhouse-connect returns naive `datetime`
    objects for them. Left as-is, FastAPI's JSON encoder calls `.isoformat()`
    on the naive value, which omits the timezone offset — and a bare
    "YYYY-MM-DDTHH:MM:SS" string is ambiguous to JS's `Date` parser (browsers
    treat it as local time), silently shifting every event's displayed and
    compared timestamp by the browser's UTC offset.

    `event_id` comes back from clickhouse-connect as a `uuid.UUID` (the
    column is natively `UUID`), while every other part of the codebase
    (Postgres annotations, cursors, API responses) treats event ids as
    `str`. Stringify it here so callers never have to remember to do it
    themselves — e.g. export's annotation lookup keys its dict by `str`
    annotation `event_id`s and would silently miss every match otherwise.
    """
    for key in ("timestamp", "ingest_time"):
        value = row.get(key)
        if isinstance(value, datetime):
            row[key] = ensure_utc_iso(value)
    if "event_id" in row:
        row["event_id"] = str(row["event_id"])
    return row


# SQL column list for every event query (shared between paginated query and
# export), derived from the same column tuple anomaly_stats.py hydrates
# representative events with — see _columns.EVENT_SELECT_COLUMNS.
_EVENT_SELECT_COLUMNS = ",\n    ".join(EVENT_SELECT_COLUMNS)


class _ParameterizedQueryBuilder:
    """Build a ClickHouse WHERE clause using named parameters."""

    def __init__(self) -> None:
        self.conditions: list[str] = []
        self.parameters: dict[str, Any] = {}
        self._counter = 0

    def _param_name(self) -> str:
        name = f"p{self._counter}"
        self._counter += 1
        return name

    def add(self, condition: str) -> None:
        """Add a raw condition that does not need parameterization."""
        self.conditions.append(condition)

    def add_param(self, sql_fragment: str, value: Any) -> None:
        """Add a condition containing exactly one ':name' placeholder."""
        name = self._param_name()
        self.conditions.append(sql_fragment.replace(":name", f"{{{name}:String}}"))
        self.parameters[name] = value

    def add_in_list(self, column: str, values: list[str]) -> None:
        """Add a membership condition for a list of string values.

        Uses ``has({arr:Array(String)}, toString(column))`` rather than
        ``column IN ({p0}, {p1}, ...)`` because ClickHouse 24.x requires the
        second argument of ``IN`` to be a constant or table expression — a list
        of individual parameterized strings does not qualify.

        The column is wrapped in ``toString()`` because this is also used for
        ``event_id``, a native ``UUID`` column — ``has()`` requires a common
        type between the array and the column, and there is no implicit
        common type between ``Array(String)`` and ``UUID`` (this fails with
        ClickHouse error 386 NO_COMMON_TYPE), even when the array is empty.
        """
        name = self._param_name()
        self.conditions.append(f"has({{{name}:Array(String)}}, toString({column}))")
        self.parameters[name] = values

    def add_not_in_list(self, column: str, values: list[str]) -> None:
        """Add a negated membership condition — the inverse of :py:meth:`add_in_list`."""
        name = self._param_name()
        self.conditions.append(f"NOT has({{{name}:Array(String)}}, toString({column}))")
        self.parameters[name] = values

    def add_tag_filter(self, filt: TagFilter, negate: bool) -> None:
        """Add a unified tag predicate: ``hasAny(tags, :values) OR has(:ids, toString(event_id))``.

        OR-combines the two tagging systems in one ClickHouse expression
        instead of resolving parser-tag matches into Python and re-injecting
        them as a second event_id list (see :class:`TagFilter`). Negated as a
        whole for the exclude side, so "has neither" rather than "doesn't
        have one specific half."
        """
        tags_name = self._param_name()
        ids_name = self._param_name()
        clause = (
            f"(hasAny(tags, {{{tags_name}:Array(String)}}) "
            f"OR has({{{ids_name}:Array(String)}}, toString(event_id)))"
        )
        self.conditions.append(f"NOT {clause}" if negate else clause)
        self.parameters[tags_name] = filt.tag_values
        self.parameters[ids_name] = filt.postgres_event_ids

    def add_field_filter(self, key: str, value: str) -> None:
        """Add an equality filter on a top-level column or attribute."""
        column = self._column_expr(key)
        self.add_param(f"{column} = :name", value)

    def add_field_exclusion(self, key: str, values: list[str]) -> None:
        """Add a NOT IN exclusion on a top-level column or attribute."""
        column = self._column_expr(key)
        if len(values) == 1:
            self.add_param(f"{column} != :name", values[0])
        else:
            name = self._param_name()
            self.conditions.append(f"{column} NOT IN {{{name}:Array(String)}}")
            self.parameters[name] = values

    def add_tag_exclusion(self, value: str) -> None:
        """Exclude events that have *value* in their tags array."""
        self.add_param("NOT has(tags, :name)", value)

    def add_broad_text_search(self, value: str) -> None:
        """OR-match *value* as a substring across every field an analyst would
        expect a free-text search to cover: the fixed text columns, parser
        tags, and every value in the ``attributes`` Map — not just ``message``.
        """
        name = self._param_name()
        self.parameters[name] = f"%{_escape_like(value)}%"
        columns = [
            "message",
            "display_name",
            "artifact",
            "artifact_long",
            "timestamp_desc",
            "source_file",
        ]
        clauses = [f"{c} ILIKE {{{name}:String}}" for c in columns]
        clauses.append(f"arrayExists(v -> v ILIKE {{{name}:String}}, tags)")
        clauses.append(f"arrayExists(v -> v ILIKE {{{name}:String}}, mapValues(attributes))")
        self.conditions.append("(" + " OR ".join(clauses) + ")")

    def add_cursor(self, op: str, ts: datetime, event_id: str) -> None:
        """Add a keyset predicate ``(timestamp, event_id) {op} (ts, event_id)``.

        ClickHouse supports native tuple comparison, so ties at equal
        timestamps are broken by ``event_id`` in a single comparison — exactly
        matching the table's ``ORDER BY (..., timestamp, event_id)`` sort key,
        which is what makes this seek efficient (no OR-chain needed). Both
        sides must compare on the native ``UUID`` type, not ``toString()`` —
        ClickHouse's UUID ordering (its two internal UInt64 halves) does not
        match string ordering, so a ``toString()`` predicate would duplicate
        or skip rows sharing a timestamp across a page boundary. Native
        comparison also lets this predicate use the table's
        ``(case_id, source_id, timestamp, event_id)`` primary index, which
        ``toString()`` would defeat.

        ``timestamp`` is coalesced to :data:`_NULL_TIMESTAMP_SENTINEL` because
        a NULL component makes the whole tuple comparison evaluate to NULL
        (not true/false), which would silently drop every NULL-timestamp row
        from keyset-paginated results. An empty ``event_id`` is the
        jump-to-time synthetic bound (a target time with no anchor event) and
        is mapped to :data:`_MIN_EVENT_ID`, the lowest possible UUID, so it
        keeps sorting before every real event at that timestamp.
        """
        ts_name = self._param_name()
        id_name = self._param_name()
        sentinel_name = self._param_name()
        self.conditions.append(
            f"(coalesce(timestamp, {{{sentinel_name}:DateTime64(3)}}), event_id) {op} "
            f"({{{ts_name}:DateTime64(3)}}, {{{id_name}:UUID}})"
        )
        self.parameters[ts_name] = to_clickhouse_utc(ts, precise=True)
        self.parameters[id_name] = event_id or _MIN_EVENT_ID
        self.parameters[sentinel_name] = to_clickhouse_utc(_NULL_TIMESTAMP_SENTINEL, precise=True)

    def _column_expr(self, key: str) -> str:
        column, attr_key = resolve_column_token(key)
        if column is not None:
            return column
        # Map lookup; parameterize the key as well to stay defensive.
        key_param = self._param_name()
        self.parameters[key_param] = attr_key
        return f"attributes[{{{key_param}:String}}]"

    def where_clause(self) -> str:
        return " AND ".join(self.conditions)


class EventQueryService:
    """Query service for events stored in ClickHouse."""

    def __init__(self, store: ClickHouseStore | None = None) -> None:
        self.store = store or ClickHouseStore()

    def _build_where(self, query: EventQuery) -> tuple[str, dict[str, Any]]:
        """Build the parameterized WHERE clause for *query*.

        Returns the clause string and the bound parameters dict.
        Both are consumed by :py:meth:`query` (paginated) and
        :py:meth:`iter_events` (streaming export).
        """
        builder = _ParameterizedQueryBuilder()
        builder.add_param("case_id = :name", query.case_id)

        if query.source_ids is not None:
            builder.add_in_list("source_id", query.source_ids)

        if query.source_id is not None:
            builder.add_param("source_id = :name", query.source_id)

        if query.q:
            # ClickHouse tokenbf_v1 index supports hasToken and multiSearchAny.
            # We use ILIKE for substring search as a simple baseline, broadened
            # across every field (not just message) so the analyst's free-text
            # search box behaves like a real "search everything" field.
            builder.add_broad_text_search(query.q)

        # `artifact` (singular) and `artifacts` (plural) are two independent
        # optional filters on the same column. Applying both as separate ANDed
        # predicates would require an event's `artifact` to equal two
        # different values at once — unsatisfiable outside the rare case
        # where `artifact` also appears in `artifacts`, which then makes the
        # `artifacts` list redundant. Merge into one effective list instead
        # so a caller setting both intersects sanely rather than getting
        # silently-empty results.
        effective_artifacts = list(query.artifacts or [])
        if query.artifact and query.artifact not in effective_artifacts:
            effective_artifacts.append(query.artifact)
        if len(effective_artifacts) == 1:
            builder.add_param("artifact = :name", effective_artifacts[0])
        elif effective_artifacts:
            builder.add_in_list("artifact", effective_artifacts)

        if query.tag:
            builder.add_param("has(tags, :name)", query.tag)

        if query.exclude_tag:
            builder.add_tag_exclusion(query.exclude_tag)

        if query.start is not None:
            builder.add_param(
                "timestamp >= :name",
                to_clickhouse_utc(query.start),
            )

        if query.end is not None:
            builder.add_param(
                "timestamp <= :name",
                to_clickhouse_utc(query.end),
            )

        if query.event_ids is not None:
            builder.add_in_list("event_id", query.event_ids)

        if query.exclude_event_ids:
            builder.add_not_in_list("event_id", query.exclude_event_ids)

        if query.tags_include is not None:
            builder.add_tag_filter(query.tags_include, negate=False)

        if query.tags_exclude is not None:
            builder.add_tag_filter(query.tags_exclude, negate=True)

        if query.after is not None:
            ts, event_id = query.after
            op = "<" if query.order == "desc" else ">"
            builder.add_cursor(op, ts, event_id)

        if query.before is not None:
            ts, event_id = query.before
            op = ">" if query.order == "desc" else "<"
            builder.add_cursor(op, ts, event_id)

        for key, value in (query.field_filters or {}).items():
            builder.add_field_filter(key, value)

        for key, values in (query.field_exclusions or {}).items():
            builder.add_field_exclusion(key, values)

        return builder.where_clause(), builder.parameters

    def query(self, query: EventQuery) -> EventPage:
        """Execute an :py:class:`EventQuery` and return a paginated result.

        Two modes:
          - **Offset mode** (no `after`/`before` set): the original
            OFFSET/LIMIT behaviour, with a COUNT(*) for `total`. Used for the
            very first, unfiltered-by-cursor page.
          - **Cursor mode** (`after` or `before` set): seeks using the
            keyset predicate from `_build_where`, fetches `limit + 1` rows to
            derive `has_more_after`/`has_more_before` cheaply (no COUNT), and
            — for `before` — queries in the reverse sort direction to find
            the nearest preceding rows, then reverses them back into the
            page's natural (`query.order`) order before returning.
        """
        if query.after is not None and query.before is not None:
            raise ValueError("EventQuery cannot set both 'after' and 'before'")

        self.store.init_schema()

        where, parameters = self._build_where(query)
        database = self.store.database
        cursor_mode = query.after is not None or query.before is not None
        display_dir = query.order.upper()

        total: int | None = None
        if not cursor_mode:
            count_result = self.store.client.query(
                f"SELECT count() FROM {database}.events WHERE {where}",
                parameters=parameters,
            )
            total = count_result.result_rows[0][0] if count_result.result_rows else 0

        # A `before` seek wants the rows nearest the cursor, which means
        # scanning toward it — the opposite of the page's display order —
        # then reversing the result back into display order.
        if query.before is not None:
            fetch_dir = "ASC" if display_dir == "DESC" else "DESC"
        else:
            fetch_dir = display_dir

        fetch_limit = query.limit + 1 if cursor_mode else query.limit
        sql = f"""
            SELECT {_EVENT_SELECT_COLUMNS}
            FROM {database}.events
            WHERE {where}
            ORDER BY timestamp {fetch_dir}, event_id {fetch_dir}
            LIMIT {fetch_limit}
        """
        if not cursor_mode:
            sql += f" OFFSET {query.offset}"

        event_result = self.store.client.query(sql, parameters=parameters)
        columns = event_result.column_names
        rows = event_result.result_rows

        has_more_after = False
        has_more_before = False
        if cursor_mode:
            has_extra = len(rows) > query.limit
            rows = rows[: query.limit]
            if query.before is not None:
                has_more_before = has_extra
            else:
                has_more_after = has_extra
        elif total is not None:
            # Offset mode (only used for the very first page): derive
            # has_more_after from the COUNT already computed above, since
            # there's no cursor-side limit+1 trick to lean on here.
            has_more_after = (query.offset + len(rows)) < total

        events = [_normalize_event_row(dict(zip(columns, row, strict=False))) for row in rows]
        if query.before is not None:
            events.reverse()

        next_cursor = None
        prev_cursor = None
        if events:
            # A NULL timestamp must never reach the cursor as `None` — it
            # would serialize to JSON `null`, and `[null, id]` is not a
            # parseable "<iso-ts>,<event_id>" cursor string on the way back
            # in. Use the same sentinel the keyset predicate coalesces NULLs
            # to, so round-tripping this cursor lands back on the NULL rows.
            prev_ts = events[0]["timestamp"] or _NULL_TIMESTAMP_SENTINEL_ISO
            next_ts = events[-1]["timestamp"] or _NULL_TIMESTAMP_SENTINEL_ISO
            prev_cursor = (prev_ts, events[0]["event_id"])
            next_cursor = (next_ts, events[-1]["event_id"])

        return EventPage(
            total=total,
            offset=query.offset,
            limit=query.limit,
            events=events,
            has_more_after=has_more_after,
            has_more_before=has_more_before,
            next_cursor=next_cursor,
            prev_cursor=prev_cursor,
        )

    def iter_events(self, query: EventQuery, batch_size: int = 1000) -> Iterator[dict[str, Any]]:
        """Yield every event matching *query*, paging through ClickHouse in batches.

        This is used for streaming export where the full result set should not
        be materialised in memory.  The ``limit`` and ``offset`` fields of
        *query* are ignored — all matching rows are yielded.
        """
        self.store.init_schema()

        where, parameters = self._build_where(query)
        database = self.store.database
        sort_dir = query.order.upper()
        offset = 0

        while True:
            result = self.store.client.query(
                f"""
                SELECT {_EVENT_SELECT_COLUMNS}
                FROM {database}.events
                WHERE {where}
                ORDER BY timestamp {sort_dir}, event_id
                LIMIT {batch_size}
                OFFSET {offset}
                """,
                parameters=parameters,
            )
            columns = result.column_names
            rows = result.result_rows
            for row in rows:
                yield _normalize_event_row(dict(zip(columns, row, strict=False)))
            if len(rows) < batch_size:
                break
            offset += batch_size

    def query_event_refs(self, query: EventQuery, cap: int = 100_000) -> list[tuple[str, str]]:
        """Return (event_id, source_id) pairs for all events matching *query*.

        Like :py:meth:`query` but only fetches the two identifier columns,
        making it suitable for server-side bulk annotation.  ``limit`` and
        ``offset`` on *query* are ignored — the full matching set is returned
        up to *cap* rows to bound runaway writes.
        """
        self.store.init_schema()
        where, parameters = self._build_where(query)
        database = self.store.database

        result = self.store.client.query(
            f"SELECT event_id, source_id FROM {database}.events WHERE {where} LIMIT {cap}",
            parameters=parameters,
        )
        return [(row[0], row[1]) for row in result.result_rows]

    def list_fields(self, case_id: str, source_ids: list[str]) -> dict[str, list[str]]:
        """Return the displayable field names for a timeline.

        ``top_level`` contains the fixed columns common to every event.
        ``attributes`` contains the dynamic keys aggregated from the ``attributes``
        Map across a sample of up to 50 000 events.  Useful for building a column
        picker in the UI.
        """
        self.store.init_schema()
        database = self.store.database

        params: dict[str, Any] = {"p0": case_id, "src": source_ids}

        result = self.store.client.query(
            f"""
            SELECT groupUniqArrayArray(mapKeys(attributes)) AS keys
            FROM {database}.events
            WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
            """,
            parameters=params,
        )
        raw_keys: list[str] = result.result_rows[0][0] if result.result_rows else []
        return {
            "top_level": TOP_LEVEL_DISPLAY_COLUMNS,
            "attributes": sorted(raw_keys),
        }

    def list_distinct_artifacts(
        self, case_id: str, source_ids: list[str], cap: int = 500
    ) -> list[str]:
        """Return distinct non-empty ``artifact`` values, for filter autocomplete."""
        self.store.init_schema()
        database = self.store.database
        params: dict[str, Any] = {"p0": case_id, "src": source_ids}
        result = self.store.client.query(
            f"""
            SELECT DISTINCT artifact
            FROM {database}.events
            WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
                AND artifact != ''
            ORDER BY artifact
            LIMIT {cap}
            """,
            parameters=params,
        )
        return [row[0] for row in result.result_rows]

    def list_distinct_parser_tags(self, case_id: str, source_ids: list[str]) -> list[str]:
        """Return distinct values from the parser-derived ``tags`` array column.

        Distinct from user annotation tags (stored in Postgres) — these come
        from the ingested/converted log data itself.
        """
        self.store.init_schema()
        database = self.store.database
        params: dict[str, Any] = {"p0": case_id, "src": source_ids}
        result = self.store.client.query(
            f"""
            SELECT groupUniqArrayArray(tags) AS tags
            FROM {database}.events
            WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
            """,
            parameters=params,
        )
        return sorted(result.result_rows[0][0]) if result.result_rows else []

    # Top-level fields meaningful for embedding (not IDs/provenance).
    _EMBEDDABLE_TOP_LEVEL = [
        "message",
        "timestamp_desc",
        "artifact_long",
        "display_name",
        "tags",
    ]

    def list_fields_by_artifact(
        self,
        case_id: str,
        source_ids: list[str],
        *,
        encode: Callable[[list[str]], list[list[float]]] | None = None,
        sample_per_artifact: int = 400,
    ) -> dict[str, Any]:
        """Return per-artifact field information for the embedding wizard.

        For each distinct ``artifact`` across the sources, returns the event
        count, the available top-level embedding fields, the dynamic attribute
        keys, and a *content-aware* recommendation produced by the hybrid
        heuristic→pairs strategy (see :mod:`tracevector.db.field_recommend`).

        When multiple sources are passed the recommendation uses
        :func:`~tracevector.db.field_recommend.recommend_fields_across_sources`
        which applies cross-source cohesion scoring so that the wizard
        default-selects only fields that carry **comparable content across all
        sources** (avoiding the batch-effect where embedding space separates
        events by source format rather than behaviour).

        The top-level ``cohesion`` key summarises the timeline's embedding
        substrate quality: ``"strong"`` / ``"moderate"`` / ``"weak"`` /
        ``"unavailable"``.

        Per-field verdicts now include ``present_in_sources`` and ``cohesion``
        when the multi-source path is used.

        ``encode`` is the embedding callable; pass ``None`` for heuristic-only.
        """
        self.store.init_schema()
        database = self.store.database

        params: dict[str, Any] = {"p0": case_id, "src": source_ids, "per": sample_per_artifact}

        # 1. Full attribute-key inventory + event count per artifact.
        inv = self.store.client.query(
            f"""
            SELECT
                artifact,
                count() AS n,
                groupUniqArrayArray(mapKeys(attributes)) AS attr_keys
            FROM {database}.events
            WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
            GROUP BY artifact
            ORDER BY n DESC
            """,
            parameters=params,
        )
        inventory = {
            (row[0] or ""): (row[1], sorted(row[2]) if row[2] else []) for row in inv.result_rows
        }

        # 2. Randomised value sample per artifact **and source** so that
        #    cross-source cohesion can be computed per field.
        cols = ["message", "timestamp_desc", "artifact_long", "display_name", "tags"]
        sample = self.store.client.query(
            f"""
            SELECT source_id, artifact, {", ".join(cols)}, attributes
            FROM (
                SELECT source_id, artifact, {", ".join(cols)}, attributes,
                       row_number() OVER (
                           PARTITION BY artifact, source_id ORDER BY rand()
                       ) AS _rn
                FROM (
                    SELECT source_id, artifact, {", ".join(cols)}, attributes
                    FROM {database}.events
                    WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
                    LIMIT 200000
                )
            )
            WHERE _rn <= {{per:UInt32}}
            """,
            parameters=params,
        )

        is_multi_source = len(source_ids) > 1

        # artifact -> source_id -> token -> list of sampled values  (multi-source)
        # artifact -> token -> list of sampled values               (single-source)
        samples_by_src: dict[str, dict[str, dict[str, list[Any]]]] = {}
        samples_flat: dict[str, dict[str, list[Any]]] = {}
        for row in sample.result_rows:
            src_id = row[0]
            artifact_name = row[1] or ""
            src_bucket = samples_by_src.setdefault(artifact_name, {}).setdefault(src_id, {})
            flat_bucket = samples_flat.setdefault(artifact_name, {})
            for i, col in enumerate(cols, start=2):
                value = row[i]
                if col == "tags" and isinstance(value, (list, tuple)):
                    value = " ".join(str(t) for t in value)
                src_bucket.setdefault(col, []).append(value)
                flat_bucket.setdefault(col, []).append(value)
            attrs = row[len(cols) + 2]
            for key, value in _iter_attr_items(attrs):
                src_bucket.setdefault(f"attr:{key}", []).append(value)
                flat_bucket.setdefault(f"attr:{key}", []).append(value)

        artifacts = []
        all_verdicts_for_cohesion = []

        for artifact_name, (count, attr_keys) in inventory.items():
            if is_multi_source:
                # Build field_samples_by_source: source_id → token → values.
                # Seed every candidate token for every source so absent fields
                # still get verdicts with present_in_sources=0.
                all_tokens = list(self._EMBEDDABLE_TOP_LEVEL) + [f"attr:{k}" for k in attr_keys]
                src_samples: dict[str, dict[str, list[Any]]] = {
                    src_id: {
                        token: samples_by_src.get(artifact_name, {}).get(src_id, {}).get(token, [])
                        for token in all_tokens
                    }
                    for src_id in source_ids
                }
                rec = recommend_fields_across_sources(
                    src_samples,
                    source_count=len(source_ids),
                    encode=encode,
                )
                all_verdicts_for_cohesion.extend(rec.verdicts)
                field_analysis = [
                    {
                        "token": v.token,
                        "recommended": v.recommended,
                        "kind": v.kind,
                        "reason": v.reason,
                        "present_in_sources": v.present_in_sources,
                        "cohesion": v.cohesion,
                    }
                    for v in rec.verdicts
                ]
            else:
                # Single source — use the original per-artifact recommender.
                flat_bucket = samples_flat.get(artifact_name, {})
                field_samples: dict[str, list[Any]] = {
                    t: flat_bucket.get(t, []) for t in self._EMBEDDABLE_TOP_LEVEL
                }
                for key in attr_keys:
                    token = f"attr:{key}"
                    field_samples[token] = flat_bucket.get(token, [])
                rec_single = recommend_fields(field_samples, encode=encode)
                field_analysis = [
                    {
                        "token": v.token,
                        "recommended": v.recommended,
                        "kind": v.kind,
                        "reason": v.reason,
                        "present_in_sources": 1,
                        "cohesion": None,
                    }
                    for v in rec_single.verdicts
                ]
                rec = rec_single  # for recommended / related_groups below

            artifacts.append(
                {
                    "artifact": artifact_name,
                    "count": count,
                    "top_level": self._EMBEDDABLE_TOP_LEVEL,
                    "attributes": attr_keys,
                    "recommended": rec.recommended,
                    "field_analysis": field_analysis,
                    "related_groups": rec.related_groups,
                }
            )

        # Aggregate cross-source cohesion summary for the whole timeline.
        #
        # Per-artifact cohesion (all_verdicts_for_cohesion) only sees a field
        # as "shared" when the *same* artifact type appears in ≥2 sources.
        # For timelines with disjoint artifact sets this always yields zero
        # shared fields, producing a spurious "weak" verdict.
        #
        # Instead we use timeline_universal_cohesion: pool each source's
        # values across ALL its artifacts for the canonical top-level fields
        # (message, display_name, tags, timestamp_desc) and compute cohesion
        # there.  These fields exist in every Timesketch source regardless of
        # artifact type, so they provide an honest cross-source signal.
        if is_multi_source:
            # Build source_id -> token -> [values] pooled across all artifacts.
            pooled_by_source: dict[str, dict[str, list[Any]]] = {}
            for _artifact_name, src_map in samples_by_src.items():
                for src_id, token_map in src_map.items():
                    dest = pooled_by_source.setdefault(src_id, {})
                    for token, vals in token_map.items():
                        dest.setdefault(token, []).extend(vals)
            universal_verdicts = timeline_universal_cohesion(
                pooled_by_source,
                encode=encode,
            )
            cohesion_summary = timeline_cohesion_summary(
                universal_verdicts,
                source_count=len(source_ids),
                encode_available=encode is not None,
            )
        else:
            cohesion_summary = timeline_cohesion_summary(
                [],
                source_count=len(source_ids),
                encode_available=encode is not None,
            )

        return {
            "artifacts": artifacts,
            "cohesion": {
                "level": cohesion_summary.level,
                "mean_cohesion": cohesion_summary.mean_cohesion,
                "shared_field_count": cohesion_summary.shared_field_count,
                "source_count": cohesion_summary.source_count,
                "message": cohesion_summary.message,
            },
        }

    def histogram(self, query: EventQuery, buckets: int = 60) -> dict[str, Any]:
        """Return a bucketed event-count histogram honoring all query filters.

        If the query has no explicit time range the min/max timestamps are
        derived from the filtered event set first.  Returns an empty bucket
        list when there are no matching events.
        """
        self.store.init_schema()
        where, parameters = self._build_where(query)
        database = self.store.database

        # Resolve time range.
        if query.start is not None and query.end is not None:
            min_ts: datetime | None = ensure_utc(query.start)
            max_ts: datetime | None = ensure_utc(query.end)
        else:
            min_ts, max_ts = query_timestamp_range(self.store.client, database, where, parameters)

        if min_ts is None or max_ts is None:
            return {"interval_seconds": 0, "min": None, "max": None, "buckets": []}

        interval = bucket_interval_seconds(min_ts, max_ts, buckets)

        bucket_result = self.store.client.query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {interval} second) AS bucket,
                   count() AS c
            FROM {database}.events
            WHERE {where} AND timestamp IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket
            """,
            parameters=parameters,
        )

        bucket_list = [
            {"start": ensure_utc_iso(row[0]), "count": row[1]} for row in bucket_result.result_rows
        ]
        return {
            "interval_seconds": interval,
            "min": min_ts.isoformat(),
            "max": max_ts.isoformat(),
            "buckets": bucket_list,
        }
