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
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np

from tracevector.db.clickhouse import ClickHouseStore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Top-level event columns usable directly in SQL (vs attributes map lookup).
_TOP_LEVEL_COLUMNS = frozenset({
    "artifact",
    "timestamp_desc",
    "display_name",
    "message",
    "artifact_long",
    "parser_name",
    "source_file",
    "source_id",
})

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

# Minimum buckets in a frequency series for z-scoring to be meaningful.
_MIN_FREQUENCY_BUCKETS = 3

# Floor applied to the leave-one-out std in self-baseline mode so that a
# near-constant rest-of-series doesn't divide by ~0 when scoring the
# excluded point (half an event-count unit — small enough to still flag any
# real deviation, large enough to avoid blowing up the z-score to inf/NaN).
_MIN_FREQUENCY_STD = 0.5

# Columns selected when hydrating a representative event.
_EVENT_COLUMNS = (
    "event_id",
    "case_id",
    "source_id",
    "message",
    "timestamp",
    "timestamp_desc",
    "artifact",
    "artifact_long",
    "display_name",
    "tags",
    "attributes",
    "content_hash",
    "file_hash",
    "parser_name",
    "parser_version",
    "source_file",
    "byte_offset",
    "line_number",
    "embedding_model",
    "embedding_config_hash",
    "vector_id",
    "ingest_time",
)


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
class StatAnomalyResult:
    """Return value of statistical anomaly detection."""

    status: str         # "ok" | "no_data" | "insufficient_data"
    detector: str       # "value_novelty" | "frequency"
    method: str         # "self-baseline" | "temporal" | "z-score" | "temporal-z-score"
    baseline_size: int  # total events (value_novelty) or event-count used for z-score
    results: list[ValueFinding | FreqFinding] = field(default_factory=list)
    # Effective |z| cutoff used by the frequency detector; None for value_novelty.
    z_threshold: float | None = None


@dataclass
class NoveltyFieldInfo:
    """Field recommendation produced by :meth:`recommend_novelty_fields`.

    Used by the API to populate the field picker and frequency GROUP BY dropdown,
    and internally as the smart default for :meth:`find_value_novelty`.
    """

    token: str          # e.g. "artifact", "attr:status_code"
    distinct: int       # uniqExact() count
    coverage: float     # fraction of events with a non-empty value (0–1)
    kind: str           # "constant" | "identifier" | "categorical" | "sparse"
    recommended: bool   # True → include in default scan


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _col_expr(
    field_token: str,
    params: dict[str, Any],
    ctr: list[int],
) -> str:
    """Return a ClickHouse SQL expression for a field token.

    Top-level columns (``"artifact"``, ``"display_name"``, …) are returned as-is.
    Attribute keys prefixed with ``"attr:"`` (``"attr:user_agent"``) or bare
    attribute names are returned as ``attributes[{fkN:String}]`` with the key
    injected into *params*.  *ctr* is a single-element mutable list used as an
    auto-incrementing counter for unique parameter names.
    """
    if field_token in _TOP_LEVEL_COLUMNS:
        return field_token
    key = field_token[5:] if field_token.startswith("attr:") else field_token
    name = f"fk{ctr[0]}"
    ctr[0] += 1
    params[name] = key
    return f"attributes[{{{name}:String}}]"


def _fmt_dt(dt: datetime) -> str:
    """Format a datetime for ClickHouse comparison (no timezone)."""
    return dt.strftime("%Y-%m-%d %H:%M:%S")


def _ensure_utc(dt: Any) -> datetime:
    """Attach UTC if the datetime is naive."""
    if hasattr(dt, "tzinfo") and dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


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


def _row_to_event(columns: tuple[str, ...], row: tuple) -> dict[str, Any]:
    """Convert a ClickHouse result row into an Event-compatible dict.

    `timestamp`/`ingest_time` come back as naive `datetime` objects (the
    columns have no explicit timezone component) — attach UTC before
    serializing, otherwise the resulting "YYYY-MM-DDTHH:MM:SS" string (no
    offset) is ambiguous to JS's `Date` parser, which treats it as local time
    and silently shifts the displayed/compared timestamp by the browser's
    UTC offset.
    """
    d: dict[str, Any] = dict(zip(columns, row, strict=False))
    for key in ("timestamp", "ingest_time"):
        v = d.get(key)
        if v is not None and not isinstance(v, str):
            try:
                d[key] = _ensure_utc(v).isoformat()
            except AttributeError:
                d[key] = str(v)
    if "event_id" in d:
        d["event_id"] = str(d["event_id"])
    return d


def _fetch_event(
    ch: ClickHouseStore,
    db: str,
    case_id: str,
    source_ids: list[str],
    col: str,
    field_value: str,
    window_start: str,
    window_end: str,
    extra_params: dict[str, Any],
) -> dict[str, Any] | None:
    """Return one representative event for a (field_value, time_window) pair."""
    params = {
        **extra_params,
        "cid": case_id,
        "src": source_ids,
        "sv": field_value,
        "ws": window_start[:19].replace("T", " "),
        "we": window_end[:19].replace("T", " "),
    }
    cols_sql = ", ".join(_EVENT_COLUMNS)
    sql = f"""
        SELECT {cols_sql}
        FROM {db}.events
        WHERE case_id = {{cid:String}}
          AND has({{src:Array(String)}}, source_id)
          AND {col} = {{sv:String}}
          AND timestamp >= {{ws:String}}
          AND timestamp < {{we:String}}
        ORDER BY timestamp
        LIMIT 1
    """
    try:
        res = ch.client.query(sql, parameters=params)
        if res.result_rows:
            return _row_to_event(_EVENT_COLUMNS, res.result_rows[0])
    except Exception:  # noqa: BLE001
        pass
    return None


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

    def recommend_novelty_fields(
        self,
        case_id: str,
        source_ids: list[str],
    ) -> list[NoveltyFieldInfo]:
        """Return a ranked, annotated list of candidate fields for value_novelty.

        Fields are classified by cardinality *without* any content heuristics so
        the result is valid for any timeseries type (nginx, Windows events, …):

        * ``constant``   — distinct ≤ 1 value; no signal.
        * ``identifier`` — near-unique (distinct / non-empty ≥ 0.9); hashes, IDs,
          free-text messages.  Not useful for grouping.
        * ``sparse``     — non-empty coverage < 5 % of events.  Low signal.
        * ``categorical``— recommended; moderate cardinality with decent coverage.

        Candidate set = a curated list of categorical top-level columns
        (``artifact``, ``timestamp_desc``, ``display_name``, ``parser_name``)
        plus every attribute key found in the events table.
        """
        self.ch.init_schema()
        db = self.ch.database
        params: dict[str, Any] = {"cid": case_id, "src": source_ids}

        # Total event count used for coverage denominator.
        total_res = self.ch.client.query(
            f"SELECT count() FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)",
            parameters=params,
        )
        total = int(total_res.result_rows[0][0]) if total_res.result_rows else 0
        if total == 0:
            return []

        findings: list[NoveltyFieldInfo] = []

        # -- Top-level columns (batched in a single aggregation) ---------------
        agg_parts = []
        for col in _NOVELTY_CANDIDATE_TOP_LEVEL:
            agg_parts.append(
                f"uniqExact({col}) AS {col}_dist,"
                f" countIf({col} != '') AS {col}_cov"
            )
        top_sql = (
            f"SELECT {', '.join(agg_parts)}"
            f" FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
        )
        top_res = self.ch.client.query(top_sql, parameters=params)
        if top_res.result_rows:
            row = top_res.result_rows[0]
            for i, col in enumerate(_NOVELTY_CANDIDATE_TOP_LEVEL):
                dist = int(row[i * 2])
                cov_count = int(row[i * 2 + 1])
                coverage = cov_count / total if total else 0.0
                kind, recommended = _classify_field(dist, cov_count, total)
                findings.append(
                    NoveltyFieldInfo(
                        token=col,
                        distinct=dist,
                        coverage=round(coverage, 4),
                        kind=kind,
                        recommended=recommended,
                    )
                )

        # -- Attribute keys (ARRAY JOIN to enumerate + aggregate in one pass) --
        attr_sql = f"""
            SELECT
                key,
                uniqExact(attributes[key])        AS dist,
                countIf(notEmpty(attributes[key])) AS cov_count
            FROM {db}.events
            ARRAY JOIN mapKeys(attributes) AS key
            WHERE case_id = {{cid:String}}
              AND has({{src:Array(String)}}, source_id)
            GROUP BY key
            ORDER BY cov_count DESC
            LIMIT {{max_keys:UInt32}}
        """
        attr_res = self.ch.client.query(
            attr_sql, parameters={**params, "max_keys": _RECOMMENDER_MAX_ATTR_KEYS}
        )
        for key, dist, cov_count in attr_res.result_rows:
            coverage = int(cov_count) / total if total else 0.0
            kind, recommended = _classify_field(int(dist), int(cov_count), total)
            findings.append(
                NoveltyFieldInfo(
                    token=f"attr:{key}",
                    distinct=int(dist),
                    coverage=round(coverage, 4),
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
        res = self.ch.client.query(
            f"SELECT min(timestamp), max(timestamp) FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" AND timestamp IS NOT NULL",
            parameters=params,
        )
        if not res.result_rows:
            return None
        min_ts, max_ts = res.result_rows[0]
        if min_ts is None or max_ts is None:
            return None
        min_dt = _ensure_utc(min_ts)
        max_dt = _ensure_utc(max_ts)
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
    ) -> StatAnomalyResult:
        """Return rare or first-seen values per field, ranked by surprise score.

        *fields* is a list of field tokens (``"artifact"``, ``"timestamp_desc"``,
        ``"attr:user_agent"``).  When ``None``, the cardinality-based recommender
        selects useful attribute fields automatically (falls back to
        ``_DEFAULT_NOVELTY_FIELDS`` only if recommendation yields nothing).

        In *self-baseline* mode values appearing ≤ *rarity_floor* times are
        flagged.  In *temporal* mode (``baseline_end`` provided) any value absent
        from the baseline window but present in the detect window is flagged.
        """
        self.ch.init_schema()
        db = self.ch.database

        if fields is not None:
            scan_fields = fields
        else:
            # Auto-discover useful fields for this specific timeseries.
            rec = self.recommend_novelty_fields(case_id, source_ids)
            scan_fields = [f.token for f in rec if f.recommended] or _DEFAULT_NOVELTY_FIELDS

        base_params: dict[str, Any] = {"cid": case_id, "src": source_ids}
        method = "self-baseline" if baseline_end is None else "temporal"

        # Total event count for surprise score denominator.
        total_res = self.ch.client.query(
            f"SELECT count() FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)",
            parameters=base_params,
        )
        total_events = int(total_res.result_rows[0][0]) if total_res.result_rows else 0

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
                parameters={**base_params, "bl": _fmt_dt(baseline_end)},
            )
            baseline_size = int(bl_res.result_rows[0][0]) if bl_res.result_rows else 0

        all_findings: list[ValueFinding] = []

        for field_token in scan_fields:
            ctr: list[int] = [0]
            params: dict[str, Any] = {**base_params}
            col = _col_expr(field_token, params, ctr)

            if baseline_end is None:
                # Self-baseline: flag values with count ≤ rarity_floor.
                params["floor"] = rarity_floor
                params["lim"] = per_field_limit
                sql = f"""
                    SELECT
                        {col} AS val,
                        count() AS cnt,
                        min(timestamp) AS first_seen,
                        toString(argMin(event_id, timestamp)) AS evt_id,
                        argMin(source_id, timestamp) AS src_id,
                        argMin(message, timestamp) AS msg
                    FROM {db}.events
                    WHERE case_id = {{cid:String}}
                      AND has({{src:Array(String)}}, source_id)
                      AND {col} != ''
                    GROUP BY val
                    HAVING cnt <= {{floor:UInt32}}
                    ORDER BY cnt ASC, first_seen ASC
                    LIMIT {{lim:UInt32}}
                """
            else:
                # Temporal: flag values seen in detect window but not in baseline.
                params["bl"] = _fmt_dt(baseline_end)
                params["lim"] = per_field_limit
                sql = f"""
                    SELECT
                        {col} AS val,
                        countIf(timestamp >= {{bl:String}}) AS detect_cnt,
                        countIf(timestamp < {{bl:String}}) AS baseline_cnt,
                        minIf(timestamp, timestamp >= {{bl:String}}) AS first_seen,
                        toString(argMinIf(event_id, timestamp, timestamp >= {{bl:String}})) AS evt_id,
                        argMinIf(source_id, timestamp, timestamp >= {{bl:String}}) AS src_id,
                        argMinIf(message, timestamp, timestamp >= {{bl:String}}) AS msg
                    FROM {db}.events
                    WHERE case_id = {{cid:String}}
                      AND has({{src:Array(String)}}, source_id)
                      AND {col} != ''
                      AND timestamp IS NOT NULL
                    GROUP BY val
                    HAVING baseline_cnt = 0 AND detect_cnt > 0
                    ORDER BY detect_cnt ASC, first_seen ASC
                    LIMIT {{lim:UInt32}}
                """

            rows = self.ch.client.query(sql, parameters=params).result_rows

            for row in rows:
                if baseline_end is None:
                    val, cnt, first_seen, evt_id, src_id, msg = row
                    effective_cnt = int(cnt)
                else:
                    val, detect_cnt, _bl_cnt, first_seen, evt_id, src_id, msg = row
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
                first_seen_str = _ensure_utc(first_seen).isoformat() if first_seen else None
                evt_id_str = str(evt_id) if evt_id else None
                mini_event: dict[str, Any] | None = None
                if evt_id:
                    mini_event = {
                        "event_id": evt_id_str,
                        "case_id": case_id,
                        "source_id": str(src_id) if src_id else "",
                        "message": str(msg) if msg else "",
                        "timestamp": first_seen_str,
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
                        "vector_id": None,
                        "ingest_time": None,
                    }

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
                f for f in all_findings
                if not f.event_id or f.event_id not in exclude_event_ids
            ]

        # Sort by surprise descending (rarest first), apply global limit.
        all_findings.sort(key=lambda f: f.score, reverse=True)
        return StatAnomalyResult(
            status="ok",
            detector="value_novelty",
            method=method,
            baseline_size=baseline_size,
            results=all_findings[:limit],
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
            baseline_end = _ensure_utc(baseline_end)
        self.ch.init_schema()
        db = self.ch.database
        ctr: list[int] = [0]
        field_params: dict[str, Any] = {}
        col = _col_expr(series_field, field_params, ctr)

        src_params: dict[str, Any] = {"cid": case_id, "src": source_ids}

        # Resolve time range.
        range_res = self.ch.client.query(
            f"SELECT min(timestamp), max(timestamp) FROM {db}.events"
            f" WHERE case_id = {{cid:String}}"
            f" AND has({{src:Array(String)}}, source_id)"
            f" AND timestamp IS NOT NULL",
            parameters=src_params,
        )
        row0 = range_res.result_rows[0] if range_res.result_rows else (None, None)
        min_ts, max_ts = row0[0], row0[1]
        if min_ts is None or max_ts is None:
            return StatAnomalyResult(
                status="no_data",
                detector="frequency",
                method="z-score" if baseline_end is None else "temporal-z-score",
                baseline_size=0,
                z_threshold=z_threshold,
            )

        min_ts = _ensure_utc(min_ts)
        max_ts = _ensure_utc(max_ts)
        duration = (max_ts - min_ts).total_seconds()
        interval = max(1, int(duration / bucket_count))

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
              AND timestamp IS NOT NULL
              AND {col} != ''
            GROUP BY bucket, series_val
            ORDER BY bucket
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
            pts_aware = [(_ensure_utc(b), c) for b, c in pts]

            if baseline_end is not None:
                bl_pts = [(b, c) for b, c in pts_aware if b < baseline_end]
                detect_pts = [(b, c) for b, c in pts_aware if b >= baseline_end]
                if len(bl_pts) < _MIN_FREQUENCY_BUCKETS or not detect_pts:
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
                                series_field, sv, bucket_dt, interval, cnt,
                                mean_val, z, method,
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
                    var_loo = (total_sq - c * c - n_loo * mean_val * mean_val) / (
                        n_loo - 1
                    )
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
                                series_field, sv, bucket_dt, interval, int(cnt),
                                mean_val, z, method,
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

        # Sort by |z| descending, apply limit, hydrate representative events.
        findings.sort(key=lambda f: f.score, reverse=True)
        top = findings[:limit]
        top = self._hydrate_freq_findings(
            top, case_id, source_ids, col, db, field_params, interval
        )

        # Suppress findings whose representative event was marked normal.
        if exclude_event_ids:
            top = [
                f for f in top
                if not f.event_id or f.event_id not in exclude_event_ids
            ]

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
        """Fetch one representative event per frequency finding."""
        hydrated: list[FreqFinding] = []
        cols_sql = ", ".join(_EVENT_COLUMNS)
        for f in findings:
            params: dict[str, Any] = {
                **field_params,
                "cid": case_id,
                "src": source_ids,
                "sv": f.series_value,
                "ws": f.window_start[:19].replace("T", " "),
                "we": f.window_end[:19].replace("T", " "),
            }
            sql = f"""
                SELECT {cols_sql}
                FROM {db}.events
                WHERE case_id = {{cid:String}}
                  AND has({{src:Array(String)}}, source_id)
                  AND {col} = {{sv:String}}
                  AND timestamp >= {{ws:String}}
                  AND timestamp < {{we:String}}
                ORDER BY timestamp
                LIMIT 1
            """
            try:
                res = self.ch.client.query(sql, parameters=params)
                if res.result_rows:
                    evt = _row_to_event(_EVENT_COLUMNS, res.result_rows[0])
                    hydrated.append(
                        replace(f, event_id=str(evt.get("event_id", "")), event=evt)
                    )
                    continue
            except Exception:  # noqa: BLE001
                pass
            hydrated.append(f)
        return hydrated
