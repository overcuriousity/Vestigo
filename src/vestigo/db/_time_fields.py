"""Virtual `time:` fields — time parts addressable as ordinary field tokens.

Every chart and filter in Vestigo names the thing it groups by with a *field
token* (`artifact`, `attr:src_ip`, a mapped canonical name). Time parts were
the one exception: the only way to group by an hour or a weekday was
``EventQueryService.time_punchcard``, which hardwires day-of-week × hour-of-day
and takes no field at all. "Which country attacks during which hours?" was
therefore unaskable — not because the punch card is inflexible, but because an
hour could not be *named*.

This module makes time parts tokens. ``time:hour_of_day`` resolves in
:func:`vestigo.db.queries._field_column_expr` exactly like any other token, so
one definition reaches every aggregation (terms, pivot/sankey, timeseries,
scatter) *and* the events filter builder — a field×hour heatmap and a
"weekends only" filter fall out of the same table.

Three properties are deliberate and load-bearing:

* **UTC-explicit.** ``toHour`` et al. read a ``DateTime64`` in the *server's*
  timezone unless told otherwise, which would silently reshape every temporal
  chart when a deployment's TZ changes. Same convention (and same reasoning)
  as ``time_punchcard``; ``toDayOfWeek``'s mode ``0`` is ISO (Mon=1 … Sun=7),
  matching it exactly so the punch card and a ``time:day_of_week`` chart can
  never disagree about what "day 1" means.
* **Zero-padded values.** Values are strings (every field token resolves to a
  String expression), so ``'9'`` would sort after ``'10'``. Padding to a fixed
  width makes lexical order equal chronological order, which is what lets the
  existing ``sort: "value"`` chart option order an hour axis with no special
  case anywhere downstream.
* **Sentinel rows collapse to ``''``.** Undated events carry the year-2299
  sentinel; a time expression over one yields a real-looking hour. Every
  caller already drops empty field values (``col != ''``), so emitting ``''``
  for sentinel rows reuses that guard instead of needing a new one — without
  it every temporal chart grows a phantom bucket of undated events.

The offset correction is threaded in by the caller (:meth:`TimeFieldSpec.sql`
takes the already-built effective-timestamp expression) so a source with a declared
clock skew buckets by its *corrected* hour, matching how W2 treats every other
time-derived query.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from vestigo.db._dt import VESTIGO_NOT_SENTINEL_SQL

#: Reserved token namespace. Field mappings may not define canonical names
#: under this prefix (see ``validate_field_mappings``) — resolution order in
#: ``_field_column_expr`` puts time fields first, so a colliding mapping would
#: be silently unreachable rather than merely ambiguous.
TIME_FIELD_PREFIX = "time:"

_MONTH_LABELS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)
_DOW_LABELS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


@dataclass(frozen=True)
class TimeFieldSpec:
    """One virtual time field: how to compute it, and what values it can take.

    ``domain`` is the full natural-order value list for a *cyclical* part
    (every hour exists, whether or not the data covers it), or ``None`` for an
    unbounded absolute part like a calendar date. Callers use its presence to
    decide between "render the whole axis in order" and "rank the top N by
    count" — see ``EventQueryService.field_pivot``.

    ``display`` maps a canonical value to a human label where the raw value is
    opaque (``'1'`` → ``'Mon'``). It is presentation-only: the canonical value
    is what lands in chart configs, URLs, saved charts and filters, so a label
    change never invalidates a stored chart.
    """

    label: str
    scale: str
    expr: Callable[[str], str]
    domain: tuple[str, ...] | None = None
    display: dict[str, str] | None = None

    def sql(self, ts_expr: str) -> str:
        """SQL for this time part over *ts_expr*, blanking sentinel rows."""
        return f"if({VESTIGO_NOT_SENTINEL_SQL}, {self.expr(ts_expr)}, '')"


def _padded(fn: str, width: int) -> Callable[[str], str]:
    """A UTC-explicit ClickHouse date part, left-padded to *width* digits."""
    return lambda ts: f"leftPad(toString({fn}({ts}, 'UTC')), {width}, '0')"


TIME_FIELD_SPECS: dict[str, TimeFieldSpec] = {
    "time:hour_of_day": TimeFieldSpec(
        label="Hour of day (UTC)",
        scale="ordinal",
        expr=_padded("toHour", 2),
        domain=tuple(f"{h:02d}" for h in range(24)),
        display={f"{h:02d}": f"{h:02d}:00" for h in range(24)},
    ),
    "time:day_of_week": TimeFieldSpec(
        label="Day of week (UTC)",
        scale="ordinal",
        # Mode 0 is ISO (Mon=1 … Sun=7) — the convention time_punchcard uses.
        expr=lambda ts: f"toString(toDayOfWeek({ts}, 0, 'UTC'))",
        domain=tuple(str(d) for d in range(1, 8)),
        display=dict(zip((str(d) for d in range(1, 8)), _DOW_LABELS, strict=True)),
    ),
    "time:day_of_month": TimeFieldSpec(
        label="Day of month (UTC)",
        scale="ordinal",
        expr=_padded("toDayOfMonth", 2),
        domain=tuple(f"{d:02d}" for d in range(1, 32)),
    ),
    "time:week_of_year": TimeFieldSpec(
        label="ISO week of year (UTC)",
        scale="ordinal",
        expr=_padded("toISOWeek", 2),
        domain=tuple(f"{w:02d}" for w in range(1, 54)),
    ),
    "time:month": TimeFieldSpec(
        label="Month (UTC)",
        scale="ordinal",
        expr=_padded("toMonth", 2),
        domain=tuple(f"{m:02d}" for m in range(1, 13)),
        display=dict(zip((f"{m:02d}" for m in range(1, 13)), _MONTH_LABELS, strict=True)),
    ),
    # Absolute, unbounded parts: no domain, so callers fall back to top-N by
    # count exactly as they do for a real high-cardinality field.
    "time:date": TimeFieldSpec(
        label="Date (UTC)",
        scale="interval",
        expr=lambda ts: f"toString(toDate({ts}, 'UTC'))",
    ),
    "time:year_month": TimeFieldSpec(
        label="Year-month (UTC)",
        scale="interval",
        expr=lambda ts: f"formatDateTime({ts}, '%Y-%m', 'UTC')",
    ),
}


def resolve_time_field(token: str) -> TimeFieldSpec | None:
    """Return the spec for *token* if it names a virtual time field.

    Matching is whitespace/case-insensitive, like ``resolve_column_token``.
    An ``attr:`` prefix always means "attribute", so it never resolves here —
    the same escape hatch mappings honour.
    """
    if token.startswith("attr:"):
        return None
    return TIME_FIELD_SPECS.get(token.strip().lower())


def is_time_field(token: str) -> bool:
    """True when *token* names a virtual time field."""
    return resolve_time_field(token) is not None
