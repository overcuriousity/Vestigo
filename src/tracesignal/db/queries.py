"""ClickHouse event query builder and result mapping."""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Literal

from tracesignal.db._buckets import (
    aligned_bucket_starts,
    bucket_interval_seconds,
    query_timestamp_range,
)
from tracesignal.db._columns import (
    EVENT_SELECT_COLUMNS,
    TOP_LEVEL_NON_STRING_COLUMNS,
    resolve_column_token,
)
from tracesignal.db._dt import ensure_utc, ensure_utc_iso, to_clickhouse_utc
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.field_mappings import (
    apply_mappings_to_attribute_keys,
    mapping_coalesce_expr,
    resolve_mapping,
)
from tracesignal.db.field_recommend import (
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

_logger = logging.getLogger(__name__)


def _guard_encoder(
    encode: Callable[[list[str]], list[list[float]]] | None,
) -> Callable[[list[str]], list[list[float]]] | None:
    """Wrap an embedding encoder so a runtime failure degrades to heuristic-only.

    The field wizard treats ``encode is None`` as "no embedding substrate" and
    falls back to pure-heuristic recommendations. A *remote* encoder, however,
    only fails when actually called (401, endpoint down, dropped connection),
    which would otherwise propagate a 500 out of an advisory endpoint. This
    guard catches the first failure, logs it once, and thereafter returns empty
    vectors — which the downstream centroid/cohesion code already treats as
    "unusable" — so the whole request quietly completes heuristic-only.
    """
    if encode is None:
        return None
    failed = False

    def guarded(texts: list[str]) -> list[list[float]]:
        nonlocal failed
        if failed:
            return []
        try:
            return encode(texts)
        except Exception:  # noqa: BLE001 - any encoder failure degrades gracefully
            failed = True
            _logger.warning(
                "Embedding encoder failed; field wizard degrading to heuristic-only",
                exc_info=True,
            )
            return []

    return guarded


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
    # Interpret `q` as an RE2 regular expression (ClickHouse `match()`)
    # instead of a literal ILIKE substring. Case-sensitive; analysts prefix
    # `(?i)` for case-insensitive matching.
    q_regex: bool = False
    artifact: str | None = None
    artifacts: list[str] | None = None
    source_id: str | None = None
    tag: str | None = None
    exclude_tag: str | None = None
    start: datetime | None = None
    end: datetime | None = None
    field_filters: dict[str, str] = field(default_factory=dict)
    field_exclusions: dict[str, list[str]] = field(default_factory=dict)
    # Match mode per field key ("exact" when absent): exact | wildcard | regex.
    # Wildcard: */? glob translated to ILIKE (case-insensitive, consistent
    # with the broad text search). Regex: RE2 via match(), case-sensitive
    # with `(?i)` opt-in — same semantics as q_regex. One mode per key; for
    # exclusions it applies to every value under that key.
    filter_modes: dict[str, str] = field(default_factory=dict)
    exclusion_modes: dict[str, str] = field(default_factory=dict)
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
    # Timeline field mappings (issue #10): canonical name → ordered raw
    # attribute keys, applied at query time wherever a field token resolves
    # to SQL. None/empty means no mapping. See db/field_mappings.py.
    field_mappings: dict[str, list[str]] | None = None


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


# Match modes accepted for field filters/exclusions. "exact" is the implied
# default everywhere a mode map has no entry for a field key.
VALID_MATCH_MODES = ("exact", "wildcard", "regex")


def _glob_to_like(value: str) -> str:
    """Translate an analyst glob (``*`` any-run, ``?`` single char) to a LIKE pattern.

    LIKE metacharacters are escaped FIRST (so a literal ``%``/``_``/``\\`` in
    the value stays literal), then the glob characters are mapped — ``*`` and
    ``?`` are not LIKE metacharacters, so they survive ``_escape_like``
    untouched.
    """
    return _escape_like(value).replace("*", "%").replace("?", "_")


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


def _field_column_expr(
    field_token: str,
    parameters: dict[str, Any],
    param_name: str | Callable[[], str],
    *,
    cast_non_string: bool = True,
    field_mappings: dict[str, list[str]] | None = None,
) -> str:
    """Resolve *field_token* to a SQL expression, binding an attribute key if needed.

    The single column-resolution implementation, shared by two call styles:

    - The viz aggregations pass an explicit, caller-chosen *param_name*
      string — they build their WHERE clause via ``_build_where`` first
      (which already claims ``p0..pN``) and then need one more parameter for
      the field-under-analysis without risking a name collision with those.
    - ``_ParameterizedQueryBuilder._column_expr`` passes its bound
      ``_param_name`` method so a fresh ``pN`` is minted lazily — only when
      the token actually resolves to an attribute key; an eager mint would
      shift the numbering of every subsequent parameter.

    ``cast_non_string`` wraps non-string top-level columns (``timestamp``) in
    ``toString(...)`` so string comparisons like ``!= ''`` and ``GROUP BY``
    work; the filter builder disables it because its equality/``NOT IN``
    predicates compare against typed literals directly.

    ``field_mappings`` (issue #10): a token naming a canonical mapped field
    resolves to a coalesce over its raw attribute keys instead — checked
    before column/attribute routing, since validation guarantees canonical
    names never collide with core columns or existing raw keys. ``attr:``
    tokens always bypass mappings.
    """
    mapped_raws = resolve_mapping(field_token, field_mappings)
    if mapped_raws:
        return mapping_coalesce_expr(mapped_raws, parameters, param_name)
    column, attr_key = resolve_column_token(field_token)
    if column is not None:
        if cast_non_string and column in TOP_LEVEL_NON_STRING_COLUMNS:
            return f"toString({column})"
        return column
    name = param_name() if callable(param_name) else param_name
    parameters[name] = attr_key
    return f"attributes[{{{name}:String}}]"


class _ParameterizedQueryBuilder:
    """Build a ClickHouse WHERE clause using named parameters."""

    def __init__(self, field_mappings: dict[str, list[str]] | None = None) -> None:
        self.conditions: list[str] = []
        self.parameters: dict[str, Any] = {}
        self._counter = 0
        self._field_mappings = field_mappings

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

    def _match_column_expr(self, key: str, mode: str) -> str:
        """Column expression for a field predicate under *mode*.

        Exact keeps typed comparison (no toString) so `=`/`NOT IN` compare
        against typed literals; wildcard/regex are string operations and need
        non-string top-level columns cast.
        """
        if mode == "exact":
            return self._column_expr(key)
        return _field_column_expr(
            key,
            self.parameters,
            self._param_name,
            cast_non_string=True,
            field_mappings=self._field_mappings,
        )

    def add_field_filter(self, key: str, value: str, mode: str = "exact") -> None:
        """Add a filter on a top-level column or attribute.

        Mode: exact equality, wildcard (*/? glob via ILIKE, case-insensitive),
        or regex (RE2 match(), case-sensitive). Routers validate mode strings
        up front; the ValueError here is a defense-in-depth backstop.
        """
        column = self._match_column_expr(key, mode)
        if mode == "exact":
            self.add_param(f"{column} = :name", value)
        elif mode == "wildcard":
            self.add_param(f"{column} ILIKE :name", _glob_to_like(value))
        elif mode == "regex":
            self.add_param(f"match({column}, :name)", value)
        else:
            raise ValueError(f"invalid match mode: {mode!r}")

    def add_field_exclusion(self, key: str, values: list[str], mode: str = "exact") -> None:
        """Add an exclusion on a top-level column or attribute.

        Exact uses `!=`/`NOT IN`; wildcard/regex OR one predicate per value
        and negate the whole, so "matches none of the patterns".
        """
        column = self._match_column_expr(key, mode)
        if mode == "exact":
            if len(values) == 1:
                self.add_param(f"{column} != :name", values[0])
            else:
                name = self._param_name()
                self.conditions.append(f"{column} NOT IN {{{name}:Array(String)}}")
                self.parameters[name] = values
        elif mode in ("wildcard", "regex"):
            clauses = []
            for value in values:
                name = self._param_name()
                if mode == "wildcard":
                    clauses.append(f"{column} ILIKE {{{name}:String}}")
                    self.parameters[name] = _glob_to_like(value)
                else:
                    clauses.append(f"match({column}, {{{name}:String}})")
                    self.parameters[name] = value
            self.conditions.append("NOT (" + " OR ".join(clauses) + ")")
        else:
            raise ValueError(f"invalid match mode: {mode!r}")

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

    def add_broad_text_regex(self, value: str) -> None:
        """OR-match *value* as an RE2 regex across the same fields as
        :py:meth:`add_broad_text_search`.

        The pattern is bound raw — no LIKE escaping, no ``%`` wrapping —
        because the analyst is writing regex syntax deliberately.
        ``match()`` is case-sensitive (unlike ILIKE); ``(?i)`` opts in to
        case-insensitivity. Regex matching cannot use the tokenbf_v1 index,
        so this is a full-scan predicate — an accepted tradeoff for an
        explicit, analyst-chosen regex search.
        """
        name = self._param_name()
        self.parameters[name] = value
        columns = [
            "message",
            "display_name",
            "artifact",
            "artifact_long",
            "timestamp_desc",
            "source_file",
        ]
        clauses = [f"match({c}, {{{name}:String}})" for c in columns]
        clauses.append(f"arrayExists(v -> match(v, {{{name}:String}}), tags)")
        clauses.append(f"arrayExists(v -> match(v, {{{name}:String}}), mapValues(attributes))")
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
        return _field_column_expr(
            key,
            self.parameters,
            self._param_name,
            cast_non_string=False,
            field_mappings=self._field_mappings,
        )

    def where_clause(self) -> str:
        return " AND ".join(self.conditions)


class EventQueryService:
    """Query service for events stored in ClickHouse."""

    def __init__(self, store: ClickHouseStore | None = None) -> None:
        self.store = store or ClickHouseStore()

    def _run_parallel(self, *fns: Callable[[], Any]) -> list[Any]:
        """Run independent ClickHouse scans concurrently in threads.

        Used for Compare-mode queries, where the primary/comparison layers
        are separate scans with no data dependency between them — running
        them in threads halves wall-clock latency over doing them serially.
        """
        with ThreadPoolExecutor(max_workers=len(fns)) as pool:
            futures = [pool.submit(fn) for fn in fns]
            return [f.result() for f in futures]

    def _build_where(self, query: EventQuery) -> tuple[str, dict[str, Any]]:
        """Build the parameterized WHERE clause for *query*.

        Returns the clause string and the bound parameters dict.
        Both are consumed by :py:meth:`query` (paginated) and
        :py:meth:`iter_events` (streaming export).
        """
        builder = _ParameterizedQueryBuilder(field_mappings=query.field_mappings)
        builder.add_param("case_id = :name", query.case_id)

        if query.source_ids is not None:
            builder.add_in_list("source_id", query.source_ids)

        if query.source_id is not None:
            builder.add_param("source_id = :name", query.source_id)

        if query.q:
            if query.q_regex:
                builder.add_broad_text_regex(query.q)
            else:
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
            builder.add_field_filter(key, value, mode=(query.filter_modes or {}).get(key, "exact"))

        for key, values in (query.field_exclusions or {}).items():
            builder.add_field_exclusion(
                key, values, mode=(query.exclusion_modes or {}).get(key, "exact")
            )

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

    def list_fields(
        self,
        case_id: str,
        source_ids: list[str],
        field_mappings: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Return the displayable field names for a timeline.

        ``top_level`` contains the fixed columns common to every event.
        ``attributes`` contains the dynamic keys aggregated from the ``attributes``
        Map across a sample of up to 50 000 events.  Useful for building a column
        picker in the UI.

        When the timeline defines ``field_mappings``, mapped raw keys are
        hidden from ``attributes`` and replaced by their canonical names;
        ``mapped`` carries the merge provenance so the UI can render
        ``ip_address ← src_ip, ip_addr``.

        ``attributes`` is returned sorted — deliberate, so the ColumnPicker
        (and any other consumer) gets deterministic ordering regardless of
        ClickHouse aggregation order.
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

        keys, provenance = apply_mappings_to_attribute_keys(sorted(raw_keys), field_mappings)
        # Enrichment-derived keys ("<attr>:<field>", e.g. "src_ip:geo_country")
        # live directly in events.attributes, so they surface through the
        # mapKeys scan above and are filterable like any other attribute.
        return {
            "top_level": TOP_LEVEL_DISPLAY_COLUMNS,
            "attributes": sorted(keys),
            "mapped": provenance,
        }

    def field_coverage(
        self,
        case_id: str,
        source_ids: list[str],
        sample_rows_per_source: int = 20_000,
        samples_per_field: int = 3,
    ) -> dict[str, Any]:
        """Return per-raw-attribute-key coverage across sources, for the timeline wizard.

        For every attribute key: which of the given sources carry it, its
        non-empty count there, and up to *samples_per_field* example values —
        the data the field-aggregation step needs to show merge candidates
        with real sample values. Scans up to *sample_rows_per_source* events
        per source (``LIMIT n BY source_id``), so counts are per-sample, not
        exact totals — coverage, not statistics.
        """
        self.store.init_schema()
        database = self.store.database
        params: dict[str, Any] = {
            "p0": case_id,
            "src": source_ids,
            "per": sample_rows_per_source,
        }
        result = self.store.client.query(
            f"""
            SELECT
                k,
                source_id,
                countIf(v != '') AS non_empty,
                groupUniqArrayIf({samples_per_field})(v, v != '') AS samples
            FROM (
                SELECT source_id, attributes
                FROM {database}.events
                WHERE case_id = {{p0:String}} AND has({{src:Array(String)}}, source_id)
                LIMIT {{per:UInt32}} BY source_id
            )
            ARRAY JOIN mapKeys(attributes) AS k, mapValues(attributes) AS v
            GROUP BY k, source_id
            HAVING non_empty > 0
            ORDER BY k, source_id
            """,
            parameters=params,
        )
        fields: dict[str, list[dict[str, Any]]] = {}
        for key, source_id, non_empty, samples in result.result_rows:
            fields.setdefault(key, []).append(
                {
                    "source_id": source_id,
                    "count": int(non_empty),
                    "samples": list(samples),
                }
            )
        return {
            "fields": [
                {"key": key, "sources": per_source} for key, per_source in sorted(fields.items())
            ],
            "sampled_rows_per_source": sample_rows_per_source,
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
        heuristic→pairs strategy (see :mod:`tracesignal.db.field_recommend`).

        When multiple sources are passed the recommendation uses
        :func:`~tracesignal.db.field_recommend.recommend_fields_across_sources`
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
        If ``encode`` is supplied but fails at call time (remote endpoint down,
        401, dropped connection), the wizard degrades to heuristic-only for the
        rest of the request instead of surfacing a 500 — a flaky embedder must
        not break field recommendation.
        """
        encode = _guard_encoder(encode)
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

        artifacts: list[dict[str, Any]] = []

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
        # Per-artifact cohesion only sees a field as "shared" when the *same*
        # artifact type appears in ≥2 sources. For timelines with disjoint
        # artifact sets this always yields zero shared fields, producing a
        # spurious "weak" verdict.
        #
        # Instead we use timeline_universal_cohesion: pool each source's
        # values across ALL its artifacts for the canonical top-level fields
        # (message, display_name, tags, timestamp_desc) and compute cohesion
        # there. These fields exist in every Timesketch source regardless of
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

    def field_terms(self, query: EventQuery, field_token: str, limit: int = 50) -> dict[str, Any]:
        """Return a top-N terms aggregation (value → count) for *field_token*.

        Honors all query filters (same ``_build_where`` as every other
        aggregation here), so the result always matches the currently
        filtered Explorer view. Powers both the per-value histogram modal's
        top-list and the Visualization page's nominal/ordinal chart types.

        ``other_count`` is the count of non-empty values that fall outside
        the top *limit* — present so a bar/pie chart can render a truthful
        "Other" slice instead of silently dropping the tail.
        """
        self.store.init_schema()
        where, parameters = self._build_where(query)
        database = self.store.database
        col_expr = _field_column_expr(
            field_token, parameters, "field_key", field_mappings=query.field_mappings
        )

        # Single scan: the window aggregates run after GROUP BY but before
        # ORDER BY/LIMIT, so every surviving row carries the pre-LIMIT event
        # total and group count (= distinct non-empty values, since the
        # grouping key is the value itself).
        result = self.store.client.query(
            f"""
            SELECT {col_expr} AS val,
                   count() AS c,
                   sum(count()) OVER () AS total,
                   count() OVER () AS n_groups
            FROM {database}.events
            WHERE {where} AND {col_expr} != ''
            GROUP BY val
            ORDER BY c DESC, val ASC
            LIMIT {int(limit)}
            """,
            parameters=parameters,
        )
        rows = result.result_rows
        if not rows:
            return {
                "field": field_token,
                "total": 0,
                "distinct": 0,
                "values": [],
                "other_count": 0,
            }

        total, distinct = rows[0][2], rows[0][3]
        values = [{"value": row[0], "count": row[1]} for row in rows]
        other_count = total - sum(v["count"] for v in values)
        return {
            "field": field_token,
            "total": total,
            "distinct": distinct,
            "values": values,
            "other_count": max(0, other_count),
        }

    # Quantiles reported for every numeric field — chosen to cover both the
    # box-plot five-number summary (0.25/0.5/0.75, whiskers approximated from
    # the data range) and tail behavior an analyst investigating a DoS or
    # outlier burst cares about (0.01/0.05/0.95/0.99).
    _NUMERIC_QUANTILES = (0.01, 0.05, 0.25, 0.5, 0.75, 0.95, 0.99)

    def field_numeric_stats(
        self, query: EventQuery, field_token: str, bins: int = 30
    ) -> dict[str, Any]:
        """Return summary statistics and a fixed-width histogram for a numeric field.

        Values are cast with ``toFloat64OrNull(toString(...))`` since dynamic
        attributes are stored as strings; non-numeric values are silently
        dropped from the cast (become NULL) rather than erroring. ``count ==
        0`` is the signal callers use to fall back to treating the field as
        categorical instead.

        Bins are **fixed-width** (evenly spaced across ``[min, max]``), not
        ClickHouse's adaptive ``histogram()`` function — reproducibility (the
        same filters always produce the same bin edges) matters more here
        than adaptive bin placement.

        The two scans are deliberate: bin edges are a function of the first
        scan's min/max, and the single-scan alternatives (adaptive
        ``histogram()``, or frameless window ``min/max OVER ()`` forcing
        ClickHouse to buffer every row) are worse than a second
        aggregate-only pass.
        """
        self.store.init_schema()
        where, parameters = self._build_where(query)
        database = self.store.database
        col_expr = _field_column_expr(
            field_token, parameters, "field_key", field_mappings=query.field_mappings
        )
        cast_expr = f"toFloat64OrNull(toString({col_expr}))"

        quantile_exprs = ", ".join(f"quantile({q})(v)" for q in self._NUMERIC_QUANTILES)
        stats_result = self.store.client.query(
            f"""
            SELECT count(v) AS n, min(v) AS mn, max(v) AS mx, avg(v) AS mean,
                   stddevPop(v) AS sd, {quantile_exprs}
            FROM (SELECT {cast_expr} AS v FROM {database}.events WHERE {where}) AS t
            WHERE v IS NOT NULL
            """,
            parameters=parameters,
        )
        row = stats_result.result_rows[0] if stats_result.result_rows else None
        count = row[0] if row else 0

        empty: dict[str, Any] = {
            "field": field_token,
            "count": 0,
            "min": None,
            "max": None,
            "mean": None,
            "stddev": None,
            "quantiles": {},
            "bins": [],
        }
        if not count:
            return empty

        mn, mx, mean, sd, *quantile_values = row[1:]
        quantiles = dict(
            zip((str(q) for q in self._NUMERIC_QUANTILES), quantile_values, strict=True)
        )

        bin_count = max(1, int(bins))
        span = mx - mn
        bin_width = span / bin_count if span > 0 else 1.0

        hist_result = self.store.client.query(
            f"""
            SELECT greatest(0, least({bin_count - 1},
                   toInt64(floor((v - {{mn:Float64}}) / {{bw:Float64}})))) AS bin_idx,
                   count() AS c
            FROM (SELECT {cast_expr} AS v FROM {database}.events WHERE {where}) AS t
            WHERE v IS NOT NULL
            GROUP BY bin_idx
            ORDER BY bin_idx
            """,
            parameters={**parameters, "mn": mn, "bw": bin_width},
        )
        counts_by_bin = {row[0]: row[1] for row in hist_result.result_rows}
        bins_out = [
            {
                "x0": mn + i * bin_width,
                "x1": mn + (i + 1) * bin_width,
                "count": counts_by_bin.get(i, 0),
            }
            for i in range(bin_count)
        ]

        return {
            "field": field_token,
            "count": count,
            "min": mn,
            "max": mx,
            "mean": mean,
            "stddev": sd,
            "quantiles": quantiles,
            "bins": bins_out,
        }

    def field_value_timeseries(
        self,
        query: EventQuery,
        field_token: str,
        buckets: int = 60,
        series_limit: int = 12,
    ) -> dict[str, Any]:
        """Return per-value event counts bucketed over time for *field_token*.

        Restricts to the top *series_limit* values by overall count (via
        :py:meth:`field_terms`) so a high-cardinality field doesn't explode
        into hundreds of series — the Visualization page surfaces
        ``field_terms``' ``other_count``/``distinct`` alongside this so the
        analyst knows series were capped. Powers the multi-series line chart
        and the value×time heatmap.
        """
        self.store.init_schema()
        where, parameters = self._build_where(query)
        database = self.store.database

        if query.start is not None and query.end is not None:
            min_ts: datetime | None = ensure_utc(query.start)
            max_ts: datetime | None = ensure_utc(query.end)
        else:
            min_ts, max_ts = query_timestamp_range(self.store.client, database, where, parameters)

        empty: dict[str, Any] = {
            "field": field_token,
            "interval_seconds": 0,
            "min": None,
            "max": None,
            "series": [],
        }
        if min_ts is None or max_ts is None:
            return empty

        terms = self.field_terms(query, field_token, limit=series_limit)
        top_values = [v["value"] for v in terms["values"]]
        if not top_values:
            return {
                **empty,
                "interval_seconds": 0,
                "min": min_ts.isoformat(),
                "max": max_ts.isoformat(),
            }

        interval = bucket_interval_seconds(min_ts, max_ts, buckets)
        col_expr = _field_column_expr(
            field_token, parameters, "field_key", field_mappings=query.field_mappings
        )
        parameters["series_values"] = top_values

        bucket_result = self.store.client.query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {interval} second) AS bucket,
                   {col_expr} AS val,
                   count() AS c
            FROM {database}.events
            WHERE {where} AND timestamp IS NOT NULL
                AND has({{series_values:Array(String)}}, {col_expr})
            GROUP BY bucket, val
            ORDER BY bucket
            """,
            parameters=parameters,
        )

        # Pivot into one bucket-list per value, in the same top-N order as
        # `terms`, filling buckets with zero rows so every series has an
        # entry for every bucket the chart draws.
        by_value: dict[str, dict[str, int]] = {v: {} for v in top_values}
        for bucket_ts, val, count in bucket_result.result_rows:
            by_value.setdefault(val, {})[ensure_utc_iso(bucket_ts)] = count

        # Derive bucket starts from [min_ts, max_ts] rather than from the
        # query result rows: a bucket where *none* of the top-N values had
        # any events produces no GROUP BY row at all, and would otherwise be
        # missing from every series instead of zero-filled.
        all_starts = aligned_bucket_starts(min_ts, max_ts, interval)
        series = [
            {
                "value": value,
                "buckets": [
                    {"start": start, "count": by_value.get(value, {}).get(start, 0)}
                    for start in all_starts
                ],
            }
            for value in top_values
        ]

        return {
            "field": field_token,
            "interval_seconds": interval,
            "min": min_ts.isoformat(),
            "max": max_ts.isoformat(),
            "series": series,
        }

    def _union_timestamp_range(
        self, primary: EventQuery, comparison: EventQuery
    ) -> tuple[datetime | None, datetime | None]:
        """Return the union (min, max) timestamp range across both layers.

        Explicit ``start``/``end`` on the primary win outright — comparison
        layers are constructed to share the primary's time window (see the
        compare endpoint), so an explicit window is already the shared grid.
        Otherwise the union of both layers' data ranges is used, so neither
        layer's buckets get truncated to the other's extent.
        """
        if primary.start is not None and primary.end is not None:
            return ensure_utc(primary.start), ensure_utc(primary.end)
        database = self.store.database
        ranges = []
        for query in (primary, comparison):
            where, parameters = self._build_where(query)
            ranges.append(query_timestamp_range(self.store.client, database, where, parameters))
        mins = [r[0] for r in ranges if r[0] is not None]
        maxs = [r[1] for r in ranges if r[1] is not None]
        if not mins or not maxs:
            return None, None
        return min(mins), max(maxs)

    def _bucketed_counts(self, query: EventQuery, interval: int) -> dict[str, int]:
        """Return epoch-aligned bucket-start (ISO) → event count for *query*."""
        where, parameters = self._build_where(query)
        result = self.store.client.query(
            f"""
            SELECT toStartOfInterval(timestamp, INTERVAL {interval} second) AS bucket,
                   count() AS c
            FROM {self.store.database}.events
            WHERE {where} AND timestamp IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket
            """,
            parameters=parameters,
        )
        return {ensure_utc_iso(row[0]): row[1] for row in result.result_rows}

    def compare_time_histogram(
        self, primary: EventQuery, comparison: EventQuery, buckets: int = 60
    ) -> dict[str, Any]:
        """Return event counts over time for two layers on one shared bucket grid.

        The comparability invariant lives here: one time range (see
        :py:meth:`_union_timestamp_range`), one ``bucket_interval_seconds``,
        one epoch-aligned bucket-start list — both layers are counted against
        that grid and zero-filled onto it, so the two series are comparable
        by construction. The response carries only raw counts; derived
        metrics (delta/rate/ratio/cumulative) are frontend transforms.
        """
        self.store.init_schema()
        min_ts, max_ts = self._union_timestamp_range(primary, comparison)
        if min_ts is None or max_ts is None:
            return {
                "kind": "time",
                "interval_seconds": 0,
                "min": None,
                "max": None,
                "buckets": [],
                "primary_total": 0,
                "comparison_total": 0,
            }

        interval = bucket_interval_seconds(min_ts, max_ts, buckets)
        primary_counts, comparison_counts = self._run_parallel(
            lambda: self._bucketed_counts(primary, interval),
            lambda: self._bucketed_counts(comparison, interval),
        )
        starts = aligned_bucket_starts(min_ts, max_ts, interval)
        bucket_list = [
            {
                "start": start,
                "primary": primary_counts.get(start, 0),
                "comparison": comparison_counts.get(start, 0),
            }
            for start in starts
        ]
        return {
            "kind": "time",
            "interval_seconds": interval,
            "min": min_ts.isoformat(),
            "max": max_ts.isoformat(),
            "buckets": bucket_list,
            "primary_total": sum(primary_counts.values()),
            "comparison_total": sum(comparison_counts.values()),
        }

    def _terms_counts_for_values(
        self, query: EventQuery, field_token: str, values: list[str]
    ) -> tuple[dict[str, int], int]:
        """Return per-value counts restricted to *values*, plus the layer's non-empty total."""
        where, parameters = self._build_where(query)
        col_expr = _field_column_expr(
            field_token, parameters, "field_key", field_mappings=query.field_mappings
        )
        parameters["cmp_values"] = values
        result = self.store.client.query(
            f"""
            SELECT if(has({{cmp_values:Array(String)}}, {col_expr}), {col_expr}, '') AS val,
                   count() AS c
            FROM {self.store.database}.events
            WHERE {where} AND {col_expr} != ''
            GROUP BY val
            """,
            parameters=parameters,
        )
        counts: dict[str, int] = {}
        total = 0
        for val, count in result.result_rows:
            total += count
            if val != "":
                counts[val] = count
        return counts, total

    def compare_field_terms(
        self, primary: EventQuery, comparison: EventQuery, field_token: str, limit: int = 50
    ) -> dict[str, Any]:
        """Return top-N term counts for two layers over one shared category list.

        The primary layer's :py:meth:`field_terms` fixes the top-N value
        list; the comparison layer is then counted against those same values
        (everything else folds into its ``other``), so both bar groups share
        categories — the terms-kind comparability invariant.
        """
        self.store.init_schema()
        terms = self.field_terms(primary, field_token, limit=limit)
        top_values = [v["value"] for v in terms["values"]]
        primary_by_value = {v["value"]: v["count"] for v in terms["values"]}

        comparison_by_value: dict[str, int] = {}
        comparison_total = 0
        if top_values:
            comparison_by_value, comparison_total = self._terms_counts_for_values(
                comparison, field_token, top_values
            )

        values = [
            {
                "value": value,
                "primary": primary_by_value.get(value, 0),
                "comparison": comparison_by_value.get(value, 0),
            }
            for value in top_values
        ]
        return {
            "kind": "terms",
            "field": field_token,
            "values": values,
            "distinct": terms["distinct"],
            "primary_total": terms["total"],
            "comparison_total": comparison_total,
            "primary_other": terms["other_count"],
            "comparison_other": max(0, comparison_total - sum(v["comparison"] for v in values)),
        }

    def _numeric_layer_stats(
        self, query: EventQuery, field_token: str
    ) -> tuple[int, float | None, float | None]:
        """Return (count, min, max) of the numeric cast of *field_token* for one layer."""
        where, parameters = self._build_where(query)
        col_expr = _field_column_expr(
            field_token, parameters, "field_key", field_mappings=query.field_mappings
        )
        cast_expr = f"toFloat64OrNull(toString({col_expr}))"
        result = self.store.client.query(
            f"""
            SELECT count(v), min(v), max(v)
            FROM (SELECT {cast_expr} AS v FROM {self.store.database}.events WHERE {where}) AS t
            WHERE v IS NOT NULL
            """,
            parameters=parameters,
        )
        row = result.result_rows[0] if result.result_rows else (0, None, None)
        return row[0] or 0, row[1], row[2]

    def _numeric_bin_counts(
        self, query: EventQuery, field_token: str, mn: float, bin_width: float, bin_count: int
    ) -> dict[int, int]:
        """Return bin-index → count for one layer, bucketed on explicit shared edges."""
        where, parameters = self._build_where(query)
        col_expr = _field_column_expr(
            field_token, parameters, "field_key", field_mappings=query.field_mappings
        )
        cast_expr = f"toFloat64OrNull(toString({col_expr}))"
        result = self.store.client.query(
            f"""
            SELECT greatest(0, least({bin_count - 1},
                       toInt64(floor((v - {{mn:Float64}}) / {{bw:Float64}})))) AS bin_idx,
                   count() AS c
            FROM (SELECT {cast_expr} AS v FROM {self.store.database}.events WHERE {where}) AS t
            WHERE v IS NOT NULL
            GROUP BY bin_idx
            ORDER BY bin_idx
            """,
            parameters={**parameters, "mn": mn, "bw": bin_width},
        )
        return {row[0]: row[1] for row in result.result_rows}

    def compare_field_numeric(
        self, primary: EventQuery, comparison: EventQuery, field_token: str, bins: int = 30
    ) -> dict[str, Any]:
        """Return fixed-width numeric histograms for two layers on shared bin edges.

        Bin edges are derived from the **union** min/max of both layers, then
        both layers are bucketed on those explicit edges — the numeric-kind
        comparability invariant. Same fixed-width/reproducible-edges policy
        as :py:meth:`field_numeric_stats`.
        """
        self.store.init_schema()
        (p_count, p_mn, p_mx), (c_count, c_mn, c_mx) = self._run_parallel(
            lambda: self._numeric_layer_stats(primary, field_token),
            lambda: self._numeric_layer_stats(comparison, field_token),
        )

        mins = [m for m in (p_mn, c_mn) if m is not None]
        maxs = [m for m in (p_mx, c_mx) if m is not None]
        if not mins or not maxs:
            return {
                "kind": "numeric",
                "field": field_token,
                "min": None,
                "max": None,
                "bins": [],
                "primary_total": 0,
                "comparison_total": 0,
            }
        mn, mx = min(mins), max(maxs)

        bin_count = max(1, int(bins))
        span = mx - mn
        bin_width = span / bin_count if span > 0 else 1.0

        primary_bins, comparison_bins = self._run_parallel(
            lambda: (
                self._numeric_bin_counts(primary, field_token, mn, bin_width, bin_count)
                if p_count
                else {}
            ),
            lambda: (
                self._numeric_bin_counts(comparison, field_token, mn, bin_width, bin_count)
                if c_count
                else {}
            ),
        )
        bins_out = [
            {
                "x0": mn + i * bin_width,
                "x1": mn + (i + 1) * bin_width,
                "primary": primary_bins.get(i, 0),
                "comparison": comparison_bins.get(i, 0),
            }
            for i in range(bin_count)
        ]
        return {
            "kind": "numeric",
            "field": field_token,
            "min": mn,
            "max": mx,
            "bins": bins_out,
            "primary_total": p_count,
            "comparison_total": c_count,
        }
