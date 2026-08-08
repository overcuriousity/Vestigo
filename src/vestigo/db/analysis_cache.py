"""Fingerprint-keyed memoization for analysis results.

The key is a hash of every input that can change an answer: which timeline,
which sources (by their content hash), which enrichment generation, which
scope, which method, which parameters, and which detection-affecting analyst
verdicts. Sources are immutable after ingestion, so identical inputs cannot
describe different data — a hit is therefore provably the same answer, and
there is no staleness heuristic anywhere in the system. Deliberately no TTL:
a TTL would answer "is this recent?", which is not the question.

``dispositions_hash`` is in the key because ``kind="normal"`` verdicts are
detection-affecting (see :class:`FindingDisposition`): marking a value normal
must invalidate the cached findings that would otherwise keep showing it.

Every row here is derived data. Eviction costs a rescan and nothing else,
which is why it needs no policy beyond a per-case bound.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from vestigo.db.postgres import AnalysisCache, SourceEnrichment, SourceFieldStats, generate_id

if TYPE_CHECKING:
    from vestigo.db.postgres import PostgresStore


def fingerprint(
    *,
    timeline_id: str,
    source_hashes: list[str],
    enrichment_generation: str,
    frame: str,
    baseline_id: str | None,
    baseline_config_hash: str | None,
    field_mappings: dict[str, list[str]] | None,
    source_offsets: dict[str, int] | None,
    detector_settings: dict[str, Any],
    method: str,
    params: dict[str, Any],
    limit: int,
    dispositions_hash: str,
) -> str:
    """Return the cache key for one method run under one scope over one dataset.

    Source hashes are sorted and the JSON is key-sorted, so the key depends on
    the *content* of the inputs and not on the order a caller happened to
    assemble them in.

    ``limit`` is in the key because the runners truncate to it: a payload
    computed at 50 rows is not the answer to a request for 500, and serving it
    as a hit would assert a completeness it does not have.

    Four inputs are here for the same reason and are easy to forget, because
    none of them is a request parameter:

    - ``baseline_config_hash`` — a definition is edited *in place*, keeping its
      id. Keying on the id alone would serve the pre-edit answer for a window
      the analyst has since moved.
    - ``field_mappings`` — every detector resolves canonical field aliases
      through them, so remapping a field changes what was scanned.
    - ``source_offsets`` — a declared per-source clock-skew correction shifts
      every timestamp the temporal detectors bucket by.
    - ``detector_settings`` — the runtime-editable thresholds the runners fall
      back to whenever a knob is omitted. An admin lowering ``stat_z_threshold``
      in the console changes every default-parameter answer in the system.
    """
    material = json.dumps(
        {
            "timeline_id": timeline_id,
            "source_hashes": sorted(source_hashes),
            "enrichment_generation": enrichment_generation,
            "frame": frame,
            "baseline_id": baseline_id,
            "baseline_config_hash": baseline_config_hash,
            "field_mappings": {k: sorted(v) for k, v in sorted((field_mappings or {}).items())},
            "source_offsets": dict(sorted((source_offsets or {}).items())),
            "detector_settings": detector_settings,
            "method": method,
            "params": params,
            "limit": limit,
            "dispositions_hash": dispositions_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


#: Settings-field prefixes whose values the detectors read as knob fallbacks.
#: ``stat_scan_*`` is deliberately excluded: those tune ClickHouse's resource
#: budget for the scan, not what the scan concludes.
_DETECTOR_SETTING_PREFIXES = ("stat_",)
_DETECTOR_SETTING_EXCLUDED = ("stat_scan_",)


def detector_settings(settings: Any) -> dict[str, Any]:
    """The detector-affecting subset of the resolved settings, as key material.

    Taken as a prefix sweep rather than an enumerated list: a new ``stat_*``
    threshold added without touching this file must still invalidate the cache,
    and the failure mode of forgetting one is a wrong answer served as proof.
    """
    return {
        name: getattr(settings, name)
        for name in sorted(type(settings).model_fields)
        if name.startswith(_DETECTOR_SETTING_PREFIXES)
        and not name.startswith(_DETECTOR_SETTING_EXCLUDED)
    }


async def enrichment_generation(store: PostgresStore, source_ids: list[str]) -> str:
    """Return a token that changes whenever any source's attributes could have.

    Derived from :class:`SourceFieldStats`: ingestion and enrichment apply are
    the only two paths that mutate ``events.attributes``, and both already
    refresh that row. Reusing it means no new bookkeeping and no second
    invalidation path to keep in sync with the first.

    :class:`SourceEnrichment` is folded in because the stats row alone has a
    state it cannot distinguish: *absent*. ``enrichers/jobs.py`` drops the row
    when the post-apply refresh fails, so two successive enrichers that both
    fail their refresh leave the identical "no row" material while the
    attributes underneath differ — a hit served for data computed before the
    second enricher existed, under a contract that says a hit is proof. The
    enrichment provenance row is upserted per ``(source_id, enricher_key)``
    with its own ``applied_at``/``job_id``, so it separates those two states
    and does so durably, independent of whether the refresh succeeded.
    """
    if not source_ids:
        return "empty"
    async with store.session_factory() as session:
        rows = (
            await session.execute(
                select(
                    SourceFieldStats.source_id,
                    SourceFieldStats.computed_at,
                    SourceFieldStats.stats_version,
                ).where(SourceFieldStats.source_id.in_(source_ids))
            )
        ).all()
        applies = (
            await session.execute(
                select(
                    SourceEnrichment.source_id,
                    SourceEnrichment.enricher_key,
                    SourceEnrichment.enricher_config_hash,
                    SourceEnrichment.job_id,
                    SourceEnrichment.applied_at,
                ).where(SourceEnrichment.source_id.in_(source_ids))
            )
        ).all()
    material = sorted(
        f"{source_id}:{version}:{computed_at.isoformat() if computed_at else ''}"
        for source_id, computed_at, version in rows
    )
    material += sorted(
        f"{source_id}:{key}:{config_hash}:{job_id}:{applied_at.isoformat() if applied_at else ''}"
        for source_id, key, config_hash, job_id, applied_at in applies
    )
    return hashlib.sha256("|".join(material).encode()).hexdigest()[:32]


async def cache_get(store: PostgresStore, case_id: str, key: str) -> dict[str, Any] | None:
    """Return the cached payload for *key* within *case_id*, or None on a miss."""
    async with store.session_factory() as session:
        row = (
            await session.execute(
                select(AnalysisCache).where(
                    AnalysisCache.case_id == case_id, AnalysisCache.cache_key == key
                )
            )
        ).scalar_one_or_none()
        return dict(row.payload) if row is not None else None


async def cache_put(
    store: PostgresStore, case_id: str, key: str, payload: dict[str, Any], max_rows: int
) -> None:
    """Store *payload* under *key*, then evict this case down to *max_rows*.

    Eviction is scoped to the writing case: one busy investigation must not
    evict a quiet one's answers, since the two share nothing but a table.

    It is least-recently-**computed**, not least-recently-used. ``cache_get``
    deliberately writes nothing: ``/analysis/findings`` is a ``require_case_read``
    endpoint whose single write exception (see CLAUDE.md) is the miss path, and
    turning every hit into a write would trade the whole point of the hit path
    for a better eviction order. The consequence is bounded — a hot entry that
    was computed long ago can be evicted ahead of a cold one computed recently,
    and it costs exactly one rescan. Every row is derived data keyed by a
    fingerprint of its own inputs, so eviction can never produce a wrong answer,
    only a slower one.

    Losing the insert race is not an error. Two clients that miss the same key
    computed the same answer over the same inputs — that is what the key means —
    so the loser's ``IntegrityError`` is swallowed rather than allowed to fail a
    request whose scan already succeeded. Failing there would report a method as
    unrunnable because a *cache write* collided.
    """
    try:
        await _cache_put(store, case_id, key, payload, max_rows)
    except IntegrityError:
        return


async def _cache_put(
    store: PostgresStore, case_id: str, key: str, payload: dict[str, Any], max_rows: int
) -> None:
    async with store.session_factory() as session:
        existing = (
            await session.execute(
                select(AnalysisCache).where(
                    AnalysisCache.case_id == case_id, AnalysisCache.cache_key == key
                )
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.payload = payload
            # Eviction keeps the newest `computed_at`, so a recomputed row that
            # kept its original stamp would age while it is still being answered
            # from, and be evicted ahead of rows nobody has recomputed since.
            existing.computed_at = datetime.now(UTC)
            await session.commit()
            # A replace cannot grow the table, so it can never push the case
            # over the cap. Running eviction here was pure waste on a path that
            # already holds the row it just wrote.
            return

        session.add(
            AnalysisCache(id=generate_id("cache"), case_id=case_id, cache_key=key, payload=payload)
        )
        await session.flush()

        # Only an insert can exceed the cap, and only then is the delete worth
        # its cost: materializing up to `max_rows` ids and issuing a `NOT IN`
        # over them on every single miss is a lot of work to discover there is
        # nothing to evict.
        rows = (
            await session.execute(
                select(func.count())
                .select_from(AnalysisCache)
                .where(AnalysisCache.case_id == case_id)
            )
        ).scalar_one()
        if rows > max_rows:
            keep = (
                (
                    await session.execute(
                        select(AnalysisCache.id)
                        .where(AnalysisCache.case_id == case_id)
                        .order_by(AnalysisCache.computed_at.desc(), AnalysisCache.id.desc())
                        .limit(max_rows)
                    )
                )
                .scalars()
                .all()
            )
            await session.execute(
                delete(AnalysisCache).where(
                    AnalysisCache.case_id == case_id, AnalysisCache.id.notin_(keep)
                )
            )
        await session.commit()
