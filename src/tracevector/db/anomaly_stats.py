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
    value using z-score.  The same bucket math as the histogram endpoint is
    reused.  Score = |z|; windows above ``z_threshold`` are returned.

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

# Default fields scanned by value_novelty when no list is supplied.
_DEFAULT_NOVELTY_FIELDS = ["artifact", "timestamp_desc", "display_name"]

# Minimum buckets in a frequency series for z-scoring to be meaningful.
_MIN_FREQUENCY_BUCKETS = 3

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


def _row_to_event(columns: tuple[str, ...], row: tuple) -> dict[str, Any]:
    """Convert a ClickHouse result row into an Event-compatible dict."""
    d: dict[str, Any] = dict(zip(columns, row, strict=False))
    for key in ("timestamp", "ingest_time"):
        v = d.get(key)
        if v is not None and not isinstance(v, str):
            try:
                d[key] = v.isoformat()
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
# Service
# ---------------------------------------------------------------------------


class StatisticalAnomalyService:
    """Statistical anomaly detection over ClickHouse event fields.

    Operates purely on ingested events — no embeddings required.
    """

    def __init__(self, clickhouse: ClickHouseStore | None = None) -> None:
        self.ch = clickhouse or ClickHouseStore()

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
    ) -> StatAnomalyResult:
        """Return rare or first-seen values per field, ranked by surprise score.

        *fields* is a list of field tokens (``"artifact"``, ``"timestamp_desc"``,
        ``"attr:user_agent"``).  Defaults to ``["artifact", "timestamp_desc",
        "display_name"]`` when omitted.

        In *self-baseline* mode values appearing ≤ *rarity_floor* times are
        flagged.  In *temporal* mode (``baseline_end`` provided) any value absent
        from the baseline window but present in the detect window is flagged.
        """
        self.ch.init_schema()
        db = self.ch.database
        scan_fields = fields or _DEFAULT_NOVELTY_FIELDS

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
                        toString(min(timestamp)) AS first_seen,
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
                        toString(minIf(timestamp, timestamp >= {{bl:String}})) AS first_seen,
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
                first_seen_str = str(first_seen) if first_seen else None
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
            )

        # Build series dict: series_val → [(bucket_dt, cnt)].
        series: dict[str, list[tuple[Any, int]]] = defaultdict(list)
        for brow in brows:
            bucket, sv, cnt = brow
            if sv:
                series[sv].append((bucket, int(cnt)))

        baseline_size = sum(sum(c for _, c in pts) for pts in series.values())
        method = "z-score" if baseline_end is None else "temporal-z-score"

        # Z-score each bucket, collect anomalous windows.
        findings: list[FreqFinding] = []

        for sv, pts in series.items():
            pts_aware = [(_ensure_utc(b), c) for b, c in pts]

            if baseline_end is not None:
                bl_pts = [(b, c) for b, c in pts_aware if b < baseline_end]
                detect_pts = [(b, c) for b, c in pts_aware if b >= baseline_end]
                if len(bl_pts) < _MIN_FREQUENCY_BUCKETS or not detect_pts:
                    continue
                counts_bl = np.array([c for _, c in bl_pts], dtype=np.float64)
                mean_val = float(counts_bl.mean())
                std_val = float(counts_bl.std())
                score_pts = detect_pts
            else:
                if len(pts_aware) < _MIN_FREQUENCY_BUCKETS:
                    continue
                counts = np.array([c for _, c in pts_aware], dtype=np.float64)
                mean_val = float(counts.mean())
                std_val = float(counts.std())
                score_pts = pts_aware

            if std_val < 1e-9:
                continue  # Perfectly constant series — no z-score possible.

            for bucket_dt, cnt in score_pts:
                z = (cnt - mean_val) / std_val
                if abs(z) >= z_threshold:
                    window_end_dt = bucket_dt + timedelta(seconds=interval)
                    findings.append(
                        FreqFinding(
                            series_field=series_field,
                            series_value=str(sv),
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
                                "series_value": str(sv),
                                "window_start": bucket_dt.isoformat(),
                                "window_end": window_end_dt.isoformat(),
                                "observed": cnt,
                                "expected": round(mean_val, 2),
                                "z_score": round(z, 4),
                                "interval_seconds": interval,
                            },
                        )
                    )

        if not findings:
            return StatAnomalyResult(
                status="ok",
                detector="frequency",
                method=method,
                baseline_size=baseline_size,
                results=[],
            )

        # Sort by |z| descending, apply limit, hydrate representative events.
        findings.sort(key=lambda f: f.score, reverse=True)
        top = findings[:limit]
        top = self._hydrate_freq_findings(
            top, case_id, source_ids, col, db, field_params, interval
        )

        return StatAnomalyResult(
            status="ok",
            detector="frequency",
            method=method,
            baseline_size=baseline_size,
            results=top,
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
