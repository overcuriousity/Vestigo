"""Per-source field statistics cache (roadmap M15).

Sources are immutable after ingestion except for enrichment applies, which
mutate ``events.attributes`` (adding/stripping derived keys). Field
inventories therefore don't need a live full-scan ClickHouse aggregation on
every read: stats are computed once per source — right after ingestion and
again after each enrichment apply — cached in Postgres
(``source_field_stats``), and merged per timeline at read time.

Merge semantics across sources:

* ``coverage`` (non-empty counts) and ``events_total`` add exactly.
* ``distinct`` merges as **max-across-sources** — a documented approximation
  (union cardinality is unknowable from per-source counts without a sketch);
  acceptable because distinct only feeds UI hints and the novelty
  recommender's cardinality classification.
* Canonical ``field_mappings`` aggregates are NOT derivable from per-source
  caches (an event carrying several raw keys must be deduped exactly), so
  that small query stays live — see the callers in ``api/routers/events.py``.
* Per-field top values (``values``, M24a) merge by **summing counts across
  per-source top-N lists** — a documented approximation in the same
  acceptance class as the ``distinct`` max-merge: a value that ranks just
  below every source's per-source cutoff is under-counted (or missed)
  relative to a global scan. Single-source timelines are exact. Per-value
  counts that do survive are exact sums, and ``other_count`` is computed as
  the exact residual against the summed coverage.

Read paths are self-healing: a cache miss (pre-existing database, missed
trigger, ``STATS_VERSION`` bump) computes and stores the stats on the spot —
the miss path costs exactly what the previous always-live scan did.
"""

from __future__ import annotations

import asyncio
import zlib
from typing import TYPE_CHECKING, Any

from tracesignal.db._scan import HEAVY_SCAN_SETTINGS

# Top-level categorical columns tracked in the payload — single-sourced from
# the anomaly recommender, whose inventory this cache replaces.
from tracesignal.db.anomaly_stats import _NOVELTY_CANDIDATE_TOP_LEVEL
from tracesignal.db.field_mappings import apply_mappings_to_attribute_keys

if TYPE_CHECKING:
    from tracesignal.db.clickhouse import ClickHouseStore
    from tracesignal.db.postgres import PostgresStore

# Bump whenever the payload shape below changes — mismatched rows are treated
# as cache misses and recomputed, so no migration is ever needed.
STATS_VERSION = 2


def _effective_stats_version() -> int:
    """``STATS_VERSION`` folded with a fingerprint of ``_NOVELTY_CANDIDATE_TOP_LEVEL``.

    The cached payload's ``top_level`` keys come directly from that list, but
    it lives in ``anomaly_stats.py`` with no direct reference back to
    ``STATS_VERSION`` here — a maintainer adding/removing/reordering a column
    there could easily forget to bump ``STATS_VERSION``, leaving every
    already-cached source silently missing the new column until it happens to
    be re-ingested or re-enriched. Folding a fingerprint of the list into the
    version used for the cache-hit check makes that class of change
    self-invalidating instead of relying on the manual bump.
    """
    fingerprint = zlib.crc32("|".join(_NOVELTY_CANDIDATE_TOP_LEVEL).encode()) % 10_000_000
    return STATS_VERSION * 10_000_000 + fingerprint


EFFECTIVE_STATS_VERSION = _effective_stats_version()

# Per-source attribute-key cap, ordered by coverage descending. Bounds the
# payload on pathological datasets; must stay far above the read-side caps
# (the recommender/viz picker cap at 50) so list_fields keeps full-parity key
# unions on any realistic dataset.
_MAX_ATTR_KEYS_PER_SOURCE = 5000

# Sample values retained per attribute key (what field_coverage shows in the
# timeline wizard).
_SAMPLES_PER_FIELD = 3

# Top values (value → count) retained per field (M24a) — matches the
# field-terms endpoint's default limit so an unfiltered first chart load can
# be served entirely from this cache.
_TOP_VALUES_PER_FIELD = 50

# Attribute keys (top by coverage) that get a top-values list. Deliberately
# far above the viz field picker's 50-key cap but far below
# _MAX_ATTR_KEYS_PER_SOURCE, bounding the payload growth.
_VALUES_KEYS_PER_SOURCE = 200

# A field whose top-N contains any value longer than this gets NO values
# list at all (live fallback): cached values must be servable verbatim —
# truncation would fabricate values that don't exist in the data.
_MAX_VALUE_LEN = 256


def _attach_top_values(entries: dict[str, dict[str, Any]], rows: list[Any]) -> None:
    """Group ``(key, value, count)`` rows into per-field ``values`` lists.

    A field whose top-N contains any value longer than ``_MAX_VALUE_LEN``
    gets no ``values`` entry at all — cached values are served verbatim, so
    truncating would fabricate values that don't exist in the data.
    """
    per_field: dict[str, list[list[Any]]] = {}
    oversized: set[str] = set()
    for key, val, cnt in rows:
        if len(val) > _MAX_VALUE_LEN:
            oversized.add(key)
        per_field.setdefault(key, []).append([val, int(cnt)])
    for key, values in per_field.items():
        if key in oversized or key not in entries:
            continue
        entries[key]["values"] = values


def compute_source_field_stats(
    clickhouse: ClickHouseStore, case_id: str, source_id: str
) -> tuple[int, dict[str, Any]]:
    """Compute one source's field stats with four aggregation queries.

    Returns ``(events_total, payload)`` where payload is::

        {
          "top_level": {"artifact": {"distinct": 3, "coverage": 9000,
                                     "values": [["log:nginx", 9000], ...]}, ...},
          "attributes": {"src_ip": {"distinct": 41, "coverage": 8000,
                                    "samples": ["10.0.0.5", ...],
                                    "values": [["10.0.0.5", 4000], ...]}, ...},
          "attr_keys_truncated": false,
        }

    ``values`` (M24a) holds the top ``_TOP_VALUES_PER_FIELD`` values by count
    — present for every top-level column and for the top
    ``_VALUES_KEYS_PER_SOURCE`` attribute keys by coverage; absent for
    truncated keys and for fields with oversized values (see
    ``_attach_top_values``). ``attr_keys_truncated`` distinguishes "key
    absent ⇒ zero coverage in this source" (safe to merge) from "key possibly
    dropped by the ``_MAX_ATTR_KEYS_PER_SOURCE`` cap" (the read side must
    fall back to a live scan).

    Synchronous (blocking ClickHouse client) — run in a worker thread from
    async contexts.
    """
    clickhouse.init_schema()
    db = clickhouse.database
    params: dict[str, Any] = {"cid": case_id, "sid": source_id}
    where = "case_id = {cid:String} AND source_id = {sid:String}"

    total_res = clickhouse.client.query(
        f"SELECT count() FROM {db}.events WHERE {where}", parameters=params
    )
    total = int(total_res.result_rows[0][0]) if total_res.result_rows else 0
    payload: dict[str, Any] = {"top_level": {}, "attributes": {}, "attr_keys_truncated": False}
    if total == 0:
        return 0, payload

    agg_parts = [
        f"uniqExact({col}) AS {col}_dist, countIf({col} != '') AS {col}_cov"
        for col in _NOVELTY_CANDIDATE_TOP_LEVEL
    ]
    top_res = clickhouse.client.query(
        f"SELECT {', '.join(agg_parts)} FROM {db}.events WHERE {where} {HEAVY_SCAN_SETTINGS}",
        parameters=params,
    )
    if top_res.result_rows:
        row = top_res.result_rows[0]
        for i, col in enumerate(_NOVELTY_CANDIDATE_TOP_LEVEL):
            payload["top_level"][col] = {
                "distinct": int(row[i * 2]),
                "coverage": int(row[i * 2 + 1]),
            }

    attr_res = clickhouse.client.query(
        f"""
        SELECT
            k,
            uniqExact(v)                                    AS dist,
            countIf(v != '')                                AS cov,
            groupUniqArrayIf({_SAMPLES_PER_FIELD})(v, v != '') AS samples
        FROM {db}.events
        ARRAY JOIN mapKeys(attributes) AS k, mapValues(attributes) AS v
        WHERE {where}
        GROUP BY k
        ORDER BY cov DESC
        LIMIT {{max_keys:UInt32}}
        {HEAVY_SCAN_SETTINGS}
        """,
        parameters={**params, "max_keys": _MAX_ATTR_KEYS_PER_SOURCE},
    )
    for key, dist, cov, samples in attr_res.result_rows:
        payload["attributes"][key] = {
            "distinct": int(dist),
            "coverage": int(cov),
            "samples": list(samples),
        }
    payload["attr_keys_truncated"] = len(payload["attributes"]) >= _MAX_ATTR_KEYS_PER_SOURCE

    # Top values per top-level column, one scan via tuple unpivot. Ordering
    # `count DESC, value ASC` matches _field_terms_impl exactly so a cached
    # merge reproduces the live ranking.
    unpivot = ", ".join(f"('{col}', {col})" for col in _NOVELTY_CANDIDATE_TOP_LEVEL)
    top_values_res = clickhouse.client.query(
        f"""
        SELECT t.1 AS k, t.2 AS v, count() AS c
        FROM {db}.events
        ARRAY JOIN [{unpivot}] AS t
        WHERE {where} AND t.2 != ''
        GROUP BY k, v
        ORDER BY k, c DESC, v ASC
        LIMIT {{n_vals:UInt32}} BY k
        {HEAVY_SCAN_SETTINGS}
        """,
        parameters={**params, "n_vals": _TOP_VALUES_PER_FIELD},
    )
    _attach_top_values(payload["top_level"], top_values_res.result_rows)

    # Top values for the highest-coverage attribute keys, memory-safe
    # LIMIT n BY key pattern (see anomaly_stats._batched_attr_novelty_rows).
    value_keys = sorted(payload["attributes"], key=lambda k: -payload["attributes"][k]["coverage"])
    value_keys = value_keys[:_VALUES_KEYS_PER_SOURCE]
    if value_keys:
        attr_values_res = clickhouse.client.query(
            f"""
            SELECT k, v, count() AS c
            FROM {db}.events
            ARRAY JOIN mapKeys(attributes) AS k, mapValues(attributes) AS v
            WHERE {where} AND has({{vkeys:Array(String)}}, k) AND v != ''
            GROUP BY k, v
            ORDER BY k, c DESC, v ASC
            LIMIT {{n_vals:UInt32}} BY k
            {HEAVY_SCAN_SETTINGS}
            """,
            parameters={**params, "vkeys": value_keys, "n_vals": _TOP_VALUES_PER_FIELD},
        )
        _attach_top_values(payload["attributes"], attr_values_res.result_rows)
    return total, payload


async def refresh_source_field_stats(
    store: PostgresStore, clickhouse: ClickHouseStore, case_id: str, source_id: str
) -> None:
    """Compute and upsert one source's stats (post-ingest / post-enrichment hook)."""
    total, payload = await asyncio.to_thread(
        compute_source_field_stats, clickhouse, case_id, source_id
    )
    await store.upsert_source_field_stats(
        case_id=case_id,
        source_id=source_id,
        stats_version=EFFECTIVE_STATS_VERSION,
        events_total=total,
        payload=payload,
    )


async def ensure_source_field_stats(
    store: PostgresStore,
    clickhouse: ClickHouseStore,
    case_id: str,
    source_ids: list[str],
) -> dict[str, tuple[int, dict[str, Any]]]:
    """Return ``{source_id: (events_total, payload)}``, computing misses on the spot.

    The self-healing read path: cached rows at the current ``STATS_VERSION``
    are used as-is; anything else is computed synchronously (worker thread)
    and persisted, so pre-existing databases converge to cached reads.
    """
    stats: dict[str, tuple[int, dict[str, Any]]] = {}
    for row in await store.get_source_field_stats(source_ids):
        if row.stats_version == EFFECTIVE_STATS_VERSION:
            stats[row.source_id] = (row.events_total, row.payload)

    async def _fill_miss(source_id: str) -> None:
        total, payload = await asyncio.to_thread(
            compute_source_field_stats, clickhouse, case_id, source_id
        )
        await store.upsert_source_field_stats(
            case_id=case_id,
            source_id=source_id,
            stats_version=EFFECTIVE_STATS_VERSION,
            events_total=total,
            payload=payload,
        )
        stats[source_id] = (total, payload)

    misses = [source_id for source_id in source_ids if source_id not in stats]
    if misses:
        await asyncio.gather(*(_fill_miss(source_id) for source_id in misses))
    return stats


def merged_list_fields(
    stats: dict[str, tuple[int, dict[str, Any]]],
    field_mappings: dict[str, list[str]] | None = None,
) -> dict[str, Any]:
    """Cached equivalent of ``EventQueryService.list_fields`` (same shape)."""
    # Import here, not at module top: queries.py is a heavy module and this
    # avoids widening the import graph for the compute-only callers.
    from tracesignal.db.queries import TOP_LEVEL_DISPLAY_COLUMNS

    raw_keys: set[str] = set()
    for _, payload in stats.values():
        raw_keys.update(payload.get("attributes", {}))
    keys, provenance = apply_mappings_to_attribute_keys(sorted(raw_keys), field_mappings)
    return {
        "top_level": TOP_LEVEL_DISPLAY_COLUMNS,
        "attributes": sorted(keys),
        "mapped": provenance,
    }


def merged_inventory(
    stats: dict[str, tuple[int, dict[str, Any]]],
    field_mappings: dict[str, list[str]] | None = None,
    max_attr_keys: int = 50,
) -> tuple[list[tuple[str, int, int]], int]:
    """Cached equivalent of ``StatisticalAnomalyService.field_inventory``.

    Same output contract: ``([(token, distinct, coverage), ...], total)``,
    top-level columns first in candidate order, then ``attr:<key>`` tokens by
    coverage descending (capped at *max_attr_keys*, mirroring the live scan's
    LIMIT). ``distinct`` is max-across-sources (see module docstring). Keys
    replaced by a canonical mapping are skipped — the caller appends the
    canonical entries from the live coalesce aggregation.
    """
    total = sum(t for t, _ in stats.values())
    if total == 0:
        return [], 0

    inventory: list[tuple[str, int, int]] = []
    for col in _NOVELTY_CANDIDATE_TOP_LEVEL:
        cov = sum(p.get("top_level", {}).get(col, {}).get("coverage", 0) for _, p in stats.values())
        dist = max(
            (p.get("top_level", {}).get(col, {}).get("distinct", 0) for _, p in stats.values()),
            default=0,
        )
        inventory.append((col, dist, cov))

    mapped_raws = {r for raws in (field_mappings or {}).values() for r in raws}
    merged_attrs: dict[str, tuple[int, int]] = {}
    for _, payload in stats.values():
        for key, entry in payload.get("attributes", {}).items():
            if key in mapped_raws:
                continue
            dist, cov = merged_attrs.get(key, (0, 0))
            merged_attrs[key] = (
                max(dist, int(entry.get("distinct", 0))),
                cov + int(entry.get("coverage", 0)),
            )
    ranked = sorted(merged_attrs.items(), key=lambda kv: -kv[1][1])[:max_attr_keys]
    inventory.extend((f"attr:{key}", dist, cov) for key, (dist, cov) in ranked)
    return inventory, total


def merged_field_terms(
    stats: dict[str, tuple[int, dict[str, Any]]],
    field_token: str,
    limit: int = 50,
) -> dict[str, Any] | None:
    """Cached equivalent of ``EventQueryService.field_terms`` for unfiltered queries.

    Returns the exact ``field_terms`` response shape, or ``None`` when the
    cache cannot answer honestly and the caller must fall back to a live
    scan: *limit* exceeds the cached per-field top-N, any covering source
    lacks a ``values`` list (oversized values, pre-M24a payload), or the
    token is an attribute key absent from a source whose key list was
    truncated. ``total``/per-value counts are exact sums; ``distinct`` is
    max-across-sources and the top-N merge is approximate (module docstring).
    """
    if limit > _TOP_VALUES_PER_FIELD:
        return None
    is_attr = field_token.startswith("attr:")
    key = field_token.removeprefix("attr:")
    if not is_attr and key not in _NOVELTY_CANDIDATE_TOP_LEVEL:
        return None

    total_cov = 0
    distinct = 0
    counts: dict[str, int] = {}
    for _, payload in stats.values():
        if is_attr:
            entry = payload.get("attributes", {}).get(key)
            if entry is None:
                if payload.get("attr_keys_truncated"):
                    return None
                continue
        else:
            entry = payload.get("top_level", {}).get(key)
            if entry is None:
                continue
        cov = int(entry.get("coverage", 0))
        if cov <= 0:
            continue
        values = entry.get("values")
        if values is None:
            return None
        total_cov += cov
        distinct = max(distinct, int(entry.get("distinct", 0)))
        for val, cnt in values:
            counts[val] = counts.get(val, 0) + int(cnt)

    if total_cov == 0:
        return {"field": field_token, "total": 0, "distinct": 0, "values": [], "other_count": 0}
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:limit]
    top = [{"value": val, "count": cnt} for val, cnt in ranked]
    other = total_cov - sum(cnt for _, cnt in ranked)
    return {
        "field": field_token,
        "total": total_cov,
        "distinct": distinct,
        "values": top,
        "other_count": max(0, other),
    }


def merged_field_coverage(stats: dict[str, tuple[int, dict[str, Any]]]) -> dict[str, Any]:
    """Cached equivalent of ``EventQueryService.field_coverage`` (same shape).

    Counts are now exact per-source non-empty totals rather than the previous
    20k-rows-per-source sample.
    """
    fields: dict[str, list[dict[str, Any]]] = {}
    for source_id in sorted(stats):
        _, payload = stats[source_id]
        for key, entry in payload.get("attributes", {}).items():
            if int(entry.get("coverage", 0)) <= 0:
                continue
            fields.setdefault(key, []).append(
                {
                    "source_id": source_id,
                    "count": int(entry.get("coverage", 0)),
                    "samples": list(entry.get("samples", []))[:_SAMPLES_PER_FIELD],
                }
            )
    return {
        "fields": [
            {"key": key, "sources": per_source} for key, per_source in sorted(fields.items())
        ],
    }
