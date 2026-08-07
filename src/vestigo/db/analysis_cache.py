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
from typing import TYPE_CHECKING, Any

from sqlalchemy import delete, select

from vestigo.db.postgres import AnalysisCache, SourceFieldStats, generate_id

if TYPE_CHECKING:
    from vestigo.db.postgres import PostgresStore


def fingerprint(
    *,
    timeline_id: str,
    source_hashes: list[str],
    enrichment_generation: str,
    frame: str,
    baseline_id: str | None,
    method: str,
    params: dict[str, Any],
    dispositions_hash: str,
) -> str:
    """Return the cache key for one method run under one scope over one dataset.

    Source hashes are sorted and the JSON is key-sorted, so the key depends on
    the *content* of the inputs and not on the order a caller happened to
    assemble them in.
    """
    material = json.dumps(
        {
            "timeline_id": timeline_id,
            "source_hashes": sorted(source_hashes),
            "enrichment_generation": enrichment_generation,
            "frame": frame,
            "baseline_id": baseline_id,
            "method": method,
            "params": params,
            "dispositions_hash": dispositions_hash,
        },
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(material.encode()).hexdigest()


async def enrichment_generation(store: PostgresStore, source_ids: list[str]) -> str:
    """Return a token that changes whenever any source's attributes could have.

    Derived from :class:`SourceFieldStats`: ingestion and enrichment apply are
    the only two paths that mutate ``events.attributes``, and both already
    refresh that row. Reusing it means no new bookkeeping and no second
    invalidation path to keep in sync with the first.
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
    material = sorted(
        f"{source_id}:{version}:{computed_at.isoformat() if computed_at else ''}"
        for source_id, computed_at, version in rows
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
    """
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
        else:
            session.add(
                AnalysisCache(
                    id=generate_id("cache"), case_id=case_id, cache_key=key, payload=payload
                )
            )
        await session.flush()

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
