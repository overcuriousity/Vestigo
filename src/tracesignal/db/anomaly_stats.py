"""ClickHouse-backed statistical anomaly detection.

Two AMiner-inspired detectors operate directly on parsed event fields stored in
ClickHouse.  No embeddings or Qdrant are required — both detectors run against
already-ingested data.

**value_novelty** (``detector="value_novelty"``)
    Score rare or first-seen field values by surprise: ``-log(count/total)``.

    Two modes:

    * *self-baseline* — the full timeline is its own reference.  Values
      appearing ≤ ``rarity_floor`` times are flagged.  Works immediately after
      ingestion; no baseline window required.
    * *temporal* — analyst supplies a ``baseline_end`` timestamp.  Values absent
      from the baseline window but present in the detect window are flagged as
      "first seen after incident start."  ``rarity_floor`` is ignored.

**frequency** (``detector="frequency"``)
    Detect event-count spikes and silences in time windows per ``series_field``
    value using z-score.  Buckets are computed with the same
    ``duration / bucket_count`` interval formula as the histogram endpoint, but
    over the *unfiltered* timeline for the given ``case_id``/``source_ids`` —
    it does not honor the events-view's ``q``/``artifact``/``tag``/time-range
    filters the way ``QueryService.histogram`` does, so anomaly windows can be
    computed over a different span than what a filtered histogram displays.
    Score = |z|; windows above ``z_threshold`` are returned.

    Two sub-modes mirror value_novelty:

    * *z-score* — baseline = all buckets in the timeline; flag any window
      beyond ``z_threshold`` standard deviations from the series mean.
    * *temporal-z-score* — baseline = buckets before ``baseline_end``; detect =
      buckets after.  Mean/std computed from the baseline window only.

**value_combo** (``detector="value_combo"``)
    The multi-field extension of ``value_novelty`` (AMiner
    ``NewMatchPathValueComboDetector``): instead of scoring a single field's
    values, score *combinations* of two or more fields (``GROUP BY`` over the
    field expressions). A combination rare or absent in the baseline is
    flagged even when each field's individual values are common — an
    ``(action, hour)`` pair like ``(login_ok, 03:00)`` can be novel while
    ``login_ok`` and ``03:00`` are each unremarkable. Same two modes and same
    surprise score as ``value_novelty``; auto mode picks a single tuple from
    the two highest-coverage recommended fields (no pair enumeration).

**numeric_range** (``detector="numeric_range"``)
    For fields whose values parse as numbers (syntactic type detection via
    ``toFloat64OrNull`` — never by field meaning), learn a baseline band and
    flag detect-window values outside it. AMiner ``ValueRangeDetector``. Two
    modes: *self-baseline* (``method="iqr"``) uses a Tukey fence
    ``[q1 − 1.5·IQR, q3 + 1.5·IQR]`` over the whole corpus — an exact min/max
    over the corpus flags nothing by construction; *temporal*
    (``method="temporal-range"``) learns exact min/max from the baseline
    window and flags detect-window values outside it. Findings group by
    distinct violating value; score = distance outside the band ÷ band width.

**timestamp_order** (``detector="timestamp_order"``)
    Flag events whose parsed timestamp jumps *backwards* relative to the
    previous record in the source file (record order = ``byte_offset``, then
    ``line_number``).  Log-tampering / clock-manipulation indicator, adapted
    from AMiner's ``TimestampsUnsortedDetector``.  Mode-less
    (``method="sequential"``): there is no baseline/detect split — the
    violation is purely positional.  Each event is compared to its immediate
    predecessor (``lagInFrame``), not to a running maximum, so a single
    future-dated outlier flags two boundaries instead of cascading over every
    later event.  ``min_skew_seconds`` suppresses sub-second logger jitter.
    Score = backwards jump in seconds.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from tracesignal.db._buckets import bucket_interval_seconds, query_timestamp_range
from tracesignal.db._columns import resolve_column_token
from tracesignal.db._dt import (
    TS_NOT_SENTINEL_SQL,
    ensure_utc,
    is_null_ts_sentinel,
    to_clickhouse_utc,
)
from tracesignal.db.clickhouse import ClickHouseStore
from tracesignal.db.field_mappings import mapping_coalesce_expr, resolve_mapping

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Default fields scanned by value_novelty when no list is supplied (fallback only).
_DEFAULT_NOVELTY_FIELDS = ["artifact", "timestamp_desc", "display_name"]

# Top-level columns considered by the field recommender (excludes free-text
# and identifier-like columns that are not useful for novelty detection).
_NOVELTY_CANDIDATE_TOP_LEVEL = [
    "artifact",
    "timestamp_desc",
    "display_name",
    "parser_name",
]

# Maximum number of attribute keys the recommender will evaluate.
_RECOMMENDER_MAX_ATTR_KEYS = 50

# Cap on how many auto-selected fields find_value_novelty scans per call.
# Each field is a separate sequential full-partition-scan query (no batching
# across fields today), so an uncapped recommended set (up to ~54 fields —
# _NOVELTY_CANDIDATE_TOP_LEVEL plus _RECOMMENDER_MAX_ATTR_KEYS) could turn
# one panel-open into dozens of serial ClickHouse round-trips. Capped to the
# highest-coverage recommended fields, since recommend_novelty_fields already
# sorts by (recommended, -coverage).
_MAX_AUTO_SCAN_FIELDS = 15

# Minimum baseline numeric samples before the range detector trusts a field's
# learned band — below this, the min/max or quartiles are too noisy to score.
_MIN_RANGE_BASELINE = 20

# Fraction of a field's non-empty values that must parse as numbers for it to
# be offered as a range-detector candidate (syntactic type detection only).
_MIN_NUMERIC_RATIO = 0.9

# Minimum buckets in a frequency series for z-scoring to be meaningful.
_MIN_FREQUENCY_BUCKETS = 3

# Floor applied to the leave-one-out std in self-baseline mode so that a
# near-constant rest-of-series doesn't divide by ~0 when scoring the
# excluded point (half an event-count unit — small enough to still flag any
# real deviation, large enough to avoid blowing up the z-score to inf/NaN).
_MIN_FREQUENCY_STD = 0.5

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class ValueFinding:
    """One rare/novel value returned by the value-novelty detector."""

    field: str
    value: str
    count: int
    # -log(count / total_events); higher = rarer.
    score: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class FreqFinding:
    """One anomalous time window returned by the frequency detector."""

    series_field: str
    series_value: str
    window_start: str
    window_end: str
    observed: int
    expected: float
    z_score: float
    # |z_score|; used for ranking.
    score: float
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class RangeFinding:
    """One out-of-range numeric value from the numeric-range detector."""

    field: str
    value: float
    count: int
    # excess distance beyond the band, normalized by band width.
    score: float
    direction: str  # "below" | "above"
    lower: float
    upper: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class NumericFieldInfo:
    """A numeric-parseable field candidate for the range detector."""

    token: str
    distinct: int
    coverage: float
    # fraction of non-empty values that parse as a number (0–1).
    numeric_ratio: float
    recommended: bool


@dataclass
class ComboFinding:
    """One rare/novel field *combination* from the value-combo detector."""

    fields: list[str]
    values: list[str]
    count: int
    # -log(count / total_events); higher = rarer.
    score: float
    first_seen: str | None
    event_id: str | None
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class OrderFinding:
    """One out-of-order timestamp returned by the timestamp-order detector."""

    source_id: str
    event_id: str
    # Violating event's timestamp (ISO, UTC).
    timestamp: str
    # Previous record's timestamp in file/record order (ISO, UTC).
    prev_timestamp: str
    # prev_timestamp - timestamp, in seconds (always > 0 for a violation).
    skew_seconds: float
    byte_offset: int
    line_number: int
    # = skew_seconds; used for ranking.
    score: float
    event: dict[str, Any] | None
    details: dict[str, Any]


@dataclass
class StatAnomalyResult:
    """Return value of statistical anomaly detection."""

    status: str  # "ok" | "no_data" | "insufficient_data"
    # "value_novelty" | "value_combo" | "frequency" | "timestamp_order" | "numeric_range"
    detector: str
    # "self-baseline" | "temporal" | "z-score" | "temporal-z-score" | "sequential"
    #  | "iqr" | "temporal-range"
    method: str
    baseline_size: int  # total events (value_novelty) or event-count used for z-score
    results: list[ValueFinding | FreqFinding | OrderFinding | ComboFinding | RangeFinding] = field(
        default_factory=list
    )
    # Effective |z| cutoff used by the frequency detector; None for value_novelty.
    z_threshold: float | None = None


@dataclass
class NoveltyFieldInfo:
    """Field recommendation produced by :meth:`recommend_novelty_fields`.

    Used by the API to populate the field picker and frequency GROUP BY dropdown,
    and internally as the smart default for :meth:`find_value_novelty`.
    """

    token: str  # e.g. "artifact", "attr:status_code"
    distinct: int  # uniqExact() count
    coverage: float  # fraction of events with a non-empty value (0–1)
    kind: str  # "constant" | "identifier" | "categorical" | "sparse"
    recommended: bool  # True → include in default scan


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _col_expr(
    field_token: str,
    params: dict[str, Any],
    field_mappings: dict[str, list[str]] | None = None,
    prefix: str = "fk",
) -> str:
    """Return a ClickHouse SQL expression for a field token.

    Top-level columns (``"artifact"``, ``"display_name"``, …) are returned as-is,
    case/whitespace-insensitively, sharing the same allowlist `queries.py`
    uses for the events-view filter — a field like `parser_version` must
    resolve to the real column in both places, or a detector can silently
    score against an always-empty attribute lookup instead of the values the
    events view shows for that field. Attribute keys prefixed with
    ``"attr:"`` (``"attr:user_agent"``) or any other non-top-level token are
    returned as ``attributes[{prefix:String}]`` with the key injected into
    *params*. Single-field call sites use a fresh *params* dict per token, so
    the default ``"fk"`` name is safe; multi-field queries (value_combo) pass
    a distinct *prefix* per field (``fk0``, ``fk1``, …) to share one dict.

    ``field_mappings`` (issue #10): a token naming a canonical mapped field
    resolves to a coalesce over its raw attribute keys (parameter names
    ``{prefix}_m0..{prefix}_mN`` — same params-dict-uniqueness assumption).
    """
    mapped_raws = resolve_mapping(field_token, field_mappings)
    if mapped_raws:
        return mapping_coalesce_expr(mapped_raws, params, prefix)
    column, attr_key = resolve_column_token(field_token)
    if column is not None:
        return column
    params[prefix] = attr_key
    return f"attributes[{{{prefix}:String}}]"


def _freq_finding(
    series_field: str,
    series_value: Any,
    bucket_dt: datetime,
    interval: int,
    cnt: int,
    mean_val: float,
    z: float,
    method: str,
) -> FreqFinding:
    """Build a `FreqFinding` for one anomalous bucket."""
    window_end_dt = bucket_dt + timedelta(seconds=interval)
    return FreqFinding(
        series_field=series_field,
        series_value=str(series_value),
        window_start=bucket_dt.isoformat(),
        window_end=window_end_dt.isoformat(),
        observed=cnt,
        expected=round(mean_val, 2),
        z_score=round(z, 4),
        score=round(abs(z), 4),
        event_id=None,
        event=None,
        details={
            "detector": "frequency",
            "method": method,
            "series_field": series_field,
            "series_value": str(series_value),
            "window_start": bucket_dt.isoformat(),
            "window_end": window_end_dt.isoformat(),
            "observed": cnt,
            "expected": round(mean_val, 2),
            "z_score": round(z, 4),
            "interval_seconds": interval,
        },
    )


# Shared guardrails for every whole-corpus detector scan (GROUP BY over up to
# hundreds of millions of rows): spill large aggregation states to disk
# instead of ballooning RAM, cap the query's memory hard (fail one query, not
# the server), and bound thread fan-out so several concurrent detector scans
# don't oversubscribe the box. Values match the field-inventory scan that
# first needed them.
_HEAVY_SCAN_SETTINGS = (
    "SETTINGS max_threads = 8, "
    "max_bytes_before_external_group_by = 4000000000, "
    "max_memory_usage = 12000000000"
)


def _stub_event(evt_id: str | None, case_id: str, first_seen: str | None) -> dict[str, Any] | None:
    """Minimal full-shape event stub for a finding's representative event.

    The scan queries only aggregate ``argMin(event_id, ...)`` — fat columns
    (message, attributes) are deliberately not read there. Findings that
    survive ranking get their stub replaced by a fully hydrated event via
    ``StatisticalAnomalyService._hydrate_finding_events``; this stub is the
    fallback shape when hydration misses (e.g. a concurrent source delete).
    """
    if not evt_id:
        return None
    return {
        "event_id": evt_id,
        "case_id": case_id,
        "source_id": "",
        "message": "",
        "timestamp": first_seen,
        "timestamp_desc": None,
        "artifact": None,
        "artifact_long": None,
        "display_name": None,
        "tags": [],
        "attributes": {},
        "content_hash": "",
        "file_hash": "",
        "parser_name": "",
        "parser_version": "",
        "source_file": "",
        "byte_offset": None,
        "line_number": None,
        "embedding_model": None,
        "embedding_config_hash": None,
        "ingest_time": None,
    }


def _present_ts(value: Any) -> str | None:
    """Serialize a first-seen/anchor timestamp for API payloads.

    Maps falsy values and the no-timestamp storage sentinel (see
    `db/_dt.py`) to ``None`` so findings never surface the fake 2299 date —
    a group whose representative rows are all undated has no meaningful
    first-seen time.
    """
    if not value or is_null_ts_sentinel(value):
        return None
    return ensure_utc(value).isoformat()


# ---------------------------------------------------------------------------
# Field classification helper
# ---------------------------------------------------------------------------


def _classify_field(distinct: int, non_empty_count: int, total: int = 0) -> tuple[str, bool]:
    """Return (kind, recommended) for a field based on cardinality metrics.

    Parameters
    ----------
    distinct:
        ``uniqExact()`` — number of distinct non-empty values.
    non_empty_count:
        ``countIf(col != '')`` — events with a non-empty value.
    total:
        Total event count (used for sparse threshold).  When 0, sparse check is
        skipped.

    Classification rules (no content assumptions — purely numeric):

    * ``constant``   : distinct ≤ 1                           → not recommended
    * ``sparse``     : non_empty / total < 5 %                → not recommended
    * ``identifier`` : distinct / non_empty_count ≥ 0.9       → not recommended
      (hashes, UUIDs, free-text where nearly every value is unique)
    * ``categorical``: otherwise                               → recommended
    """
    if distinct <= 1:
        return "constant", False
    if total > 0 and non_empty_count / total < 0.05:
        return "sparse", False
    if non_empty_count > 0 and distinct / non_empty_count >= 0.9:
        return "identifier", False
    return "categorical", True


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


class StatisticalAnomalyService:
    """Statistical anomaly detection over ClickHouse event fields.

    Operates purely on ingested events — no embeddings required.
    """

    def __init__(self, clickhouse: ClickHouseStore | None = None) -> None:
        self.ch = clickhouse or ClickHouseStore()

    # ------------------------------------------------------------------
    # Field recommendation
    # ------------------------------------------------------------------

    def _count_events(self, case_id: str, source_ids: list[str]) -> int:
        """Return the total event count for a case/source scope."""
        self.ch.init_schema()
        db = self.ch.database
        total_res = self.ch.client.query(
            f"SELECT count() FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)",
            parameters={"cid": case_id, "src": source_ids},
        )
        return int(total_res.result_rows[0][0]) if total_res.result_rows else 0

    def _hydrate_finding_events(
        self,
        case_id: str,
        source_ids: list[str],
        findings: list[Any],
    ) -> None:
        """Replace findings' stub events with fully hydrated rows, in one batch.

        Called on the final (post-suppression, post-limit) finding slice, so
        at most ``limit`` events are fetched — the detector scans themselves
        only aggregate ``argMin(event_id, ...)`` and never read the fat
        message/attributes columns. Findings whose id is missing from the
        result keep their :func:`_stub_event` fallback.
        """
        ids = [f.event_id for f in findings if f.event_id]
        if not ids:
            return
        by_id = self.ch.get_events_by_ids(case_id, source_ids, ids)
        for f in findings:
            if f.event_id and f.event_id in by_id:
                f.event = by_id[f.event_id]

    def field_inventory(
        self,
        case_id: str,
        source_ids: list[str],
        total: int | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> tuple[list[tuple[str, int, int]], int]:
        """Return ``((token, distinct, non_empty_count), ...), total`` for candidate fields.

        The raw, unclassified field enumeration shared by
        :py:meth:`recommend_novelty_fields` and the Visualization page's
        field picker: a curated list of categorical top-level columns
        (``artifact``, ``timestamp_desc``, ``display_name``, ``parser_name``)
        plus every attribute key found in the events table (as ``attr:<key>``
        tokens). Order: top-level columns in candidate order, then attribute
        keys by non-empty count descending — callers apply their own ranking.

        *total*, the event count callers use as the coverage denominator, is
        queried internally when omitted — pass it when the caller already has
        it to avoid a redundant identical round-trip.
        """
        self.ch.init_schema()
        db = self.ch.database
        params: dict[str, Any] = {"cid": case_id, "src": source_ids}

        if total is None:
            total = self._count_events(case_id, source_ids)
        if total == 0:
            return [], 0

        inventory: list[tuple[str, int, int]] = []

        # -- Top-level columns (batched in a single aggregation) ---------------
        agg_parts = []
        for col in _NOVELTY_CANDIDATE_TOP_LEVEL:
            agg_parts.append(f"uniqExact({col}) AS {col}_dist, countIf({col} != '') AS {col}_cov")
        top_sql = (
            f"SELECT {', '.join(agg_parts)}"
            f" FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" {_HEAVY_SCAN_SETTINGS}"
        )
        top_res = self.ch.client.query(top_sql, parameters=params)
        if top_res.result_rows:
            row = top_res.result_rows[0]
            for i, col in enumerate(_NOVELTY_CANDIDATE_TOP_LEVEL):
                inventory.append((col, int(row[i * 2]), int(row[i * 2 + 1])))

        # -- Attribute keys (ARRAY JOIN to enumerate + aggregate in one pass) --
        #
        # Memory-safety is deliberate here: the paired keys/values ARRAY JOIN
        # avoids re-materializing the whole map per expanded row (the former
        # ``attributes[key]`` lookup kept the full map column alive in every
        # expanded row and OOM-killed the server on wide sources), the
        # ``val != ''`` pre-filter shrinks the expansion before GROUP BY
        # (empties count as absent everywhere in novelty scoring anyway), and
        # ``uniq`` (approximate, ~1% error) replaces ``uniqExact`` — the
        # cardinality classification thresholds don't need exactness, and
        # exact per-key hash sets over near-unique values are the other
        # memory blowup. _HEAVY_SCAN_SETTINGS (external GROUP BY spill + a
        # query memory cap) bounds the worst case instead of trusting the
        # server-wide limit.
        attr_sql = f"""
            SELECT
                key,
                uniq(val)  AS dist,
                count()    AS cov_count
            FROM {db}.events
            ARRAY JOIN mapKeys(attributes) AS key, mapValues(attributes) AS val
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND val != ''
            GROUP BY key
            ORDER BY cov_count DESC
            LIMIT {{max_keys:UInt32}}
            {_HEAVY_SCAN_SETTINGS}
        """
        attr_res = self.ch.client.query(
            attr_sql, parameters={**params, "max_keys": _RECOMMENDER_MAX_ATTR_KEYS}
        )
        mapped_raws = {r for raws in (field_mappings or {}).values() for r in raws}
        for key, dist, cov_count in attr_res.result_rows:
            if key in mapped_raws:
                continue  # replaced by the canonical entry below
            inventory.append((f"attr:{key}", int(dist), int(cov_count)))

        if field_mappings:
            inventory.extend(self.canonical_inventory(case_id, source_ids, field_mappings))

        return inventory, total

    def canonical_inventory(
        self,
        case_id: str,
        source_ids: list[str],
        field_mappings: dict[str, list[str]],
    ) -> list[tuple[str, int, int]]:
        """Exact ``(canonical, distinct, coverage)`` aggregates for mapped fields.

        Canonical mapped fields (issue #10) aggregate over the coalesce
        expression, all canonicals batched into one round-trip — summing the
        per-raw-key numbers would double-count events that carry several of
        the raw keys. This is the one inventory piece that stays a live query
        even when the rest comes from the per-source stats cache
        (``db/field_stats.py``): per-source counts cannot dedupe those events.
        """
        self.ch.init_schema()
        db = self.ch.database
        m_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        m_parts = []
        canonicals = list(field_mappings.items())
        for i, (_, raws) in enumerate(canonicals):
            expr = mapping_coalesce_expr(raws, m_params, f"inv{i}")
            m_parts.append(f"uniqExact({expr}) AS d{i}, countIf({expr} != '') AS c{i}")
        m_sql = (
            f"SELECT {', '.join(m_parts)}"
            f" FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" {_HEAVY_SCAN_SETTINGS}"
        )
        m_res = self.ch.client.query(m_sql, parameters=m_params)
        out: list[tuple[str, int, int]] = []
        if m_res.result_rows:
            row = m_res.result_rows[0]
            for i, (canonical, _) in enumerate(canonicals):
                out.append((canonical, int(row[i * 2]), int(row[i * 2 + 1])))
        return out

    def recommend_novelty_fields(
        self,
        case_id: str,
        source_ids: list[str],
        total: int | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
    ) -> list[NoveltyFieldInfo]:
        """Return a ranked, annotated list of candidate fields for value_novelty.

        Fields are classified by cardinality *without* any content heuristics so
        the result is valid for any timeseries type (nginx, Windows events, …):

        * ``constant``   — distinct ≤ 1 value; no signal.
        * ``identifier`` — near-unique (distinct / non-empty ≥ 0.9); hashes, IDs,
          free-text messages.  Not useful for grouping.
        * ``sparse``     — non-empty coverage < 5 % of events.  Low signal.
        * ``categorical``— recommended; moderate cardinality with decent coverage.

        The candidate set comes from :py:meth:`field_inventory`; this method
        only layers the novelty classification and ranking on top.

        *total*, the event count used as the coverage denominator, is queried
        internally when omitted — pass it when the caller already has it
        (e.g. ``find_value_novelty``'s auto-field-selection path) to avoid a
        redundant identical round-trip.

        *inventory* lets callers supply a pre-merged candidate list (the
        per-source stats cache, ``db/field_stats.py``) instead of the live
        :py:meth:`field_inventory` scan; *total* must be passed with it.
        """
        if inventory is None:
            inventory, total = self.field_inventory(case_id, source_ids, total, field_mappings)
        elif total is None:
            raise ValueError("total is required when inventory is supplied")
        if total == 0:
            return []

        findings: list[NoveltyFieldInfo] = []
        for token, dist, cov_count in inventory:
            kind, recommended = _classify_field(dist, cov_count, total)
            findings.append(
                NoveltyFieldInfo(
                    token=token,
                    distinct=dist,
                    coverage=round(cov_count / total, 4),
                    kind=kind,
                    recommended=recommended,
                )
            )

        # Sort: recommended first, then by coverage descending.
        findings.sort(key=lambda f: (not f.recommended, -f.coverage))
        return findings

    # ------------------------------------------------------------------
    # Timeline range helper
    # ------------------------------------------------------------------

    def get_timeline_midpoint(
        self,
        case_id: str,
        source_ids: list[str],
    ) -> datetime | None:
        """Return the midpoint timestamp of the timeline, or None if no events."""
        self.ch.init_schema()
        db = self.ch.database
        params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        min_dt, max_dt = query_timestamp_range(
            self.ch.client,
            db,
            "case_id = {cid:String} AND has({src:Array(String)}, source_id)",
            params,
        )
        if min_dt is None or max_dt is None:
            return None
        return min_dt + (max_dt - min_dt) / 2

    # ------------------------------------------------------------------
    # Value / combo novelty
    # ------------------------------------------------------------------

    def find_value_novelty(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        rarity_floor: int = 3,
        baseline_end: datetime | None = None,
        per_field_limit: int = 25,
        exclude_event_ids: set[str] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
        inventory_total: int | None = None,
    ) -> StatAnomalyResult:
        """Return rare or first-seen values per field, ranked by surprise score.

        *fields* is a list of field tokens (``"artifact"``, ``"timestamp_desc"``,
        ``"attr:user_agent"``).  When ``None``, the cardinality-based recommender
        selects useful attribute fields automatically (falls back to
        ``_DEFAULT_NOVELTY_FIELDS`` only if recommendation yields nothing).

        *inventory* / *inventory_total* let async callers pass a pre-merged
        field candidate list from the per-source stats cache
        (``db/field_stats.py::merged_inventory``) so the ``fields is None``
        path skips the live :py:meth:`field_inventory` scan — the map-scanning
        query family that can be very expensive on wide sources. The cache
        approximates merged ``distinct`` as max-across-sources, which is fine
        for the recommender's coarse cardinality classification. Ignored when
        *fields* is given.

        In *self-baseline* mode values appearing ≤ *rarity_floor* times are
        flagged.  In *temporal* mode (``baseline_end`` provided) any value absent
        from the baseline window but present in the detect window is flagged.
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "self-baseline" if baseline_end is None else "temporal"

        # Total event count for surprise score denominator.
        total_events = self._count_events(case_id, source_ids)

        if fields is not None:
            scan_fields = fields
        else:
            # Auto-discover useful fields for this specific timeseries. Pass
            # the total we already have — recommend_novelty_fields would
            # otherwise re-run the exact same count() query. A supplied cache
            # inventory keeps its own total: coverage counts in it were
            # computed against the cache's event totals, not _count_events.
            rec = self.recommend_novelty_fields(
                case_id,
                source_ids,
                total=inventory_total if inventory is not None else total_events,
                field_mappings=field_mappings,
                inventory=inventory,
            )
            scan_fields = [f.token for f in rec if f.recommended] or _DEFAULT_NOVELTY_FIELDS
            # Each field below is a separate sequential ClickHouse round-trip
            # (no cross-field batching); cap how many an auto-selected set can
            # trigger per call. recommend_novelty_fields already sorts
            # recommended fields by coverage descending, so this keeps the
            # most useful ones.
            scan_fields = scan_fields[:_MAX_AUTO_SCAN_FIELDS]

        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="value_novelty",
                method=method,
                baseline_size=0,
            )

        # Baseline size (temporal only).
        baseline_size = total_events
        if baseline_end is not None:
            bl_res = self.ch.client.query(
                f"SELECT count() FROM {db}.events"
                f" WHERE case_id = {{cid:String}}"
                f" AND has({{src:Array(String)}}, source_id)"
                f" AND timestamp < {{bl:String}}",
                parameters={**base_params, "bl": to_clickhouse_utc(baseline_end)},
            )
            baseline_size = int(bl_res.result_rows[0][0]) if bl_res.result_rows else 0

        all_findings: list[ValueFinding] = []

        for field_token in scan_fields:
            params: dict[str, Any] = {**base_params}
            col = _col_expr(field_token, params, field_mappings)

            if baseline_end is None:
                # Self-baseline: flag values with count ≤ rarity_floor.
                params["floor"] = rarity_floor
                params["lim"] = per_field_limit
                sql = f"""
                    SELECT
                        {col} AS val,
                        count() AS cnt,
                        min(timestamp) AS first_seen,
                        toString(argMin(event_id, timestamp)) AS evt_id
                    FROM {db}.events
                    WHERE case_id = {{cid:String}}
                      AND has({{src:Array(String)}}, source_id)
                      AND {col} != ''
                    GROUP BY val
                    HAVING cnt <= {{floor:UInt32}}
                    ORDER BY cnt ASC, first_seen ASC
                    LIMIT {{lim:UInt32}}
                    {_HEAVY_SCAN_SETTINGS}
                """
            else:
                # Temporal: flag values seen in detect window but not in baseline.
                params["bl"] = to_clickhouse_utc(baseline_end)
                params["lim"] = per_field_limit
                sql = f"""
                    SELECT
                        {col} AS val,
                        countIf(timestamp >= {{bl:String}}) AS detect_cnt,
                        countIf(timestamp < {{bl:String}}) AS baseline_cnt,
                        minIf(timestamp, timestamp >= {{bl:String}}) AS first_seen,
                        toString(argMinIf(event_id, timestamp, timestamp >= {{bl:String}})) AS evt_id
                    FROM {db}.events
                    WHERE case_id = {{cid:String}}
                      AND has({{src:Array(String)}}, source_id)
                      AND {col} != ''
                      AND {TS_NOT_SENTINEL_SQL}
                    GROUP BY val
                    HAVING baseline_cnt = 0 AND detect_cnt > 0
                    ORDER BY detect_cnt ASC, first_seen ASC
                    LIMIT {{lim:UInt32}}
                    {_HEAVY_SCAN_SETTINGS}
                """

            rows = self.ch.client.query(sql, parameters=params).result_rows

            for row in rows:
                if baseline_end is None:
                    val, cnt, first_seen, evt_id = row
                    effective_cnt = int(cnt)
                else:
                    val, detect_cnt, _bl_cnt, first_seen, evt_id = row
                    effective_cnt = int(detect_cnt)

                if not val:
                    continue

                score = (
                    -math.log(effective_cnt / total_events)
                    if effective_cnt > 0 and total_events > 0
                    else 0.0
                )
                # min(timestamp)/minIf(...) now return a native DateTime (not a
                # ClickHouse-formatted string) so we can attach an explicit UTC
                # offset before serializing — a bare "YYYY-MM-DD HH:MM:SS"
                # string is ambiguous to JS's Date parser (browsers treat it as
                # local time), which silently shifted the histogram markers and
                # event-grid anomaly matching by the browser's UTC offset.
                first_seen_str = _present_ts(first_seen)
                evt_id_str = str(evt_id) if evt_id else None
                mini_event = _stub_event(evt_id_str, case_id, first_seen_str)

                details: dict[str, Any] = {
                    "detector": "value_novelty",
                    "method": method,
                    "field": field_token,
                    "value": str(val),
                    "count": effective_cnt,
                    "total_events": total_events,
                    "surprise": round(score, 4),
                }
                if baseline_end is not None:
                    details["baseline_size"] = baseline_size

                all_findings.append(
                    ValueFinding(
                        field=field_token,
                        value=str(val),
                        count=effective_cnt,
                        score=round(score, 4),
                        first_seen=first_seen_str,
                        event_id=evt_id_str,
                        event=mini_event,
                        details=details,
                    )
                )

        # Suppress findings whose representative event was marked normal.
        if exclude_event_ids:
            all_findings = [
                f for f in all_findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        # Sort by surprise descending (rarest first), apply global limit,
        # then hydrate only the surviving findings' representative events.
        all_findings.sort(key=lambda f: f.score, reverse=True)
        results = all_findings[:limit]
        self._hydrate_finding_events(case_id, source_ids, results)
        return StatAnomalyResult(
            status="ok",
            detector="value_novelty",
            method=method,
            baseline_size=baseline_size,
            results=results,
        )

    def find_value_combos(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        rarity_floor: int = 3,
        baseline_end: datetime | None = None,
        exclude_event_ids: set[str] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> StatAnomalyResult:
        """Return rare or first-seen *combinations* of field values.

        The multi-field extension of :meth:`find_value_novelty`: instead of
        grouping by a single field, group by two or more field expressions
        together (``GROUP BY expr0, expr1, ...``) and score each surviving
        combination by the same surprise formula.

        *fields* must name at least two field tokens. When ``None``, the top
        two highest-coverage recommended fields are used as a single tuple —
        no pairwise enumeration (that would be one ClickHouse round-trip per
        pair and a result set no analyst can triage).

        Modes mirror :meth:`find_value_novelty`: *self-baseline* flags
        combinations appearing ≤ *rarity_floor* times in the whole corpus;
        *temporal* flags combinations absent from the baseline window but
        present after ``baseline_end``.

        Raises:
            ValueError: if fewer than two fields are resolved.
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "self-baseline" if baseline_end is None else "temporal"

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="value_combo",
                method=method,
                baseline_size=0,
            )

        # Field resolution (auto mode) issues live queries — only after the
        # empty-corpus short-circuit above.
        if fields is not None:
            combo_fields = fields
        else:
            rec = self.recommend_novelty_fields(
                case_id, source_ids, total=total_events, field_mappings=field_mappings
            )
            combo_fields = [f.token for f in rec if f.recommended][:2]

        if len(combo_fields) < 2:
            if fields is not None:
                raise ValueError("value_combo requires at least two fields")
            # Auto mode couldn't find two useful fields to combine.
            return StatAnomalyResult(
                status="insufficient_data",
                detector="value_combo",
                method=method,
                baseline_size=total_events,
            )

        # Build one expression per field into a single shared params dict —
        # distinct prefixes (fk0, fk1, …) keep the attribute-key bind params
        # from colliding (see _col_expr's `prefix`).
        params: dict[str, Any] = {**base_params}
        exprs = [
            _col_expr(tok, params, field_mappings, prefix=f"fk{i}")
            for i, tok in enumerate(combo_fields)
        ]
        val_cols = ", ".join(f"{expr} AS v{i}" for i, expr in enumerate(exprs))
        group_by = ", ".join(f"v{i}" for i in range(len(exprs)))
        non_empty = " AND ".join(f"{expr} != ''" for expr in exprs)

        # Baseline size (temporal only).
        baseline_size = total_events
        if baseline_end is not None:
            bl_res = self.ch.client.query(
                f"SELECT count() FROM {db}.events"
                f" WHERE case_id = {{cid:String}}"
                f" AND has({{src:Array(String)}}, source_id)"
                f" AND timestamp < {{bl:String}}",
                parameters={**base_params, "bl": to_clickhouse_utc(baseline_end)},
            )
            baseline_size = int(bl_res.result_rows[0][0]) if bl_res.result_rows else 0

        if baseline_end is None:
            params["floor"] = rarity_floor
            params["lim"] = limit
            sql = f"""
                SELECT
                    {val_cols},
                    count() AS cnt,
                    min(timestamp) AS first_seen,
                    toString(argMin(event_id, timestamp)) AS evt_id
                FROM {db}.events
                WHERE case_id = {{cid:String}}
                  AND has({{src:Array(String)}}, source_id)
                  AND {non_empty}
                GROUP BY {group_by}
                HAVING cnt <= {{floor:UInt32}}
                ORDER BY cnt ASC, first_seen ASC
                LIMIT {{lim:UInt32}}
                {_HEAVY_SCAN_SETTINGS}
            """
        else:
            params["bl"] = to_clickhouse_utc(baseline_end)
            params["lim"] = limit
            sql = f"""
                SELECT
                    {val_cols},
                    countIf(timestamp >= {{bl:String}}) AS detect_cnt,
                    countIf(timestamp < {{bl:String}}) AS baseline_cnt,
                    minIf(timestamp, timestamp >= {{bl:String}}) AS first_seen,
                    toString(argMinIf(event_id, timestamp, timestamp >= {{bl:String}})) AS evt_id
                FROM {db}.events
                WHERE case_id = {{cid:String}}
                  AND has({{src:Array(String)}}, source_id)
                  AND {non_empty}
                  AND {TS_NOT_SENTINEL_SQL}
                GROUP BY {group_by}
                HAVING baseline_cnt = 0 AND detect_cnt > 0
                ORDER BY detect_cnt ASC, first_seen ASC
                LIMIT {{lim:UInt32}}
                {_HEAVY_SCAN_SETTINGS}
            """

        rows = self.ch.client.query(sql, parameters=params).result_rows
        n_fields = len(exprs)

        all_findings: list[ComboFinding] = []
        for row in rows:
            values = [str(v) for v in row[:n_fields]]
            if baseline_end is None:
                cnt = int(row[n_fields])
                first_seen, evt_id = row[n_fields + 1 : n_fields + 3]
            else:
                cnt = int(row[n_fields])  # detect_cnt
                first_seen, evt_id = row[n_fields + 2 : n_fields + 4]

            if any(v == "" for v in values):
                continue

            score = -math.log(cnt / total_events) if cnt > 0 and total_events > 0 else 0.0
            first_seen_str = _present_ts(first_seen)
            evt_id_str = str(evt_id) if evt_id else None
            mini_event = _stub_event(evt_id_str, case_id, first_seen_str)

            details: dict[str, Any] = {
                "detector": "value_combo",
                "method": method,
                "fields": combo_fields,
                "values": values,
                "count": cnt,
                "total_events": total_events,
                "surprise": round(score, 4),
            }
            if baseline_end is not None:
                details["baseline_size"] = baseline_size

            all_findings.append(
                ComboFinding(
                    fields=list(combo_fields),
                    values=values,
                    count=cnt,
                    score=round(score, 4),
                    first_seen=first_seen_str,
                    event_id=evt_id_str,
                    event=mini_event,
                    details=details,
                )
            )

        if exclude_event_ids:
            all_findings = [
                f for f in all_findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        all_findings.sort(key=lambda f: f.score, reverse=True)
        results = all_findings[:limit]
        self._hydrate_finding_events(case_id, source_ids, results)
        return StatAnomalyResult(
            status="ok",
            detector="value_combo",
            method=method,
            baseline_size=baseline_size,
            results=results,
        )

    # ------------------------------------------------------------------
    # Numeric range violations
    # ------------------------------------------------------------------

    def recommend_numeric_fields(
        self,
        case_id: str,
        source_ids: list[str],
        total: int | None = None,
        field_mappings: dict[str, list[str]] | None = None,
        inventory: list[tuple[str, int, int]] | None = None,
        min_ratio: float = _MIN_NUMERIC_RATIO,
    ) -> list[NumericFieldInfo]:
        """Return fields whose values parse as numbers, for the range detector.

        Candidates come from the field inventory (top-level columns + attribute
        keys, or a supplied *inventory* — the per-source stats cache); the ones
        with non-trivial coverage and cardinality are probed with a single
        batched query computing, per field, the fraction of non-empty values
        that ``toFloat64OrNull`` parses. Fields at or above *min_ratio* are
        marked recommended. Type detection is purely syntactic — a field of
        HTTP status codes qualifies, but so would any numeric-looking id, which
        is why the UI leans on temporal mode for those.
        """
        if inventory is None:
            inventory, total = self.field_inventory(case_id, source_ids, total, field_mappings)
        elif total is None:
            raise ValueError("total is required when inventory is supplied")
        if not total:
            return []

        # Keep fields with ≥5% coverage and more than one distinct value, cap 15.
        candidates = [
            (tok, dist, cov) for tok, dist, cov in inventory if cov / total >= 0.05 and dist > 1
        ][:_MAX_AUTO_SCAN_FIELDS]
        if not candidates:
            return []

        db = self.ch.database
        params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        parts = []
        exprs = []
        for i, (tok, _, _) in enumerate(candidates):
            expr = _col_expr(tok, params, field_mappings, prefix=f"nf{i}")
            exprs.append(expr)
            parts.append(
                f"countIf(toFloat64OrNull({expr}) IS NOT NULL) AS num{i}, "
                f"countIf({expr} != '') AS ne{i}"
            )
        probe_sql = (
            f"SELECT {', '.join(parts)}"
            f" FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" {_HEAVY_SCAN_SETTINGS}"
        )
        row = self.ch.client.query(probe_sql, parameters=params).result_rows
        out: list[NumericFieldInfo] = []
        if row:
            r = row[0]
            for i, (tok, dist, cov) in enumerate(candidates):
                num = int(r[i * 2])
                ne = int(r[i * 2 + 1])
                ratio = num / ne if ne else 0.0
                out.append(
                    NumericFieldInfo(
                        token=tok,
                        distinct=dist,
                        coverage=round(cov / total, 4),
                        numeric_ratio=round(ratio, 4),
                        recommended=ratio >= min_ratio,
                    )
                )
        out.sort(key=lambda f: (not f.recommended, -f.numeric_ratio, -f.coverage))
        return out

    def find_range_violations(
        self,
        case_id: str,
        source_ids: list[str],
        fields: list[str] | None = None,
        limit: int = 50,
        per_field_limit: int = 25,
        baseline_end: datetime | None = None,
        exclude_event_ids: set[str] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> StatAnomalyResult:
        """Return numeric values falling outside a field's learned range.

        For each numeric field, learn a baseline band and flag values outside
        it. *self-baseline* (``method="iqr"``) uses the Tukey fence
        ``[q1 − 1.5·IQR, q3 + 1.5·IQR]`` over the whole corpus; *temporal*
        (``method="temporal-range"``) learns exact min/max from the baseline
        window (``timestamp < baseline_end``) and flags detect-window values
        outside it.

        When *fields* is ``None`` the numeric-field recommender selects
        candidates automatically. A field with fewer than ``_MIN_RANGE_BASELINE``
        numeric baseline samples is skipped; when every scanned field is skipped
        the status is ``insufficient_data``.
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "iqr" if baseline_end is None else "temporal-range"

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="numeric_range",
                method=method,
                baseline_size=0,
            )

        if fields is not None:
            scan_fields = fields
        else:
            rec = self.recommend_numeric_fields(
                case_id, source_ids, total=total_events, field_mappings=field_mappings
            )
            scan_fields = [f.token for f in rec if f.recommended][:_MAX_AUTO_SCAN_FIELDS]

        bl_str = to_clickhouse_utc(baseline_end) if baseline_end is not None else None
        all_findings: list[RangeFinding] = []
        evaluated_fields = 0

        for field_token in scan_fields:
            # --- Learn the band from the baseline. ---
            stat_params: dict[str, Any] = {**base_params}
            col = _col_expr(field_token, stat_params, field_mappings)
            num_src = (
                f"SELECT toFloat64OrNull({col}) AS num, timestamp"
                f" FROM {db}.events"
                f" WHERE case_id = {{cid:String}}"
                f" AND has({{src:Array(String)}}, source_id)"
                f" AND {col} != ''"
            )
            if baseline_end is None:
                stat_sql = (
                    f"SELECT quantile(0.25)(num) AS q1, quantile(0.75)(num) AS q3, count() AS n"
                    f" FROM ({num_src}) WHERE num IS NOT NULL {_HEAVY_SCAN_SETTINGS}"
                )
            else:
                stat_params["bl"] = bl_str
                stat_sql = (
                    f"SELECT min(num) AS lo, max(num) AS hi, count() AS n"
                    f" FROM ({num_src}) WHERE num IS NOT NULL AND timestamp < {{bl:String}}"
                    f" {_HEAVY_SCAN_SETTINGS}"
                )
            srows = self.ch.client.query(stat_sql, parameters=stat_params).result_rows
            if not srows or srows[0][2] is None:
                continue
            a, b, n = srows[0]
            if int(n) < _MIN_RANGE_BASELINE or a is None or b is None:
                continue

            if baseline_end is None:
                q1, q3 = float(a), float(b)
                iqr = q3 - q1
                lower = q1 - 1.5 * iqr
                upper = q3 + 1.5 * iqr
                band_extra: dict[str, Any] = {
                    "q1": round(q1, 4),
                    "q3": round(q3, 4),
                    "iqr": round(iqr, 4),
                }
            else:
                lower, upper = float(a), float(b)
                band_extra = {"baseline_min": round(lower, 4), "baseline_max": round(upper, 4)}

            # A degenerate (zero-width) band would divide the score by zero and
            # flag every off-band value with infinite severity — floor the width.
            width = max(upper - lower, 1e-9)
            evaluated_fields += 1

            # --- Flag values outside the band. ---
            viol_params: dict[str, Any] = {**base_params}
            vcol = _col_expr(field_token, viol_params, field_mappings)
            viol_params["lo"] = lower
            viol_params["hi"] = upper
            viol_params["plim"] = per_field_limit
            detect_clause = ""
            if baseline_end is not None:
                viol_params["bl"] = bl_str
                # Exclude sentinel/undated rows: they satisfy `>= bl` (year-2299
                # sentinel) and would otherwise attribute an undated event's
                # numeric value to the detect window. Matches the sentinel guard
                # value_combo/frequency apply.
                detect_clause = f" AND timestamp >= {{bl:String}} AND {TS_NOT_SENTINEL_SQL}"
            viol_sql = f"""
                SELECT
                    num AS val,
                    count() AS cnt,
                    min(timestamp) AS first_seen,
                    toString(argMin(event_id, timestamp)) AS evt_id
                FROM (
                    SELECT toFloat64OrNull({vcol}) AS num, timestamp, event_id
                    FROM {db}.events
                    WHERE case_id = {{cid:String}}
                      AND has({{src:Array(String)}}, source_id)
                      AND {vcol} != ''
                )
                WHERE num IS NOT NULL AND (num < {{lo:Float64}} OR num > {{hi:Float64}}){detect_clause}
                GROUP BY val
                ORDER BY greatest({{lo:Float64}} - val, val - {{hi:Float64}}) DESC, first_seen ASC
                LIMIT {{plim:UInt32}}
                {_HEAVY_SCAN_SETTINGS}
            """
            vrows = self.ch.client.query(viol_sql, parameters=viol_params).result_rows

            for vrow in vrows:
                val, cnt, first_seen, evt_id = vrow
                if val is None:
                    continue
                val_f = float(val)
                direction = "below" if val_f < lower else "above"
                excess = (lower - val_f) if val_f < lower else (val_f - upper)
                score = round(excess / width, 4)
                first_seen_str = _present_ts(first_seen)
                evt_id_str = str(evt_id) if evt_id else None
                mini_event = _stub_event(evt_id_str, case_id, first_seen_str)

                details: dict[str, Any] = {
                    "detector": "numeric_range",
                    "method": method,
                    "field": field_token,
                    "value": val_f,
                    "count": int(cnt),
                    "lower": round(lower, 4),
                    "upper": round(upper, 4),
                    "direction": direction,
                    "excess": round(excess, 4),
                    "baseline_n": int(n),
                    **band_extra,
                }
                all_findings.append(
                    RangeFinding(
                        field=field_token,
                        value=val_f,
                        count=int(cnt),
                        score=score,
                        direction=direction,
                        lower=round(lower, 4),
                        upper=round(upper, 4),
                        first_seen=first_seen_str,
                        event_id=evt_id_str,
                        event=mini_event,
                        details=details,
                    )
                )

        if evaluated_fields == 0:
            return StatAnomalyResult(
                status="insufficient_data",
                detector="numeric_range",
                method=method,
                baseline_size=total_events,
            )

        if exclude_event_ids:
            all_findings = [
                f for f in all_findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        all_findings.sort(key=lambda f: f.score, reverse=True)
        results = all_findings[:limit]
        self._hydrate_finding_events(case_id, source_ids, results)
        return StatAnomalyResult(
            status="ok",
            detector="numeric_range",
            method=method,
            baseline_size=total_events,
            results=results,
        )

    # ------------------------------------------------------------------
    # Frequency / volume anomalies
    # ------------------------------------------------------------------

    def find_frequency_anomalies(
        self,
        case_id: str,
        source_ids: list[str],
        series_field: str = "artifact",
        limit: int = 20,
        bucket_count: int = 60,
        z_threshold: float = 2.5,
        baseline_end: datetime | None = None,
        exclude_event_ids: set[str] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> StatAnomalyResult:
        """Return time windows with anomalous event-count frequency.

        Event counts per ``series_field`` value are windowed into
        *bucket_count* time buckets.  For each series with at least
        ``_MIN_FREQUENCY_BUCKETS`` data points, z-scores are computed and
        windows with |z| ≥ *z_threshold* are returned ranked by |z| descending.

        When ``baseline_end`` is provided, the mean/std are computed from
        baseline-window buckets only; the detect window is then scored against
        that baseline (temporal sub-mode).
        """
        if baseline_end is not None:
            baseline_end = ensure_utc(baseline_end)
        self.ch.init_schema()
        db = self.ch.database
        field_params: dict[str, Any] = {}
        col = _col_expr(series_field, field_params, field_mappings)

        src_params: dict[str, Any] = {"cid": case_id, "src": source_ids}

        # Resolve time range.
        min_ts, max_ts = query_timestamp_range(
            self.ch.client,
            db,
            "case_id = {cid:String} AND has({src:Array(String)}, source_id)",
            src_params,
        )
        if min_ts is None or max_ts is None:
            return StatAnomalyResult(
                status="no_data",
                detector="frequency",
                method="z-score" if baseline_end is None else "temporal-z-score",
                baseline_size=0,
                z_threshold=z_threshold,
            )

        interval = bucket_interval_seconds(min_ts, max_ts, bucket_count)

        # Fetch per-bucket, per-series event counts.
        params: dict[str, Any] = {**src_params, **field_params, "iv": interval}
        bucket_sql = f"""
            SELECT
                toStartOfInterval(timestamp, INTERVAL {{iv:UInt32}} second) AS bucket,
                {col} AS series_val,
                count() AS cnt
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {TS_NOT_SENTINEL_SQL}
              AND {col} != ''
            GROUP BY bucket, series_val
            ORDER BY bucket
            {_HEAVY_SCAN_SETTINGS}
        """
        brows = self.ch.client.query(bucket_sql, parameters=params).result_rows

        if not brows:
            return StatAnomalyResult(
                status="no_data",
                detector="frequency",
                method="z-score" if baseline_end is None else "temporal-z-score",
                baseline_size=0,
                z_threshold=z_threshold,
            )

        # Build series dict: series_val → [(bucket_dt, cnt)].
        series: dict[str, list[tuple[Any, int]]] = defaultdict(list)
        for brow in brows:
            bucket, sv, cnt = brow
            if sv:
                series[sv].append((bucket, int(cnt)))

        method = "z-score" if baseline_end is None else "temporal-z-score"

        # In temporal mode, "baseline" means the pre-baseline_end window only;
        # in self-baseline mode the whole series is its own baseline.
        baseline_size = 0

        # Z-score each bucket, collect anomalous windows.
        findings: list[FreqFinding] = []
        evaluated_series = 0

        for sv, pts in series.items():
            pts_aware = [(ensure_utc(b), c) for b, c in pts]

            if baseline_end is not None:
                bl_pts = [(b, c) for b, c in pts_aware if b < baseline_end]
                detect_pts = [(b, c) for b, c in pts_aware if b >= baseline_end]
                if not detect_pts:
                    continue
                if not bl_pts:
                    # Series absent from the baseline entirely but active in
                    # the detect window — no std can be computed from zero
                    # points, so score against a zero baseline instead of
                    # skipping (mirrors find_value_novelty's
                    # baseline_cnt == 0 case: "new activity after the
                    # incident start" is exactly what temporal mode should
                    # surface, not silently drop).
                    evaluated_series += 1
                    for bucket_dt, cnt in detect_pts:
                        z = cnt / _MIN_FREQUENCY_STD
                        if abs(z) >= z_threshold:
                            findings.append(
                                _freq_finding(
                                    series_field,
                                    sv,
                                    bucket_dt,
                                    interval,
                                    cnt,
                                    0.0,
                                    z,
                                    method,
                                )
                            )
                    continue
                if len(bl_pts) < _MIN_FREQUENCY_BUCKETS:
                    continue
                baseline_size += sum(c for _, c in bl_pts)
                counts_bl = np.array([c for _, c in bl_pts], dtype=np.float64)
                mean_val = float(counts_bl.mean())
                std_val = float(counts_bl.std(ddof=1))
                if std_val < 1e-9:
                    continue  # Perfectly constant baseline — no z-score possible.
                evaluated_series += 1
                for bucket_dt, cnt in detect_pts:
                    z = (cnt - mean_val) / std_val
                    if abs(z) >= z_threshold:
                        findings.append(
                            _freq_finding(
                                series_field,
                                sv,
                                bucket_dt,
                                interval,
                                cnt,
                                mean_val,
                                z,
                                method,
                            )
                        )
            else:
                if len(pts_aware) < _MIN_FREQUENCY_BUCKETS:
                    continue
                baseline_size += sum(c for _, c in pts_aware)
                # Leave-one-out mean/std: score each bucket against the rest of
                # the series, not against itself. Otherwise a single dominant
                # spike inflates its own baseline and can suppress detection
                # of the very spike being scored.
                counts = np.array([c for _, c in pts_aware], dtype=np.float64)
                n = len(counts)
                total = float(counts.sum())
                total_sq = float(np.square(counts).sum())
                evaluated_series += 1
                for (bucket_dt, cnt), c in zip(pts_aware, counts, strict=False):
                    n_loo = n - 1
                    mean_val = (total - c) / n_loo
                    var_loo = (total_sq - c * c - n_loo * mean_val * mean_val) / (n_loo - 1)
                    # Floor the leave-one-out std rather than skipping when the
                    # rest of the series is constant (or near it) — otherwise a
                    # single outlier bucket, scored against a baseline it was
                    # excluded from, would divide by ~0 and either blow up or
                    # (previously) get silently dropped instead of flagged.
                    std_val = max(math.sqrt(max(var_loo, 0.0)), _MIN_FREQUENCY_STD)
                    z = (cnt - mean_val) / std_val
                    if abs(z) >= z_threshold:
                        findings.append(
                            _freq_finding(
                                series_field,
                                sv,
                                bucket_dt,
                                interval,
                                int(cnt),
                                mean_val,
                                z,
                                method,
                            )
                        )

        if not findings:
            return StatAnomalyResult(
                status="ok" if evaluated_series > 0 else "insufficient_data",
                detector="frequency",
                method=method,
                baseline_size=baseline_size,
                results=[],
                z_threshold=z_threshold,
            )

        # Hydrate representative events for every candidate finding (one
        # batched query, not one per finding) and suppress any whose event
        # was marked normal *before* ranking/limiting — filtering after the
        # `[:limit]` slice would shrink the page below `limit` instead of
        # backfilling from the next-ranked window (find_value_novelty
        # filters before slicing for the same reason).
        findings = self._hydrate_freq_findings(
            findings, case_id, source_ids, col, db, field_params, interval
        )
        if exclude_event_ids:
            findings = [
                f for f in findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        findings.sort(key=lambda f: f.score, reverse=True)
        top = findings[:limit]

        return StatAnomalyResult(
            status="ok",
            detector="frequency",
            method=method,
            baseline_size=baseline_size,
            results=top,
            z_threshold=z_threshold,
        )

    def _hydrate_freq_findings(
        self,
        findings: list[FreqFinding],
        case_id: str,
        source_ids: list[str],
        col: str,
        db: str,
        field_params: dict[str, Any],
        interval: int,
    ) -> list[FreqFinding]:
        """Fetch one representative event per (series_value, window) pair.

        A single grouped query replaces one query per finding: every
        (bucket, series value) pair here was already aggregated together in
        ``find_frequency_anomalies``'s bucket scan, so ``argMin(..., timestamp)``
        recovers the earliest event of each pair in one round-trip.
        """
        if not findings:
            return findings

        series_values = sorted({f.series_value for f in findings})
        # Keep bucket bounds timezone-aware UTC: clickhouse_connect binds a
        # *naive* DateTime param in the client process's local timezone, so
        # stripping tzinfo here made every `IN {buckets}` comparison miss by
        # the client's UTC offset (representative events silently never
        # hydrated on any non-UTC host). Events are stored/compared in UTC.
        buckets = sorted({ensure_utc(datetime.fromisoformat(f.window_start)) for f in findings})
        params: dict[str, Any] = {
            **field_params,
            "cid": case_id,
            "src": source_ids,
            "vals": series_values,
            "buckets": buckets,
            "iv": interval,
        }
        # Phase 1 aggregates only argMin(event_id, ...) per (bucket, series)
        # pair — the former all-column argMin dragged message/attributes for
        # every matching row through the aggregation. The winners' full rows
        # are then fetched in one bounded get_events_by_ids batch.
        #
        # `timestamp` is DateTime64(3), and toStartOfInterval over a DateTime64
        # returns DateTime64 on ClickHouse builds that preserve the argument's
        # precision — comparing that against an Array(DateTime) literal then
        # raises TYPE_MISMATCH (code 53), 500-ing the whole frequency detector
        # (value_novelty is unaffected — it never runs this hydration query,
        # which is why only frequency broke). Bucket boundaries are always
        # whole seconds (interval >= 1s), so wrapping in toDateTime() is
        # lossless and pins both sides to DateTime regardless of CH version.
        bucket_expr = "toDateTime(toStartOfInterval(timestamp, INTERVAL {iv:UInt32} second))"
        sql = f"""
            SELECT
                {bucket_expr} AS bucket,
                {col} AS series_val,
                toString(argMin(event_id, timestamp)) AS evt_id
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {col} IN {{vals:Array(String)}}
              AND {bucket_expr} IN {{buckets:Array(DateTime)}}
            GROUP BY bucket, series_val
            {_HEAVY_SCAN_SETTINGS}
        """
        rows = self.ch.client.query(sql, parameters=params).result_rows

        id_by_key: dict[tuple[str, str], str] = {
            (str(series_val), ensure_utc(bucket).isoformat()): str(evt_id)
            for bucket, series_val, evt_id in rows
            if evt_id
        }
        events_by_id = self.ch.get_events_by_ids(
            case_id, source_ids, sorted(set(id_by_key.values()))
        )

        hydrated: list[FreqFinding] = []
        for f in findings:
            evt_id = id_by_key.get((f.series_value, f.window_start))
            evt = events_by_id.get(evt_id) if evt_id else None
            if evt is not None:
                hydrated.append(replace(f, event_id=str(evt.get("event_id", "")), event=evt))
            elif evt_id:
                hydrated.append(
                    replace(f, event_id=evt_id, event=_stub_event(evt_id, case_id, None))
                )
            else:
                hydrated.append(f)
        return hydrated

    # ------------------------------------------------------------------
    # Timestamp-order violations
    # ------------------------------------------------------------------

    def find_order_violations(
        self,
        case_id: str,
        source_ids: list[str],
        min_skew_seconds: float = 1.0,
        limit: int = 50,
        exclude_event_ids: set[str] | None = None,
    ) -> StatAnomalyResult:
        """Return events whose timestamp jumps backwards in record order.

        Record order within a source is ``byte_offset`` (the byte position of
        the raw record in the source file — monotonic per file), then
        ``line_number`` and ``event_id`` as deterministic tie-breaks. Each
        event's timestamp is compared to its immediate predecessor
        (``lagInFrame`` over a per-``source_id`` window); a backwards jump of
        at least *min_skew_seconds* is a violation.

        This detector is mode-less (``method="sequential"``) — there is no
        baseline/detect split. NULL timestamps are excluded (they carry no
        order signal).
        """
        self.ch.init_schema()
        db = self.ch.database
        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "sequential"

        total_events = self._count_events(case_id, source_ids)
        if total_events == 0:
            return StatAnomalyResult(
                status="no_data",
                detector="timestamp_order",
                method=method,
                baseline_size=0,
            )

        # Inner subquery: per source, lag the timestamp over record order and
        # compute the backwards skew (0 when in order). `dateDiff('millisecond')`
        # / 1000 keeps sub-second precision that dateDiff('second') would lose.
        inner = f"""
            SELECT
                source_id,
                toString(event_id) AS event_id,
                timestamp,
                -- toNullable: on the non-Nullable timestamp column lagInFrame
                -- returns the type default (1970-01-01), not NULL, for each
                -- source's first row — which would make the IS NOT NULL
                -- first-row guard below always-true.
                lagInFrame(toNullable(timestamp)) OVER w AS prev_ts,
                byte_offset,
                line_number,
                message,
                if(prev_ts IS NOT NULL AND timestamp < prev_ts,
                   dateDiff('millisecond', timestamp, prev_ts) / 1000.0, 0.) AS skew
            FROM {db}.events
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
              AND {TS_NOT_SENTINEL_SQL}
            WINDOW w AS (
                PARTITION BY source_id
                ORDER BY byte_offset, line_number, event_id
                ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING
            )
        """

        # Per-source summary: violation count + worst skew, for the UI's
        # per-source grouping header and the overall status.
        summary_params = {**base_params, "skew": float(min_skew_seconds)}
        summary_sql = f"""
            SELECT
                source_id,
                countIf(skew >= {{skew:Float64}}) AS n_viol,
                maxIf(skew, skew >= {{skew:Float64}}) AS max_skew
            FROM ({inner})
            GROUP BY source_id
            {_HEAVY_SCAN_SETTINGS}
        """
        summary_rows = self.ch.client.query(summary_sql, parameters=summary_params).result_rows
        source_summary: dict[str, tuple[int, float]] = {
            str(r[0]): (int(r[1]), float(r[2]) if r[2] is not None else 0.0) for r in summary_rows
        }
        total_violations = sum(n for n, _ in source_summary.values())

        if total_violations == 0:
            return StatAnomalyResult(
                status="ok",
                detector="timestamp_order",
                method=method,
                baseline_size=total_events,
                results=[],
            )

        # Detail: the worst violations across all sources.
        detail_params = {**base_params, "skew": float(min_skew_seconds), "lim": limit}
        detail_sql = f"""
            SELECT source_id, event_id, timestamp, prev_ts, skew, byte_offset, line_number, message
            FROM ({inner})
            WHERE skew >= {{skew:Float64}}
            ORDER BY skew DESC, source_id, byte_offset
            LIMIT {{lim:UInt32}}
            {_HEAVY_SCAN_SETTINGS}
        """
        rows = self.ch.client.query(detail_sql, parameters=detail_params).result_rows

        findings: list[OrderFinding] = []
        for row in rows:
            source_id, event_id, ts, prev_ts, skew, byte_offset, line_number, msg = row
            if not event_id:
                continue
            ts_str = _present_ts(ts)
            prev_str = ensure_utc(prev_ts).isoformat() if prev_ts else None
            n_viol, max_skew = source_summary.get(str(source_id), (0, 0.0))
            mini_event: dict[str, Any] = {
                "event_id": str(event_id),
                "case_id": case_id,
                "source_id": str(source_id),
                "message": str(msg) if msg else "",
                "timestamp": ts_str,
                "timestamp_desc": None,
                "artifact": None,
                "artifact_long": None,
                "display_name": None,
                "tags": [],
                "attributes": {},
                "content_hash": "",
                "file_hash": "",
                "parser_name": "",
                "parser_version": "",
                "source_file": "",
                "byte_offset": int(byte_offset),
                "line_number": int(line_number),
                "embedding_model": None,
                "embedding_config_hash": None,
                "ingest_time": None,
            }
            details: dict[str, Any] = {
                "detector": "timestamp_order",
                "method": method,
                "source_id": str(source_id),
                "prev_timestamp": prev_str,
                "skew_seconds": round(float(skew), 3),
                "byte_offset": int(byte_offset),
                "line_number": int(line_number),
                "min_skew_seconds": float(min_skew_seconds),
                "source_total_violations": n_viol,
                "source_max_skew": round(max_skew, 3),
            }
            findings.append(
                OrderFinding(
                    source_id=str(source_id),
                    event_id=str(event_id),
                    timestamp=ts_str or "",
                    prev_timestamp=prev_str or "",
                    skew_seconds=round(float(skew), 3),
                    byte_offset=int(byte_offset),
                    line_number=int(line_number),
                    score=round(float(skew), 3),
                    event=mini_event,
                    details=details,
                )
            )

        # Suppress findings whose representative event was marked normal
        # (before the limit is applied — filtering after would shrink the page
        # instead of backfilling from the next-worst violation).
        if exclude_event_ids:
            findings = [
                f for f in findings if not f.event_id or f.event_id not in exclude_event_ids
            ]

        return StatAnomalyResult(
            status="ok",
            detector="timestamp_order",
            method=method,
            baseline_size=total_events,
            results=findings[:limit],
        )
