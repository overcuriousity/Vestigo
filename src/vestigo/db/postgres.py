"""PostgreSQL connection and metadata models."""

from __future__ import annotations

import logging
import uuid
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    delete,
    func,
    insert,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from vestigo.core.config import get_settings

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for Vestigo metadata."""


class Case(Base):
    """An investigation case."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # Owning user (creator). Nullable only for pre-auth rows on a dev DB that
    # predates this feature; every case created through the API now gets one.
    owner_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # Investigation team this case belongs to, or None for a personal case
    # (visible only to its owner and admins).
    team_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "owner_id": self.owner_id,
            "team_id": self.team_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Source(Base):
    """One ingested file in a case.

    The Source is the atomic unit of forensic provenance and immutability.
    Events and vectors are scoped by ``source_id`` so a Source can be reused
    across multiple Timelines without duplicating data.
    """

    __tablename__ = "sources"
    __table_args__ = (Index("ix_sources_case_id_file_hash", "case_id", "file_hash", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    parser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_count: Mapped[int] = mapped_column(default=0)
    vector_count: Mapped[int] = mapped_column(default=0)
    # Ingest lifecycle: "ingesting" while the background upload job is still
    # writing events to ClickHouse, "ready" once complete. Timeline scope
    # resolution (events/_resolve_timeline_scope) excludes non-ready sources
    # so analysts never query — and detectors never baseline on — a
    # half-ingested file. There is no persisted "failed" state: a failed
    # ingest deletes its partial events and this row so the upload can be
    # retried (the duplicate check is keyed on file_hash), and startup
    # reconciliation does the same for rows orphaned by a mid-ingest restart.
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="ready", server_default="ready"
    )
    # Analyst-declared clock-skew correction (W2), in seconds. Applied at
    # QUERY TIME to this source's event timestamps across explorer, histogram,
    # detectors and exports — the ingested events are never mutated (evidence
    # stays raw; the offset is declared metadata that appears in the audit
    # trail and export output). A compromised/misconfigured host whose clock
    # drifts would otherwise lie in the merged master timeline. Positive shifts
    # this source's events later. See db/queries.py::effective_ts_expr and
    # docs/ROADMAP.md W2.
    time_offset_seconds: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )

    @property
    def is_ready(self) -> bool:
        """Whether this source has finished ingestion and is queryable.

        The single predicate every caller must use instead of comparing
        ``status`` inline — see ``events._resolve_timeline_scope``.
        """
        return self.status == "ready"

    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    timelines: Mapped[list[Timeline]] = relationship(
        "Timeline", secondary="timeline_sources", back_populates="sources"
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "name": self.name,
            "description": self.description,
            "filename": self.filename,
            "file_hash": self.file_hash,
            "size_bytes": self.size_bytes,
            "parser": self.parser,
            "parser_version": self.parser_version,
            "event_count": self.event_count,
            "vector_count": self.vector_count,
            "status": self.status,
            "time_offset_seconds": self.time_offset_seconds,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class Timeline(Base):
    """A named grouping of Sources within a case.

    The default Timeline (``is_default=True``) automatically contains every
    Source uploaded to the case. Custom Timelines are analyst-defined subsets.

    Embedding state is tracked on the Timeline, not on individual Sources.  A
    timeline is *embedded* when ``embedding_config`` is set.  It becomes *stale*
    when its current ``source_ids`` differ from ``embedded_source_ids`` — this
    is derived at serialisation time so no explicit flag-update is needed when
    sources are added or removed.
    """

    __tablename__ = "timelines"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    # Field mappings (issue #10): canonical field name → ordered raw attribute
    # keys, applied at query time (db/field_mappings.py). Pure metadata — the
    # ingested events are never rewritten; None/empty means no mapping.
    field_mappings: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    # --- Embedding state (all nullable; None → not yet embedded) -------------
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    embedding_config_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Snapshot of source_ids at embed time; used to derive staleness.
    embedded_source_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    sources: Mapped[list[Source]] = relationship(
        "Source", secondary="timeline_sources", back_populates="timelines"
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        current_ids = sorted(s.id for s in self.sources)
        embedded_ids = sorted(self.embedded_source_ids or [])
        is_embedded = self.embedding_config is not None
        is_stale = is_embedded and current_ids != embedded_ids
        return {
            "id": self.id,
            "case_id": self.case_id,
            "name": self.name,
            "description": self.description,
            "is_default": self.is_default,
            "source_ids": [s.id for s in self.sources],
            "field_mappings": self.field_mappings,
            "is_embedded": is_embedded,
            "is_stale": is_stale,
            "embedding_model": self.embedding_model,
            "embedding_config": self.embedding_config,
            "embedding_config_hash": self.embedding_config_hash,
            "embedded_source_ids": self.embedded_source_ids,
            "embedded_at": self.embedded_at.isoformat() if self.embedded_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TimelineSource(Base):
    """Many-to-many join between Timelines and Sources."""

    __tablename__ = "timeline_sources"

    timeline_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("timelines.id", ondelete="CASCADE"), primary_key=True
    )
    source_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("sources.id", ondelete="CASCADE"), primary_key=True
    )


class TimelineEnricher(Base):
    """Per-timeline enricher configuration: which enrichers run, and how.

    ``mode`` controls whether this enricher fires automatically after
    ingestion succeeds for a source belonging to this timeline, or only when
    an analyst manually triggers it. Mirrors the ``timeline_sources`` join
    table in spirit, but carries config rather than being a pure M:N link.
    """

    __tablename__ = "timeline_enrichers"
    __table_args__ = (
        Index("ix_timeline_enrichers_unique", "timeline_id", "enricher_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timeline_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("timelines.id", ondelete="CASCADE"), nullable=False, index=True
    )
    enricher_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(16), nullable=False, default="automatic")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        return {
            "id": self.id,
            "timeline_id": self.timeline_id,
            "enricher_key": self.enricher_key,
            "mode": self.mode,
            "enabled": self.enabled,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class EnricherGlobalConfig(Base):
    """Instance-wide enricher defaults, set by admins.

    ``auto_run_default`` makes an enricher run automatically after ingestion
    for every timeline that has *no explicit* ``timeline_enrichers`` row for
    it — an explicit per-timeline config always overrides this default. This
    lets an admin turn on e.g. GeoIP for the whole instance without touching
    each timeline.
    """

    __tablename__ = "enricher_global_configs"

    enricher_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    auto_run_default: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        return {
            "enricher_key": self.enricher_key,
            "auto_run_default": self.auto_run_default,
            "updated_by": self.updated_by,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class EnrichmentResultStaging(Base):
    """Crash-safe staging area for in-flight enrichment jobs.

    Rows accumulate here as an enrichment job processes batches; at job end
    they are merged into the ClickHouse ``events.attributes`` map via an
    atomic per-source partition rewrite (``ClickHouseStore.finalize_enrichment_apply``)
    and deleted only after the swap succeeds. If the process dies mid-run,
    rows survive here (Postgres is transactional) even though the in-memory
    JobStore does not — see ``list_orphaned_enrichment_job_runs``, which
    mirrors ``list_ingesting_sources`` for reconciliation on restart.

    One row per ``(job, event)``: everything an enricher derived for one
    event is a single ``fields`` JSON map (``field_key -> value``, keys
    already attr-prefixed via ``derived_field_key``). Replaces the original
    row-per-(event, attr, output_field) grain — ~3-6x fewer rows for
    multi-output enrichers, and the apply loop expands the map back into
    triples for ``stage_enrichment_rows`` without any ClickHouse-side change.
    """

    __tablename__ = "enrichment_results_staging"
    __table_args__ = (
        Index("ix_staging_job_id", "job_id"),
        Index("ix_staging_unique_row", "job_id", "event_id", unique=True),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enricher_key: Mapped[str] = mapped_column(String(128), nullable=False)
    fields: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    enricher_config_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, default="", server_default=""
    )


class EnrichmentJobRun(Base):
    """Durable marker for an in-flight enrichment job.

    The Postgres-side complement to the ephemeral JobStore (``core/jobs.py``):
    written when a job starts, deleted only after its final ClickHouse flush
    succeeds. A row still present at startup means the process died mid-run —
    the same signal ``Source.status == "ingesting"`` gives
    ``list_ingesting_sources`` for orphaned ingest jobs.
    """

    __tablename__ = "enrichment_job_runs"

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enricher_key: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    # Source ids whose staging fully completed, appended as the run progresses
    # (``mark_enrichment_source_staged``). This is what lets crash recovery
    # record provenance for finished sources instead of re-enriching the whole
    # job: only sources listed here may get a ``SourceEnrichment`` row when the
    # staged results are applied after a crash.
    completed_source_ids: Mapped[list] = mapped_column(
        JSON, nullable=False, default=list, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class SourceEnrichment(Base):
    """Durable provenance for enrichment applied to a source's events.

    Enrichment output is merged directly into the ClickHouse ``events``
    table's ``attributes`` map (atomic per-source partition rewrite), so the
    per-row provenance a separate results table would give does not exist —
    this row records which enricher configuration/data version produced the
    derived fields currently present on a source. Re-applying with a new
    config overwrites the same keys and upserts this row.
    """

    __tablename__ = "source_enrichments"
    __table_args__ = (
        Index("ix_source_enrichments_unique", "source_id", "enricher_key", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False)
    enricher_key: Mapped[str] = mapped_column(String(128), nullable=False)
    enricher_config_hash: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    job_id: Mapped[str] = mapped_column(String(64), nullable=False)
    rows_applied: Mapped[int] = mapped_column(nullable=False, default=0)
    applied_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Serialize for API responses."""
        return {
            "case_id": self.case_id,
            "source_id": self.source_id,
            "timeline_id": self.timeline_id,
            "enricher_key": self.enricher_key,
            "enricher_config_hash": self.enricher_config_hash,
            "job_id": self.job_id,
            "rows_applied": self.rows_applied,
            "applied_at": self.applied_at.isoformat() if self.applied_at else None,
        }


class SourceFieldStats(Base):
    """Cached per-source field statistics (roadmap M15).

    Derived, recomputable data — computed from the immutable ClickHouse
    events of one source after ingestion and refreshed after each enrichment
    apply (the only mutation path for ``events.attributes``). ``payload``
    shape is versioned via ``stats_version``: a mismatch is treated as a
    cache miss and recomputed, never migrated. See ``db/field_stats.py``.
    """

    __tablename__ = "source_field_stats"
    __table_args__ = (Index("ix_source_field_stats_source", "source_id", unique=True),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    stats_version: Mapped[int] = mapped_column(nullable=False, default=1)
    events_total: Mapped[int] = mapped_column(nullable=False, default=0)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    computed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )


class View(Base):
    """A saved filter view within a case."""

    __tablename__ = "views"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    query: Mapped[str] = mapped_column(String(4096), nullable=False, default="")
    view_filter: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary for the SavedView API response."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "name": self.name,
            "query": self.query,
            "filter": self.view_filter or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class SavedChart(Base):
    """A saved Visualization-page chart, scoped to a timeline.

    ``config`` holds the full versioned frontend ``ChartConfig`` (chart type,
    field, scale, metric, comparison layer, per-chart options) as opaque
    JSON — the backend never interprets it, it only round-trips it, exactly
    like ``View.view_filter``. The ``v`` version key inside lets a future
    frontend detect configs saved by an older shape instead of misreading
    them.
    """

    __tablename__ = "saved_charts"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    config: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary for the SavedChart API response."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "timeline_id": self.timeline_id,
            "name": self.name,
            "config": self.config or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


def _windows_config_hash(payload: dict[str, Any]) -> str:
    """SHA-256 over the canonical JSON of a window payload.

    Same canonicalization convention as ``models/event.py`` config hashes —
    derived, never stored, so an edited definition can't desync from it.
    """
    import hashlib
    import json

    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class BaselineDefinition(Base):
    """A named baseline + suspect-window definition for temporal anomaly detection.

    Timeline-scoped: one baseline time range (the "known-normal" reference
    period) plus 1..N labeled suspect windows (the ranges under
    investigation). Detectors resolve a ``baseline_id`` to this row at scan
    time; forensic reproducibility does **not** depend on this row surviving —
    every ``DetectorRun`` snapshots the resolved windows into its ``params``,
    so definitions stay freely editable/deletable (see ``windows_hash`` in
    ``api/routers/events.py::_persist_detector_run``).

    ``suspect_windows`` is a JSON list of ``{"id", "label", "start", "end"}``
    with ISO-8601 UTC timestamps; window semantics are half-open
    ``[start, end)`` everywhere (matching ``anomaly_stats.TimeWindow``).
    """

    __tablename__ = "baseline_definitions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    baseline_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    baseline_end: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    suspect_windows: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def windows_payload(self) -> dict[str, Any]:
        """The window ranges alone — the hash input and DetectorRun snapshot shape."""
        return {
            "baseline": {
                "start": self.baseline_start.isoformat() if self.baseline_start else None,
                "end": self.baseline_end.isoformat() if self.baseline_end else None,
            },
            "suspect_windows": self.suspect_windows or [],
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary, including the derived ``config_hash``."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "timeline_id": self.timeline_id,
            "name": self.name,
            **self.windows_payload(),
            "config_hash": _windows_config_hash(self.windows_payload()),
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class FindingDisposition(Base):
    """A single analyst verdict on an anomaly finding — the unified taxonomy.

    One table replaces the previously fragmented mechanisms (the
    ``detector_allowlist`` table, the per-event ``normal`` annotation, and the
    ``pinned`` flag on system annotations). ``kind`` carries the verdict:

    - ``normal`` — the behavior is expected; extends the baseline. Suppresses
      detection (value scope: the ``(field, value)`` pair is dropped
      post-detection on every event; event scope: the event is excluded from
      scans). Detection-affecting, therefore hashed into ``DetectorRun.params``
      via :func:`dispositions_hash`.
    - ``dismissed`` — noise for this investigation; presentation-only.
      Detectors keep scoring, the finding is filtered at response
      serialization with an explicit ``dismissed_count`` (never silently),
      and it does **not** enter the reproducibility hash.
    - ``confirmed`` — escalated true positive; durable. Event-scoped with a
      concrete detector; bulk re-scans preserve the confirmed
      ``(event, detector)`` pair's system annotation.
    - ``routine`` — a real, recurring, expected pattern (sequence_motif's
      "mark routine"). Presentation-only like ``dismissed`` — detectors keep
      scoring and it never enters the reproducibility hash — but with a
      distinct meaning and a side effect: its occurrences are materialized to
      ClickHouse (``motif_occurrences``) so the event grid can *collapse*
      them behind an explicit, always-visible collapsed-count. Value-scoped
      (``field`` = the series field, ``value`` = the " → "-joined n-gram);
      ``details`` snapshots the motif finding (period, support, n).

    "Undecided" is the absence of a row. Scope is exactly one of value
    (``field`` + ``value``, timeline-scoped) or event (``source_id`` +
    ``event_id``; ``timeline_id`` is NULL because events live once per Source
    and appear in multiple timelines). ``detector`` is a detector key or the
    literal ``"*"`` wildcard (all detectors), matched at read time via
    ``detector in (detector, "*")``. For ``frequency``, ``field`` is the
    series field and a value-scoped row covers the whole series.
    """

    __tablename__ = "finding_dispositions"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # NULL for event-scoped rows (see class docstring).
    timeline_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    detector: Mapped[str] = mapped_column(String(32), nullable=False, server_default="*")
    # Value scope (mutually exclusive with event scope).
    field: Mapped[str | None] = mapped_column(String(255), nullable=True)
    value: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # Event scope.
    source_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    event_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    note: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    # confirmed: the finding's structured details snapshot at confirm time.
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_by: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "timeline_id": self.timeline_id,
            "kind": self.kind,
            "detector": self.detector,
            "field": self.field,
            "value": self.value,
            "source_id": self.source_id,
            "event_id": self.event_id,
            "note": self.note,
            "details": self.details,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


DISPOSITION_KINDS = ("normal", "dismissed", "confirmed", "routine")


def dispositions_hash(rows: Iterable[FindingDisposition]) -> str:
    """Deterministic SHA-256 over the detection-affecting disposition rows.

    Stamped into ``DetectorRun.params`` so a run records exactly which
    suppression set it was filtered through ("why is this value not
    flagged?" stays answerable after dispositions change). Only
    ``kind="normal"`` rows enter the hash — ``dismissed`` is presentation-only
    and ``confirmed`` doesn't suppress anything, so neither changes what a
    detector computes. Value scope hashes as ``("v", detector, field, value)``,
    event scope as ``("e", detector, source_id, event_id)`` — the latter closes
    the old gap where the per-event ``normal`` annotation exclusion was applied
    but never recorded in the run params.
    """
    tuples = sorted(
        ("v", d.detector, d.field or "", d.value or "")
        if d.field is not None
        else ("e", d.detector, d.source_id or "", d.event_id or "")
        for d in rows
        if d.kind == "normal"
    )
    return _windows_config_hash({"dispositions": [list(t) for t in tuples]})


class DetectorRun(Base):
    """A persisted statistical-anomaly-detector scan result.

    Exists so the client can reference a scan's finding-event-id list by a
    short ``run_id`` instead of re-uploading it as a URL query param on every
    subsequent request (the ``live_event_ids`` approach this replaces — see
    ``_resolve_event_id_filters`` in ``api/routers/events.py``). Rows
    accumulate rather than being overwritten, matching the forensic-
    reproducibility posture of ``Annotation``/``View``: a case's history of
    what was scanned, with what parameters, and what it found, stays
    auditable rather than being silently replaced by the next scan.
    """

    __tablename__ = "detector_runs"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    detector: Mapped[str] = mapped_column(String(32), nullable=False)
    # Request params the scan was run with (fields/series_field, z_threshold,
    # baseline_id, windows, limit, ...) — kept for forensic reproducibility.
    # Rows persisted before the legacy split-point removal may still carry
    # baseline_end/temporal keys; params are never replayed, only displayed.
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    # Serialized StatAnomalyResult (status/method/baseline_size/z_threshold/
    # results), the same shape returned to the client by list_anomalies —
    # see _serialize_finding in api/routers/events.py.
    result: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary for the DetectorRun API response."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "timeline_id": self.timeline_id,
            "detector": self.detector,
            "params": self.params,
            "result": self.result,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Annotation(Base):
    """A tag or comment annotation attached to a single event.

    The ``origin`` column distinguishes human annotations (``"user"``, the
    default) from machine-generated ones (``"system"``).  System annotations are
    written by the outlier-detection pipeline and carry structured math in the
    ``details`` JSON column; they are presented differently in the UI and cannot
    be deleted through the normal annotation delete endpoint.

    Annotations are scoped by ``source_id`` because events are stored once per
    Source and may appear in multiple Timelines.
    """

    __tablename__ = "annotations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    annotation_type: Mapped[str] = mapped_column(String(16), nullable=False)
    content: Mapped[str] = mapped_column(String(4096), nullable=False)
    created_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # "user" (default) = human; "system" = machine-generated by the analysis pipeline.
    origin: Mapped[str] = mapped_column(
        String(16), nullable=False, default="user", server_default="user"
    )
    # Structured math for system annotations (null for human annotations).
    details: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # Which detector produced this system annotation (e.g. "value_novelty",
    # "frequency"); null for human annotations. Scopes confirm/clear behavior
    # so a confirmed finding from one detector doesn't suppress a distinct
    # finding from another detector on the same event.
    detector: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary for the Annotation API response."""
        return {
            "id": self.id,
            "event_id": self.event_id,
            "source_id": self.source_id,
            "annotation_type": self.annotation_type,
            "content": self.content,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
            "origin": self.origin,
            "details": self.details,
            "detector": self.detector,
        }


class User(Base):
    """An analyst or administrator account.

    ``password_hash`` is null for OIDC-only accounts that have never set a
    local password. ``auth_provider`` records how the account was created;
    an OIDC-provisioned account can still gain a local password later if an
    admin sets one. ``must_change_password`` forces a password rotation
    before any other mutating action succeeds — used for the seeded admin
    bootstrap credential (see ``core.config.Settings.admin_password``), which
    is invalidated the moment it's changed.
    """

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    username: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    must_change_password: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # "local" (username+password) or "oidc" (provisioned via an external IdP).
    auth_provider: Mapped[str] = mapped_column(String(16), nullable=False, default="local")
    oidc_subject: Mapped[str | None] = mapped_column(String(255), nullable=True, unique=True)
    onboarding_completed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary. Never includes ``password_hash``."""
        return {
            "id": self.id,
            "username": self.username,
            "display_name": self.display_name,
            "email": self.email,
            "is_admin": self.is_admin,
            "is_active": self.is_active,
            "must_change_password": self.must_change_password,
            "auth_provider": self.auth_provider,
            "onboarding_completed": self.onboarding_completed,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }


class Session(Base):
    """A server-side login session, referenced by an opaque httpOnly cookie value.

    Storing sessions in Postgres (rather than a signed stateless token) makes
    them instantly revocable — deleting/deactivating a user or rotating a
    password can invalidate every outstanding session immediately, which
    matters for a forensic tool where "who was logged in when" must be
    reconstructable and access must be cut off the moment it's revoked.
    """

    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)


class Team(Base):
    """An investigation team. Cases optionally belong to a team."""

    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class TeamMembership(Base):
    """A user's membership in a team, carrying their role within it.

    ``manager`` members may create/delete cases for the team; ``member``
    users may only access existing team cases (add sources, create
    timelines, annotate).
    """

    __tablename__ = "team_memberships"

    team_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("teams.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
    )

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        return {
            "team_id": self.team_id,
            "user_id": self.user_id,
            "role": self.role,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class AuditLog(Base):
    """An append-only forensic record of a user action.

    Written for every authenticated (and failed-auth) request by the audit
    middleware in ``api.main``, plus enriched rows for security-relevant
    events (login, admin CRUD, password rotation) via
    :py:meth:`PostgresStore.record_audit`. Rows are never mutated or deleted
    by application code. ``username_snapshot`` preserves the actor's name
    even after the ``User`` row is deleted, so the trail stays legible.
    Request/response bodies are never captured, so credentials never appear
    here.
    """

    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        index=True,
    )
    user_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    username_snapshot: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    method: Mapped[str | None] = mapped_column(String(8), nullable=True)
    path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    route: Mapped[str | None] = mapped_column(String(255), nullable=True)
    case_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_code: Mapped[int | None] = mapped_column(nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    def to_dict(self) -> dict[str, Any]:
        """Return a serializable dictionary."""
        return {
            "id": self.id,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "user_id": self.user_id,
            "username": self.username_snapshot,
            "action": self.action,
            "method": self.method,
            "path": self.path,
            "route": self.route,
            "case_id": self.case_id,
            "target_type": self.target_type,
            "target_id": self.target_id,
            "status_code": self.status_code,
            "ip": self.ip,
            "user_agent": self.user_agent,
            "detail": self.detail,
        }


def _pre_alembic_fixups(sync_conn: Any) -> None:
    """One-time schema normalization for databases that predate Alembic.

    Runs exactly once per database, immediately before it is stamped at
    revision ``0001`` (see :py:meth:`PostgresStore.init_schema`). These are
    the additive fixups the old ``init_schema`` applied on every startup;
    they bring a pre-Alembic database to the shape revision ``0001``
    describes. A pre-Alembic database is assumed to have been running a
    recent build (all tables present — true for the production deployment);
    older pre-release databases were already documented as deprecated.

    Never add to this function — new schema changes are Alembic revisions.
    """
    insp = inspect(sync_conn)
    tables = set(insp.get_table_names())
    # Destructive staging-format migration (roadmap M16): the legacy
    # row-per-(event, attr, output_field) staging table (recognized by its
    # `field_key` column) is replaced by row-per-(job, event); staged rows
    # are transient in-flight state, safe to discard.
    if "enrichment_results_staging" in tables and any(
        col["name"] == "field_key" for col in insp.get_columns("enrichment_results_staging")
    ):
        sync_conn.execute(text("DROP TABLE enrichment_results_staging"))
        # Recreate in the current shape immediately (the old path relied on a
        # subsequent create_all; revision 0001 is skipped on stamped databases).
        Base.metadata.tables["enrichment_results_staging"].create(sync_conn)
    annotation_columns = {col["name"] for col in insp.get_columns("annotations")}
    for column, ddl in (
        ("pinned", "ALTER TABLE annotations ADD COLUMN pinned BOOLEAN NOT NULL DEFAULT false"),
        ("detector", "ALTER TABLE annotations ADD COLUMN detector VARCHAR(32)"),
    ):
        if column not in annotation_columns:
            sync_conn.execute(text(ddl))
    timeline_columns = {col["name"] for col in insp.get_columns("timelines")}
    if "field_mappings" not in timeline_columns:
        sync_conn.execute(text("ALTER TABLE timelines ADD COLUMN field_mappings JSON"))
    user_columns = {col["name"] for col in insp.get_columns("users")}
    if "onboarding_completed" not in user_columns:
        sync_conn.execute(
            text("ALTER TABLE users ADD COLUMN onboarding_completed BOOLEAN NOT NULL DEFAULT false")
        )
    source_columns = {col["name"] for col in insp.get_columns("sources")}
    if "status" not in source_columns:
        # Existing rows predate the ingest-status lifecycle and are by
        # definition fully ingested, so they backfill to 'ready'.
        sync_conn.execute(
            text("ALTER TABLE sources ADD COLUMN status VARCHAR(16) NOT NULL DEFAULT 'ready'")
        )
    if sync_conn.dialect.name == "postgresql":
        size_bytes_type = next(
            col["type"] for col in insp.get_columns("sources") if col["name"] == "size_bytes"
        )
        if str(size_bytes_type) == "INTEGER":
            # Files bigger than 2 GiB (int4 max) overflowed this column.
            sync_conn.execute(text("ALTER TABLE sources ALTER COLUMN size_bytes TYPE BIGINT"))


class PostgresStore:
    """Async PostgreSQL store for metadata."""

    def __init__(self, url: str | None = None) -> None:
        self.url = url or get_settings().postgres_url
        self.engine = create_async_engine(self.url, echo=False, future=True)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )

    async def init_schema(self) -> None:
        """Bring the metadata schema to the current Alembic head.

        Schema management is Alembic-driven (``src/vestigo/db/migrations``);
        this replaces the former ``create_all`` + hand-rolled inspector-ALTER
        approach. Two paths:

        - **Fresh database** (no tables): ``upgrade head`` creates everything.
        - **Pre-Alembic database** (tables exist, no ``alembic_version``):
          the legacy inspector-based fixups run one last time to normalize
          the schema to what revision ``0001`` describes, the database is
          stamped at ``0001``, then upgraded to head. No manual deploy step.

        New schema changes must be Alembic revisions
        (``uv run alembic revision --autogenerate``), never inspector ALTERs.
        """

        def _upgrade(sync_conn: Any) -> None:
            from alembic import command
            from alembic.config import Config

            script_location = str(Path(__file__).parent / "migrations")
            cfg = Config()
            cfg.set_main_option("script_location", script_location)
            cfg.attributes["connection"] = sync_conn
            tables = set(inspect(sync_conn).get_table_names())
            if "cases" in tables and "alembic_version" not in tables:
                _pre_alembic_fixups(sync_conn)
                command.stamp(cfg, "0001")
            command.upgrade(cfg, "head")

        async with self.engine.begin() as conn:
            await conn.run_sync(_upgrade)

    async def get_case(self, case_id: str) -> Case | None:
        """Return a case by ID, or None if not found."""
        async with self.session_factory() as session:
            return await session.get(Case, case_id)

    async def create_case(
        self,
        case_id: str,
        name: str,
        description: str | None = None,
        owner_id: str | None = None,
        team_id: str | None = None,
    ) -> Case:
        """Create a new case and its default timeline."""
        case = Case(
            id=case_id, name=name, description=description, owner_id=owner_id, team_id=team_id
        )
        default_timeline = Timeline(
            id=generate_id("all-sources"),
            case_id=case_id,
            name="All sources",
            description="Default timeline containing every source in this case.",
            is_default=True,
        )
        async with self.session_factory() as session:
            session.add(case)
            session.add(default_timeline)
            await session.commit()
            await session.refresh(case)
            return case

    async def list_cases(self) -> list[Case]:
        """Return all cases ordered by creation time. Unscoped — admin/CLI use only."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(select(Case).order_by(Case.created_at.desc()))
            return list(result.scalars().all())

    async def update_case_team(self, case_id: str, team_id: str | None) -> Case | None:
        """Reassign a case's team scope (None releases it back to personal). Returns the updated case, or None if not found."""
        async with self.session_factory() as session:
            case = await session.get(Case, case_id)
            if case is None:
                return None
            case.team_id = team_id
            await session.commit()
            await session.refresh(case)
            return case

    async def list_cases_for_user(self, user_id: str, team_ids: list[str]) -> list[Case]:
        """Return cases visible to a non-admin user: their own, plus their teams'."""
        from sqlalchemy import and_, or_, select

        # Owner match only applies to personal (team-less) cases — a team
        # case is governed entirely by current membership (see
        # deps.resolve_case_access), so an owner removed from the team must
        # stop seeing it here too, or the case card lists but every click
        # dead-ends in a 403.
        conditions = [and_(Case.owner_id == user_id, Case.team_id.is_(None))]
        if team_ids:
            conditions.append(Case.team_id.in_(team_ids))
        async with self.session_factory() as session:
            result = await session.execute(
                select(Case).where(or_(*conditions)).order_by(Case.created_at.desc())
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Sources
    # ------------------------------------------------------------------

    async def get_source(self, case_id: str, source_id: str) -> Source | None:
        """Return a source by case and source IDs."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Source).where(
                    Source.case_id == case_id,
                    Source.id == source_id,
                )
            )
            return result.scalar_one_or_none()

    async def get_source_by_hash(self, case_id: str, file_hash: str) -> Source | None:
        """Return an existing source row for the same file hash, if any."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Source).where(
                    Source.case_id == case_id,
                    Source.file_hash == file_hash,
                )
            )
            return result.scalar_one_or_none()

    async def list_sources(self, case_id: str) -> list[Source]:
        """Return all sources for a case ordered by creation time."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Source).where(Source.case_id == case_id).order_by(Source.created_at.desc())
            )
            return list(result.scalars().all())

    async def create_source(
        self,
        case_id: str,
        source_id: str,
        name: str,
        file_hash: str,
        size_bytes: int,
        filename: str | None = None,
        parser: str | None = None,
        parser_version: str | None = None,
        event_count: int = 0,
        created_by: str | None = None,
        status: str = "ready",
    ) -> Source:
        """Create a new source record within a case.

        ``status`` defaults to "ready" (synchronous callers like the CLI
        create the row after ingestion completed); the upload endpoint passes
        "ingesting" and flips it to "ready" when its background job finishes.
        """
        source = Source(
            id=source_id,
            case_id=case_id,
            name=name,
            filename=filename,
            file_hash=file_hash,
            size_bytes=size_bytes,
            parser=parser,
            parser_version=parser_version,
            event_count=event_count,
            created_by=created_by,
            status=status,
        )
        async with self.session_factory() as session:
            session.add(source)
            await session.commit()
            await session.refresh(source)
            return source

    async def update_source_counts(
        self,
        case_id: str,
        source_id: str,
        event_count: int | None = None,
        vector_count: int | None = None,
    ) -> None:
        """Update stored event/vector counts for a source.

        ``event_count`` is treated as a delta and added atomically to the
        stored value. ``vector_count`` is set to the supplied absolute value.
        Pass ``None`` for a count that should not be changed.
        """
        values: dict = {"updated_at": datetime.now(UTC)}
        if vector_count is not None:
            values["vector_count"] = vector_count
        async with self.session_factory() as session:
            if event_count is not None:
                await session.execute(
                    update(Source)
                    .where(Source.id == source_id, Source.case_id == case_id)
                    .values(
                        event_count=Source.event_count + event_count,
                        **dict(values),
                    )
                )
            elif values:
                await session.execute(
                    update(Source)
                    .where(Source.id == source_id, Source.case_id == case_id)
                    .values(**values)
                )
            await session.commit()

    async def set_source_status(self, case_id: str, source_id: str, status: str) -> None:
        """Set a source's ingest-lifecycle status ("ingesting" or "ready")."""
        async with self.session_factory() as session:
            await session.execute(
                update(Source)
                .where(Source.id == source_id, Source.case_id == case_id)
                .values(status=status, updated_at=datetime.now(UTC))
            )
            await session.commit()

    async def set_source_time_offset(
        self, case_id: str, source_id: str, seconds: int
    ) -> Source | None:
        """Set a source's analyst-declared clock-skew correction (W2).

        Applied at query time only — never mutates events. Returns the updated
        Source (detached) so the caller can audit the previous vs new value and
        serialize the row, or ``None`` when no such source exists.
        """
        async with self.session_factory() as session:
            source = await session.get(Source, source_id)
            if source is None or source.case_id != case_id:
                return None
            source.time_offset_seconds = seconds
            source.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(source)
            session.expunge(source)
            return source

    async def source_hash_in_use(self, file_hash: str, *, exclude_source_id: str) -> bool:
        """Whether any *other* source row (in any case) still has this file hash.

        Retention storage is content-addressed by hash alone (not per-case),
        so a file uploaded into multiple cases shares one retained copy —
        callers must check this before deleting a retained file for a source
        being removed, or they'd delete a copy another case still needs.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(Source.id).where(
                    Source.file_hash == file_hash, Source.id != exclude_source_id
                )
            )
            return result.scalar_one_or_none() is not None

    async def list_ingesting_sources(self) -> list[Source]:
        """Return every source still marked "ingesting", across all cases.

        Used by startup reconciliation: ingestion jobs live in the in-memory
        JobStore, so a source found in this state on a fresh boot was
        orphaned by a mid-ingest restart — its partial events and row are
        removed the same way a failed ingest cleans up after itself.
        """
        async with self.session_factory() as session:
            result = await session.execute(select(Source).where(Source.status == "ingesting"))
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Enrichers
    # ------------------------------------------------------------------

    async def list_timeline_enrichers(self, timeline_id: str) -> list[TimelineEnricher]:
        """Return every enricher config row for a timeline."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(TimelineEnricher).where(TimelineEnricher.timeline_id == timeline_id)
            )
            return list(result.scalars().all())

    async def upsert_timeline_enricher(
        self,
        timeline_id: str,
        enricher_key: str,
        mode: str,
        enabled: bool,
        updated_by: str | None,
    ) -> TimelineEnricher:
        """Create or update a timeline's config for one enricher."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(TimelineEnricher).where(
                    TimelineEnricher.timeline_id == timeline_id,
                    TimelineEnricher.enricher_key == enricher_key,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = TimelineEnricher(
                    id=generate_id(f"enricher_{enricher_key}"),
                    timeline_id=timeline_id,
                    enricher_key=enricher_key,
                    mode=mode,
                    enabled=enabled,
                    updated_by=updated_by,
                )
                session.add(row)
            else:
                row.mode = mode
                row.enabled = enabled
                row.updated_by = updated_by
                row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_automatic_enrichers_for_source(
        self, source_id: str, default_auto_keys: Iterable[str] = ()
    ) -> list[tuple[str, str]]:
        """Return ``(timeline_id, enricher_key)`` pairs to auto-run after this source ingests.

        A pair is included when the timeline has an explicit config row with
        ``mode="automatic", enabled=True`` — or, for enrichers in
        ``default_auto_keys`` (the admin-set instance-wide defaults), when the
        timeline has *no* explicit row for that enricher at all. An explicit
        row always overrides the instance default, in either direction.
        """
        from vestigo.enrichers.base import effective_enricher_state

        default_keys = set(default_auto_keys)
        async with self.session_factory() as session:
            timeline_result = await session.execute(
                select(TimelineSource.timeline_id).where(TimelineSource.source_id == source_id)
            )
            timeline_ids = [row[0] for row in timeline_result.all()]
            if not timeline_ids:
                return []
            config_result = await session.execute(
                select(TimelineEnricher).where(TimelineEnricher.timeline_id.in_(timeline_ids))
            )
            configs = list(config_result.scalars().all())

        explicit = {(c.timeline_id, c.enricher_key): c for c in configs}
        candidates = set(explicit) | {
            (timeline_id, key) for timeline_id in timeline_ids for key in default_keys
        }
        pairs: list[tuple[str, str]] = []
        for timeline_id, key in sorted(candidates):
            config = explicit.get((timeline_id, key))
            enabled, mode = effective_enricher_state(
                config.enabled if config else None,
                config.mode if config else None,
                key in default_keys,
            )
            if enabled and mode == "automatic":
                pairs.append((timeline_id, key))
        return pairs

    async def list_enricher_global_configs(self) -> list[EnricherGlobalConfig]:
        """Return every instance-wide enricher config row."""
        async with self.session_factory() as session:
            result = await session.execute(select(EnricherGlobalConfig))
            return list(result.scalars().all())

    async def upsert_enricher_global_config(
        self, enricher_key: str, auto_run_default: bool, updated_by: str | None
    ) -> EnricherGlobalConfig:
        """Create or update the instance-wide config for one enricher."""
        async with self.session_factory() as session:
            row = await session.get(EnricherGlobalConfig, enricher_key)
            if row is None:
                row = EnricherGlobalConfig(
                    enricher_key=enricher_key,
                    auto_run_default=auto_run_default,
                    updated_by=updated_by,
                )
                session.add(row)
            else:
                row.auto_run_default = auto_run_default
                row.updated_by = updated_by
                row.updated_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return row

    async def stage_enrichment_results(self, rows: list[dict[str, Any]]) -> None:
        """Bulk-insert enrichment result rows into the crash-safe staging table."""
        if not rows:
            return
        async with self.session_factory() as session:
            await session.execute(insert(EnrichmentResultStaging), rows)
            await session.commit()

    async def list_staged_rows_for_job(
        self, job_id: str, limit: int
    ) -> list[EnrichmentResultStaging]:
        """Return up to ``limit`` staged rows for a job, oldest first (does not delete them)."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(EnrichmentResultStaging)
                .where(EnrichmentResultStaging.job_id == job_id)
                .order_by(EnrichmentResultStaging.id.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def delete_staged_rows_for_job(self, job_id: str) -> None:
        """Delete every staged row for a job — used to discard an orphaned/unflushed run."""
        async with self.session_factory() as session:
            await session.execute(
                delete(EnrichmentResultStaging).where(EnrichmentResultStaging.job_id == job_id)
            )
            await session.commit()

    async def list_staged_sources(self, job_id: str) -> list[tuple[str, str]]:
        """Return the distinct ``(case_id, source_id)`` pairs a job has staged rows for."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(EnrichmentResultStaging.case_id, EnrichmentResultStaging.source_id)
                .where(EnrichmentResultStaging.job_id == job_id)
                .distinct()
                .order_by(EnrichmentResultStaging.source_id.asc())
            )
            return [(row[0], row[1]) for row in result.all()]

    async def list_staged_rows_for_source(
        self, job_id: str, source_id: str, limit: int, after_id: int = 0
    ) -> list[EnrichmentResultStaging]:
        """Keyset-paged staged rows for one source of a job (does not delete).

        Apply needs the rows to survive until the partition ``REPLACE``
        succeeds — deletion happens separately via
        ``delete_staged_rows_for_source``.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(EnrichmentResultStaging)
                .where(
                    EnrichmentResultStaging.job_id == job_id,
                    EnrichmentResultStaging.source_id == source_id,
                    EnrichmentResultStaging.id > after_id,
                )
                .order_by(EnrichmentResultStaging.id.asc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def delete_staged_rows_for_source(self, job_id: str, source_id: str) -> None:
        """Delete a job's staged rows for one source — only after its partition swap succeeded."""
        async with self.session_factory() as session:
            await session.execute(
                delete(EnrichmentResultStaging).where(
                    EnrichmentResultStaging.job_id == job_id,
                    EnrichmentResultStaging.source_id == source_id,
                )
            )
            await session.commit()

    async def record_source_enrichment(
        self,
        *,
        case_id: str,
        source_id: str,
        timeline_id: str,
        enricher_key: str,
        enricher_config_hash: str,
        job_id: str,
        rows_applied: int,
    ) -> SourceEnrichment:
        """Upsert the per-source provenance row after enrichment was applied."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceEnrichment).where(
                    SourceEnrichment.source_id == source_id,
                    SourceEnrichment.enricher_key == enricher_key,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                row = SourceEnrichment(
                    id=generate_id(f"srcenrich_{enricher_key}"),
                    case_id=case_id,
                    source_id=source_id,
                )
                session.add(row)
            row.timeline_id = timeline_id
            row.enricher_key = enricher_key
            row.enricher_config_hash = enricher_config_hash
            row.job_id = job_id
            row.rows_applied = rows_applied
            row.applied_at = datetime.now(UTC)
            await session.commit()
            await session.refresh(row)
            return row

    async def list_source_enrichments(self, source_id: str) -> list[SourceEnrichment]:
        """Return every enrichment provenance row for a source."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceEnrichment).where(SourceEnrichment.source_id == source_id)
            )
            return list(result.scalars().all())

    async def list_enriched_source_ids(
        self, case_id: str, enricher_key: str, config_hash: str
    ) -> set[str]:
        """Source IDs already enriched by this enricher at exactly ``config_hash``.

        Lets a re-run skip sources whose derived fields are already current: a
        matching ``(enricher_key, enricher_config_hash)`` provenance row means
        the exact enricher configuration *and* data version already produced
        this source's fields — for GeoIP that includes the installed database's
        hash, so an admin swapping the ``.mmdb`` bumps ``config_hash`` and no
        longer matches, forcing a re-run.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceEnrichment.source_id).where(
                    SourceEnrichment.case_id == case_id,
                    SourceEnrichment.enricher_key == enricher_key,
                    SourceEnrichment.enricher_config_hash == config_hash,
                )
            )
            return set(result.scalars().all())

    async def start_enrichment_job_run(
        self, job_id: str, timeline_id: str, case_id: str, enricher_key: str
    ) -> None:
        """Write the durable in-flight marker for an enrichment job, before any processing starts."""
        async with self.session_factory() as session:
            session.add(
                EnrichmentJobRun(
                    job_id=job_id,
                    timeline_id=timeline_id,
                    case_id=case_id,
                    enricher_key=enricher_key,
                )
            )
            await session.commit()

    async def mark_enrichment_source_staged(self, job_id: str, source_id: str) -> None:
        """Record on the job marker that *source_id*'s staging fully completed.

        Called by the enrichment job after each source's last batch is staged,
        so a crash between sources leaves a marker that says exactly which
        sources finished — reconciliation can then grant those (and only
        those) provenance instead of re-enriching the whole job. Single-writer
        per job (one task owns a job id), so read-modify-write is safe.
        """
        async with self.session_factory() as session:
            run = await session.get(EnrichmentJobRun, job_id)
            if run is None:
                return
            if source_id not in run.completed_source_ids:
                # Assign a fresh list — in-place append is invisible to the
                # JSON column's change tracking.
                run.completed_source_ids = [*run.completed_source_ids, source_id]
            await session.commit()

    async def finish_enrichment_job_run(self, job_id: str) -> None:
        """Delete the durable marker for an enrichment job — only call after its final flush succeeds."""
        async with self.session_factory() as session:
            await session.execute(delete(EnrichmentJobRun).where(EnrichmentJobRun.job_id == job_id))
            await session.commit()

    async def list_orphaned_enrichment_job_runs(self) -> list[EnrichmentJobRun]:
        """Return every enrichment job marker still present at startup.

        Mirrors ``list_ingesting_sources``: enrichment jobs live in the
        in-memory JobStore, so any marker row found on a fresh boot was
        orphaned by a mid-run restart and never reached its final flush.
        """
        async with self.session_factory() as session:
            result = await session.execute(select(EnrichmentJobRun))
            return list(result.scalars().all())

    async def delete_source(self, case_id: str, source_id: str) -> bool:
        """Delete a source row and its enrichment provenance/staging.

        ``SourceEnrichment`` and ``EnrichmentResultStaging`` reference the
        source by a plain ``source_id`` column (no FK/cascade), so they are
        deleted here too or they'd orphan when the source is removed.

        Returns True if a row was removed, False if it did not exist.
        """
        from sqlalchemy import delete, select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Source).where(
                    Source.case_id == case_id,
                    Source.id == source_id,
                )
            )
            source = result.scalar_one_or_none()
            if source is None:
                return False
            await session.execute(
                delete(SourceEnrichment).where(
                    SourceEnrichment.case_id == case_id,
                    SourceEnrichment.source_id == source_id,
                )
            )
            await session.execute(
                delete(EnrichmentResultStaging).where(
                    EnrichmentResultStaging.case_id == case_id,
                    EnrichmentResultStaging.source_id == source_id,
                )
            )
            await session.execute(
                delete(SourceFieldStats).where(
                    SourceFieldStats.case_id == case_id,
                    SourceFieldStats.source_id == source_id,
                )
            )
            await session.delete(source)
            await session.commit()
            return True

    # ------------------------------------------------------------------
    # Source field stats (M15 cache — see db/field_stats.py)
    # ------------------------------------------------------------------

    async def upsert_source_field_stats(
        self,
        *,
        case_id: str,
        source_id: str,
        stats_version: int,
        events_total: int,
        payload: dict,
    ) -> SourceFieldStats:
        """Insert or replace the cached field stats for one source.

        Concurrency-safe: the read path is self-healing, so several requests
        can miss the cache for the same source at once (e.g. ColumnPicker,
        viz, and anomaly field listings all firing on one page load) and race
        to insert. ``source_id`` is uniquely indexed, so the losing insert
        raises ``IntegrityError``; we roll back and retry, on which pass the
        now-present row is updated instead.
        """
        from sqlalchemy import select
        from sqlalchemy.exc import IntegrityError

        for attempt in range(2):
            async with self.session_factory() as session:
                result = await session.execute(
                    select(SourceFieldStats).where(SourceFieldStats.source_id == source_id)
                )
                row = result.scalar_one_or_none()
                if row is None:
                    row = SourceFieldStats(
                        id=generate_id(f"fieldstats_{source_id}"),
                        case_id=case_id,
                        source_id=source_id,
                    )
                    session.add(row)
                row.case_id = case_id
                row.stats_version = stats_version
                row.events_total = events_total
                row.payload = payload
                row.computed_at = datetime.now(UTC)
                try:
                    await session.commit()
                except IntegrityError:
                    # A concurrent insert won the race; retry so this call
                    # updates the row it just observed as missing.
                    await session.rollback()
                    if attempt == 0:
                        continue
                    raise
                await session.refresh(row)
                return row
        raise AssertionError("unreachable")  # pragma: no cover

    async def delete_source_field_stats(self, source_id: str) -> None:
        """Drop one source's cached stats so the next read recomputes them."""
        from sqlalchemy import delete

        async with self.session_factory() as session:
            await session.execute(
                delete(SourceFieldStats).where(SourceFieldStats.source_id == source_id)
            )
            await session.commit()

    async def get_source_field_stats(self, source_ids: list[str]) -> list[SourceFieldStats]:
        """Return cached field-stats rows for the given sources (missing ones absent)."""
        from sqlalchemy import select

        if not source_ids:
            return []
        async with self.session_factory() as session:
            result = await session.execute(
                select(SourceFieldStats).where(SourceFieldStats.source_id.in_(source_ids))
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Timelines
    # ------------------------------------------------------------------

    async def get_timeline(self, case_id: str, timeline_id: str) -> Timeline | None:
        """Return a timeline by case and timeline IDs.

        Sources are eagerly loaded so that ``to_dict()`` can build ``source_ids``
        without triggering an async lazy load outside of a session.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Timeline)
                .options(selectinload(Timeline.sources))
                .where(
                    Timeline.case_id == case_id,
                    Timeline.id == timeline_id,
                )
            )
            return result.scalar_one_or_none()

    async def get_default_timeline(self, case_id: str) -> Timeline | None:
        """Return the default timeline for a case, if it exists.

        Sources are eagerly loaded so callers can safely read ``source_ids``.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Timeline)
                .options(selectinload(Timeline.sources))
                .where(
                    Timeline.case_id == case_id,
                    Timeline.is_default.is_(True),
                )
            )
            return result.scalar_one_or_none()

    async def create_timeline(
        self,
        case_id: str,
        timeline_id: str,
        name: str,
        description: str | None = None,
        source_ids: list[str] | None = None,
        field_mappings: dict[str, list[str]] | None = None,
    ) -> Timeline:
        """Create a new timeline within a case and optionally attach sources."""
        timeline = Timeline(
            id=timeline_id,
            case_id=case_id,
            name=name,
            description=description,
            field_mappings=field_mappings or None,
        )
        async with self.session_factory() as session:
            session.add(timeline)
            if source_ids:
                # Resolve only source IDs that actually exist so we don't
                # create dangling join rows.
                valid = await session.execute(
                    select(Source.id).where(
                        Source.case_id == case_id,
                        Source.id.in_(source_ids),
                    )
                )
                for sid in valid.scalars().all():
                    # Insert via the join table directly — accessing
                    # timeline.sources on a new object triggers a sync lazy-load
                    # inside the async session which raises MissingGreenlet.
                    session.add(TimelineSource(timeline_id=timeline_id, source_id=sid))
            await session.commit()
            await session.refresh(timeline)
            # Eagerly load sources for the returned instance so ``to_dict()`` works
            # after the session closes.
            await session.refresh(timeline, attribute_names=["sources"])
            return timeline

    async def add_source_to_timeline(
        self,
        case_id: str,
        timeline_id: str,
        source_id: str,
    ) -> bool:
        """Add a source to a timeline.

        Returns True if the source was added, False if it was already a member
        or the timeline/source did not exist.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            timeline = await session.execute(
                select(Timeline.id).where(
                    Timeline.case_id == case_id,
                    Timeline.id == timeline_id,
                )
            )
            if timeline.scalar_one_or_none() is None:
                return False

            source = await session.execute(
                select(Source.id).where(
                    Source.case_id == case_id,
                    Source.id == source_id,
                )
            )
            if source.scalar_one_or_none() is None:
                return False

            existing = await session.execute(
                select(TimelineSource).where(
                    TimelineSource.timeline_id == timeline_id,
                    TimelineSource.source_id == source_id,
                )
            )
            if existing.scalar_one_or_none() is not None:
                return False

            session.add(TimelineSource(timeline_id=timeline_id, source_id=source_id))
            await session.execute(
                update(Timeline)
                .where(Timeline.id == timeline_id)
                .values(updated_at=datetime.now(UTC))
            )
            await session.commit()
            return True

    async def remove_source_from_timeline(
        self,
        case_id: str,
        timeline_id: str,
        source_id: str,
    ) -> bool:
        """Remove a source from a timeline.

        Returns True if the source was removed, False otherwise.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            timeline = await session.execute(
                select(Timeline.id).where(
                    Timeline.case_id == case_id,
                    Timeline.id == timeline_id,
                )
            )
            if timeline.scalar_one_or_none() is None:
                return False

            result = await session.execute(
                delete(TimelineSource).where(
                    TimelineSource.timeline_id == timeline_id,
                    TimelineSource.source_id == source_id,
                )
            )
            if result.rowcount == 0:
                return False

            await session.execute(
                update(Timeline)
                .where(Timeline.id == timeline_id)
                .values(updated_at=datetime.now(UTC))
            )
            await session.commit()
            return True

    async def list_timeline_sources(self, case_id: str, timeline_id: str) -> list[Source]:
        """Return all sources attached to a timeline."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Source)
                .join(TimelineSource)
                .join(Timeline)
                .where(
                    Timeline.case_id == case_id,
                    Timeline.id == timeline_id,
                )
                .order_by(Source.created_at.desc())
            )
            return list(result.scalars().all())

    async def list_timelines_for_source(self, case_id: str, source_id: str) -> list[Timeline]:
        """Return timelines in a case that include the given source."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Timeline)
                .join(TimelineSource)
                .where(
                    Timeline.case_id == case_id,
                    TimelineSource.source_id == source_id,
                )
            )
            return list(result.scalars().all())

    async def set_timeline_embedding(
        self,
        case_id: str,
        timeline_id: str,
        *,
        model: str,
        config: dict,
        config_hash: str,
        embedded_source_ids: list[str],
    ) -> bool:
        """Persist embedding metadata on a timeline after a successful embed job.

        Returns True if the timeline was found and updated, False otherwise.
        """
        async with self.session_factory() as session:
            result = await session.execute(
                update(Timeline)
                .where(Timeline.case_id == case_id, Timeline.id == timeline_id)
                .values(
                    embedding_model=model,
                    embedding_config=config,
                    embedding_config_hash=config_hash,
                    embedded_source_ids=embedded_source_ids,
                    embedded_at=datetime.now(UTC),
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def list_timelines(self, case_id: str) -> list[Timeline]:
        """Return all timelines for a case ordered by creation time.

        Sources are eagerly loaded so that ``to_dict()`` can build ``source_ids``
        without triggering an async lazy load outside of a session.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Timeline)
                .options(selectinload(Timeline.sources))
                .where(Timeline.case_id == case_id)
                .order_by(Timeline.created_at.desc())
            )
            return list(result.scalars().all())

    async def update_timeline_field_mappings(
        self,
        case_id: str,
        timeline_id: str,
        field_mappings: dict[str, list[str]] | None,
    ) -> Timeline | None:
        """Replace a timeline's field mappings (None/empty clears them).

        Mappings are timeline metadata, not evidence — edits are allowed and
        audited at the API layer. Returns the updated timeline with sources
        eagerly loaded, or None if it doesn't exist.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Timeline).where(
                    Timeline.case_id == case_id,
                    Timeline.id == timeline_id,
                )
            )
            timeline = result.scalar_one_or_none()
            if timeline is None:
                return None
            timeline.field_mappings = field_mappings or None
            await session.commit()
            await session.refresh(timeline)
            await session.refresh(timeline, attribute_names=["sources"])
            return timeline

    async def delete_timeline(self, case_id: str, timeline_id: str) -> bool:
        """Delete a timeline row.

        The default timeline cannot be deleted. Returns True if a row was
        removed, False if it did not exist or was the default timeline.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Timeline).where(
                    Timeline.case_id == case_id,
                    Timeline.id == timeline_id,
                )
            )
            timeline = result.scalar_one_or_none()
            if timeline is None or timeline.is_default:
                return False
            # Timeline-scoped rows only — event-scoped dispositions carry a
            # NULL timeline_id and stay with the case/source.
            for model in (BaselineDefinition, FindingDisposition, SavedChart):
                await session.execute(
                    delete(model).where(model.case_id == case_id, model.timeline_id == timeline_id)
                )
            await session.delete(timeline)
            await session.commit()
            return True

    async def delete_case(self, case_id: str) -> bool:
        """Delete a case and all its owned rows in one transaction.

        Returns True if the case existed and was removed, False otherwise.

        ``View``, ``Annotation``, ``DetectorRun``, and the enrichment tables
        (``SourceEnrichment``, ``EnrichmentResultStaging``,
        ``EnrichmentJobRun``) are case-scoped by a plain ``case_id`` column
        (no FK/cascade — they aren't declared with a ``ForeignKey`` to
        ``cases.id``), so they must be deleted explicitly here alongside
        ``Timeline``/``Source`` or they'd silently orphan on every case delete
        — and a leftover ``EnrichmentJobRun`` marker would even be picked up by
        startup reconciliation for evidence that no longer exists.
        """
        from sqlalchemy import delete

        async with self.session_factory() as session:
            case = await session.get(Case, case_id)
            if case is None:
                return False
            await session.execute(delete(Timeline).where(Timeline.case_id == case_id))
            await session.execute(delete(Source).where(Source.case_id == case_id))
            await session.execute(delete(View).where(View.case_id == case_id))
            await session.execute(delete(Annotation).where(Annotation.case_id == case_id))
            await session.execute(delete(DetectorRun).where(DetectorRun.case_id == case_id))
            await session.execute(delete(SavedChart).where(SavedChart.case_id == case_id))
            await session.execute(
                delete(BaselineDefinition).where(BaselineDefinition.case_id == case_id)
            )
            await session.execute(
                delete(FindingDisposition).where(FindingDisposition.case_id == case_id)
            )
            await session.execute(
                delete(SourceEnrichment).where(SourceEnrichment.case_id == case_id)
            )
            await session.execute(
                delete(EnrichmentResultStaging).where(EnrichmentResultStaging.case_id == case_id)
            )
            await session.execute(
                delete(EnrichmentJobRun).where(EnrichmentJobRun.case_id == case_id)
            )
            await session.delete(case)
            await session.commit()
            return True

    # ------------------------------------------------------------------
    # Views
    # ------------------------------------------------------------------

    async def list_views(self, case_id: str) -> list[View]:
        """Return all saved views for a case ordered by creation time (newest first)."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(View).where(View.case_id == case_id).order_by(View.created_at.desc())
            )
            return list(result.scalars().all())

    async def get_view(self, case_id: str, view_id: str) -> View | None:
        """Return a saved view by case and view IDs."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(View).where(View.case_id == case_id, View.id == view_id)
            )
            return result.scalar_one_or_none()

    async def create_view(
        self,
        case_id: str,
        view_id: str,
        name: str,
        query: str = "",
        view_filter: dict | None = None,
    ) -> View:
        """Create a new saved view within a case."""
        view = View(
            id=view_id,
            case_id=case_id,
            name=name,
            query=query,
            view_filter=view_filter or {},
        )
        async with self.session_factory() as session:
            session.add(view)
            await session.commit()
            await session.refresh(view)
            return view

    async def delete_view(self, case_id: str, view_id: str) -> bool:
        """Delete a saved view row.

        Returns True if the row existed and was removed.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(View).where(View.case_id == case_id, View.id == view_id)
            )
            view = result.scalar_one_or_none()
            if view is None:
                return False
            await session.delete(view)
            await session.commit()
            return True

    # ------------------------------------------------------------------
    # Saved charts
    # ------------------------------------------------------------------

    async def list_saved_charts(self, case_id: str, timeline_id: str) -> list[SavedChart]:
        """Return a timeline's saved charts ordered by creation time (newest first)."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(SavedChart)
                .where(SavedChart.case_id == case_id, SavedChart.timeline_id == timeline_id)
                .order_by(SavedChart.created_at.desc())
            )
            return list(result.scalars().all())

    async def create_saved_chart(
        self,
        case_id: str,
        timeline_id: str,
        chart_id: str,
        name: str,
        config: dict,
    ) -> SavedChart:
        """Create a new saved chart within a timeline."""
        chart = SavedChart(
            id=chart_id,
            case_id=case_id,
            timeline_id=timeline_id,
            name=name,
            config=config,
        )
        async with self.session_factory() as session:
            session.add(chart)
            await session.commit()
            await session.refresh(chart)
            return chart

    async def rename_saved_chart(
        self, case_id: str, timeline_id: str, chart_id: str, name: str
    ) -> SavedChart | None:
        """Rename a saved chart; returns the updated row or None if missing.

        Only the name is mutable — the stored ``config`` is immutable like a
        View's filter payload; changing the chart means saving a new one.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(SavedChart).where(
                    SavedChart.case_id == case_id,
                    SavedChart.timeline_id == timeline_id,
                    SavedChart.id == chart_id,
                )
            )
            chart = result.scalar_one_or_none()
            if chart is None:
                return None
            chart.name = name
            await session.commit()
            await session.refresh(chart)
            return chart

    async def delete_saved_chart(self, case_id: str, timeline_id: str, chart_id: str) -> bool:
        """Delete a saved chart row. Returns True if it existed."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(SavedChart).where(
                    SavedChart.case_id == case_id,
                    SavedChart.timeline_id == timeline_id,
                    SavedChart.id == chart_id,
                )
            )
            chart = result.scalar_one_or_none()
            if chart is None:
                return False
            await session.delete(chart)
            await session.commit()
            return True

    # ------------------------------------------------------------------
    # Baseline definitions
    # ------------------------------------------------------------------

    async def list_baseline_definitions(
        self, case_id: str, timeline_id: str
    ) -> list[BaselineDefinition]:
        """Return a timeline's baseline definitions, newest first."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(BaselineDefinition)
                .where(
                    BaselineDefinition.case_id == case_id,
                    BaselineDefinition.timeline_id == timeline_id,
                )
                .order_by(BaselineDefinition.created_at.desc())
            )
            return list(result.scalars().all())

    async def get_baseline_definition(
        self, case_id: str, timeline_id: str, baseline_id: str
    ) -> BaselineDefinition | None:
        """Return one baseline definition, scoped by case and timeline."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(BaselineDefinition).where(
                    BaselineDefinition.case_id == case_id,
                    BaselineDefinition.timeline_id == timeline_id,
                    BaselineDefinition.id == baseline_id,
                )
            )
            return result.scalar_one_or_none()

    async def create_baseline_definition(
        self,
        case_id: str,
        timeline_id: str,
        name: str,
        baseline_start: datetime,
        baseline_end: datetime,
        suspect_windows: list[dict],
        created_by: str | None = None,
    ) -> BaselineDefinition:
        """Create a baseline definition within a timeline."""
        definition = BaselineDefinition(
            id=generate_id(name),
            case_id=case_id,
            timeline_id=timeline_id,
            name=name,
            baseline_start=baseline_start,
            baseline_end=baseline_end,
            suspect_windows=suspect_windows,
            created_by=created_by,
        )
        async with self.session_factory() as session:
            session.add(definition)
            await session.commit()
            await session.refresh(definition)
            return definition

    async def update_baseline_definition(
        self,
        case_id: str,
        timeline_id: str,
        baseline_id: str,
        *,
        name: str | None = None,
        baseline_start: datetime | None = None,
        baseline_end: datetime | None = None,
        suspect_windows: list[dict] | None = None,
    ) -> BaselineDefinition | None:
        """Update a baseline definition; returns the row or None if missing.

        Editing is safe for reproducibility because every DetectorRun
        snapshots the windows it actually used (see class docstring).
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(BaselineDefinition).where(
                    BaselineDefinition.case_id == case_id,
                    BaselineDefinition.timeline_id == timeline_id,
                    BaselineDefinition.id == baseline_id,
                )
            )
            definition = result.scalar_one_or_none()
            if definition is None:
                return None
            if name is not None:
                definition.name = name
            if baseline_start is not None:
                definition.baseline_start = baseline_start
            if baseline_end is not None:
                definition.baseline_end = baseline_end
            if suspect_windows is not None:
                definition.suspect_windows = suspect_windows
            await session.commit()
            await session.refresh(definition)
            return definition

    async def delete_baseline_definition(
        self, case_id: str, timeline_id: str, baseline_id: str
    ) -> bool:
        """Delete a baseline definition row. Returns True if it existed."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(BaselineDefinition).where(
                    BaselineDefinition.case_id == case_id,
                    BaselineDefinition.timeline_id == timeline_id,
                    BaselineDefinition.id == baseline_id,
                )
            )
            definition = result.scalar_one_or_none()
            if definition is None:
                return False
            await session.delete(definition)
            await session.commit()
            return True

    # ------------------------------------------------------------------
    # Detector allowlist
    # ------------------------------------------------------------------

    async def list_dispositions(
        self,
        case_id: str,
        timeline_id: str | None = None,
        source_ids: list[str] | None = None,
        kinds: list[str] | None = None,
        detector: str | None = None,
    ) -> list[FindingDisposition]:
        """Return a case's disposition rows, newest first.

        With *timeline_id*/*source_ids* given, returns the rows visible from
        that timeline: value-scoped rows matching the timeline plus
        event-scoped rows (``timeline_id`` NULL) whose ``source_id`` is one of
        the timeline's sources. *kinds* filters to specific verdicts;
        *detector* matches the concrete detector **or** the ``"*"`` wildcard.
        """
        conditions: list[Any] = [FindingDisposition.case_id == case_id]
        if timeline_id is not None or source_ids is not None:
            scope = []
            if timeline_id is not None:
                scope.append(FindingDisposition.timeline_id == timeline_id)
            if source_ids is not None:
                scope.append(FindingDisposition.source_id.in_(source_ids))
            conditions.append(or_(*scope))
        if kinds is not None:
            conditions.append(FindingDisposition.kind.in_(kinds))
        if detector is not None:
            conditions.append(FindingDisposition.detector.in_([detector, "*"]))
        async with self.session_factory() as session:
            result = await session.execute(
                select(FindingDisposition)
                .where(*conditions)
                .order_by(FindingDisposition.created_at.desc())
            )
            return list(result.scalars().all())

    async def create_disposition(
        self,
        case_id: str,
        kind: str,
        detector: str = "*",
        timeline_id: str | None = None,
        field: str | None = None,
        value: str | None = None,
        source_id: str | None = None,
        event_id: str | None = None,
        note: str | None = None,
        details: dict | None = None,
        created_by: str | None = None,
    ) -> FindingDisposition:
        """Create a disposition row, or return the existing identical one.

        Deduplication is by exact scope key —
        ``(kind, detector, field, value, source_id, event_id)`` within the
        case/timeline — so repeating the same verdict is a no-op rather than
        an error and the UI action stays idempotent. Scope validation (exactly
        one of value/event scope) lives in the API layer.
        """
        async with self.session_factory() as session:
            existing = (
                await session.execute(
                    select(FindingDisposition).where(
                        FindingDisposition.case_id == case_id,
                        FindingDisposition.timeline_id == timeline_id,
                        FindingDisposition.kind == kind,
                        FindingDisposition.detector == detector,
                        FindingDisposition.field == field,
                        FindingDisposition.value == value,
                        FindingDisposition.source_id == source_id,
                        FindingDisposition.event_id == event_id,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                return existing
            row = FindingDisposition(
                id=generate_id(f"disp_{kind}"),
                case_id=case_id,
                timeline_id=timeline_id,
                kind=kind,
                detector=detector,
                field=field,
                value=value,
                source_id=source_id,
                event_id=event_id,
                note=note,
                details=details,
                created_by=created_by,
            )
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def create_dispositions_bulk(
        self, case_id: str, items: list[dict[str, Any]]
    ) -> list[FindingDisposition]:
        """Create several disposition rows in one transaction, with per-item dedupe.

        All-or-nothing: a bulk declaration is one analyst intent, so a failure
        on any item rolls back the whole batch instead of half-applying it.
        Each *items* dict takes the same keyword arguments as
        :meth:`create_disposition`; duplicates (against existing rows or
        earlier items in the same batch) return the existing row.

        Dedupe runs against one prefetched in-memory index keyed by the full
        scope tuple instead of a per-item SELECT + flush (3n round-trips for
        an n-item batch — routine collapses can batch large); the per-row
        refresh was dropped too, since ``expire_on_commit=False`` keeps
        attributes live and ``created_at`` has a Python-side default.
        """
        if not items:
            return []

        def _scope_key(
            timeline_id: Any,
            kind: Any,
            detector: Any,
            field: Any,
            value: Any,
            source_id: Any,
            event_id: Any,
        ) -> tuple:
            return (timeline_id, kind, detector, field, value, source_id, event_id)

        async with self.session_factory() as session:
            # One prefetch covering every row a batch item could collide with.
            # NULL scope columns rule out a composite-tuple IN (NULL never
            # matches IN), so narrow by the NOT NULL columns and finish the
            # exact match in the dict lookup.
            existing_rows = (
                (
                    await session.execute(
                        select(FindingDisposition).where(
                            FindingDisposition.case_id == case_id,
                            FindingDisposition.kind.in_({it["kind"] for it in items}),
                            FindingDisposition.detector.in_(
                                {it.get("detector", "*") for it in items}
                            ),
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_key: dict[tuple, FindingDisposition] = {
                _scope_key(
                    r.timeline_id, r.kind, r.detector, r.field, r.value, r.source_id, r.event_id
                ): r
                for r in existing_rows
            }
            rows: list[FindingDisposition] = []
            for it in items:
                key = _scope_key(
                    it.get("timeline_id"),
                    it["kind"],
                    it.get("detector", "*"),
                    it.get("field"),
                    it.get("value"),
                    it.get("source_id"),
                    it.get("event_id"),
                )
                existing = by_key.get(key)
                if existing is not None:
                    rows.append(existing)
                    continue
                row = FindingDisposition(
                    id=generate_id(f"disp_{it['kind']}"),
                    case_id=case_id,
                    timeline_id=it.get("timeline_id"),
                    kind=it["kind"],
                    detector=it.get("detector", "*"),
                    field=it.get("field"),
                    value=it.get("value"),
                    source_id=it.get("source_id"),
                    event_id=it.get("event_id"),
                    note=it.get("note"),
                    details=it.get("details"),
                    created_by=it.get("created_by"),
                )
                session.add(row)
                # Make the row visible to the dedupe lookup of later items.
                by_key[key] = row
                rows.append(row)
            await session.commit()
            return rows

    async def update_disposition_details(
        self, case_id: str, disposition_id: str, patch: dict[str, Any]
    ) -> bool:
        """Shallow-merge *patch* into a disposition's ``details`` JSON.

        Used by the motif-materialization job to persist its outcome
        (``details.materialization``) durably — the JobStore result is
        in-memory and lost on restart, but a partial collapse must stay
        announced. Existing keys outside the patch (``values``, ``scope_*``)
        are preserved. Returns False when the row is gone (deleted mid-job —
        the occurrence rows are inert then, nothing to record).
        """
        async with self.session_factory() as session:
            result = await session.execute(
                select(FindingDisposition).where(
                    FindingDisposition.case_id == case_id,
                    FindingDisposition.id == disposition_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            row.details = {**(row.details or {}), **patch}
            await session.commit()
            return True

    async def delete_disposition(self, case_id: str, disposition_id: str) -> bool:
        """Delete a disposition row. Returns True if it existed."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(FindingDisposition).where(
                    FindingDisposition.case_id == case_id,
                    FindingDisposition.id == disposition_id,
                )
            )
            row = result.scalar_one_or_none()
            if row is None:
                return False
            await session.delete(row)
            await session.commit()
            return True

    async def list_confirmed_keys(
        self,
        case_id: str,
        source_ids: list[str],
        detector: str | None = None,
    ) -> set[tuple[str, str]]:
        """Return the confirmed ``(event_id, detector)`` pairs for these sources.

        Used by the bulk tag endpoint so its clear/rewrite cycle preserves the
        system annotation of a manually-confirmed finding, and skips writing a
        duplicate over it. The successor of the retired ``pinned`` flag.
        """
        conditions = [
            FindingDisposition.case_id == case_id,
            FindingDisposition.kind == "confirmed",
            FindingDisposition.source_id.in_(source_ids),
        ]
        if detector is not None:
            conditions.append(FindingDisposition.detector == detector)
        async with self.session_factory() as session:
            result = await session.execute(
                select(FindingDisposition.event_id, FindingDisposition.detector).where(*conditions)
            )
            return {(row[0], row[1]) for row in result.all() if row[0]}

    # ------------------------------------------------------------------
    # Detector runs
    # ------------------------------------------------------------------

    async def create_detector_run(
        self,
        case_id: str,
        timeline_id: str,
        detector: str,
        params: dict,
        result: dict,
    ) -> DetectorRun:
        """Persist a detector scan result and return the created row."""
        run = DetectorRun(
            id=generate_id(f"run_{detector}"),
            case_id=case_id,
            timeline_id=timeline_id,
            detector=detector,
            params=params,
            result=result,
        )
        async with self.session_factory() as session:
            session.add(run)
            await session.commit()
            await session.refresh(run)
            return run

    async def get_detector_run(self, case_id: str, run_id: str) -> DetectorRun | None:
        """Return a persisted detector run by case and run IDs."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(DetectorRun).where(DetectorRun.case_id == case_id, DetectorRun.id == run_id)
            )
            return result.scalar_one_or_none()

    # ------------------------------------------------------------------
    # Annotations
    # ------------------------------------------------------------------

    async def list_annotations(
        self,
        case_id: str,
        source_id: str,
        event_id: str,
    ) -> list[Annotation]:
        """Return annotations for a single event, oldest first."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Annotation)
                .where(
                    Annotation.case_id == case_id,
                    Annotation.source_id == source_id,
                    Annotation.event_id == event_id,
                )
                .order_by(Annotation.created_at.asc())
            )
            return list(result.scalars().all())

    async def list_source_annotations(
        self,
        case_id: str,
        source_ids: list[str],
    ) -> list[Annotation]:
        """Return all annotations for one or more sources (used for event-table chips)."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Annotation)
                .where(
                    Annotation.case_id == case_id,
                    Annotation.source_id.in_(source_ids),
                )
                .order_by(Annotation.created_at.asc())
            )
            return list(result.scalars().all())

    async def create_annotation(
        self,
        case_id: str,
        source_id: str,
        event_id: str,
        annotation_id: str,
        annotation_type: str,
        content: str,
        created_by: str | None = None,
        origin: str = "user",
        details: dict | None = None,
        detector: str | None = None,
    ) -> Annotation:
        """Persist a new annotation and return it."""
        annotation = Annotation(
            id=annotation_id,
            case_id=case_id,
            source_id=source_id,
            event_id=event_id,
            annotation_type=annotation_type,
            content=content,
            created_by=created_by,
            origin=origin,
            details=details,
            detector=detector,
        )
        async with self.session_factory() as session:
            session.add(annotation)
            await session.commit()
            await session.refresh(annotation)
            return annotation

    async def bulk_create_annotations(self, rows: list[dict]) -> int:
        """Insert multiple annotations in a single transaction.

        Each dict in ``rows`` must contain the same keys accepted by
        :py:meth:`create_annotation`.  Returns the number of rows inserted.
        """
        if not rows:
            return 0
        annotations = [
            Annotation(
                id=row["annotation_id"],
                case_id=row["case_id"],
                source_id=row["source_id"],
                event_id=row["event_id"],
                annotation_type=row["annotation_type"],
                content=row["content"],
                created_by=row.get("created_by"),
                origin=row.get("origin", "user"),
                details=row.get("details"),
                detector=row.get("detector"),
            )
            for row in rows
        ]
        async with self.session_factory() as session:
            session.add_all(annotations)
            await session.commit()
        return len(annotations)

    async def delete_system_annotations(
        self,
        case_id: str,
        source_ids: list[str],
        annotation_type: str,
        detector: str | None = None,
        preserve_keys: set[tuple[str, str]] | None = None,
    ) -> int:
        """Delete system-origin annotations of a given type.

        Used before re-writing outlier tags so that a fresh "Tag outliers" run
        does not accumulate duplicate machine annotations. *preserve_keys* is
        the confirmed ``(event_id, detector)`` set from
        :meth:`list_confirmed_keys` — those rows are excluded from the clear,
        so a manually-confirmed finding survives even if a later re-scan no
        longer surfaces it. When ``detector`` is given, only rows from that
        detector are cleared — findings from a different detector (e.g.
        ``frequency`` vs ``value_novelty``) on the same sources are left
        untouched. Returns the count of deleted rows.
        """
        conditions = [
            Annotation.case_id == case_id,
            Annotation.source_id.in_(source_ids),
            Annotation.annotation_type == annotation_type,
            Annotation.origin == "system",
        ]
        if detector is not None:
            conditions.append(Annotation.detector == detector)
        async with self.session_factory() as session:
            if preserve_keys:
                # Row-by-row filter in Python: the pair-tuple NOT IN is not
                # portable across the SQLite test dialect, and the preserved
                # set is tiny (manually-confirmed findings only).
                rows = (
                    (await session.execute(select(Annotation).where(*conditions))).scalars().all()
                )
                doomed = [a for a in rows if (a.event_id, a.detector or "") not in preserve_keys]
                for a in doomed:
                    await session.delete(a)
                await session.commit()
                return len(doomed)
            result = await session.execute(delete(Annotation).where(*conditions))
            await session.commit()
            return result.rowcount

    async def delete_annotation(
        self,
        case_id: str,
        event_id: str,
        annotation_id: str,
    ) -> bool:
        """Delete a user-origin annotation row.

        System annotations (``origin="system"``) are managed by the analysis
        pipeline and cannot be deleted through this method — they return ``False``
        as if they did not exist.  Returns ``True`` if a user annotation was found
        and removed.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Annotation).where(
                    Annotation.case_id == case_id,
                    Annotation.event_id == event_id,
                    Annotation.id == annotation_id,
                    Annotation.origin == "user",
                )
            )
            annotation = result.scalar_one_or_none()
            if annotation is None:
                return False
            await session.delete(annotation)
            await session.commit()
            return True

    async def list_distinct_tag_contents(
        self,
        case_id: str,
        source_ids: list[str],
    ) -> list[str]:
        """Return the distinct annotation-tag labels used in this timeline.

        Used to power tag autocomplete in the UI.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Annotation.content)
                .where(
                    Annotation.case_id == case_id,
                    Annotation.source_id.in_(source_ids),
                    Annotation.annotation_type == "tag",
                    Annotation.origin == "user",
                )
                .distinct()
                .order_by(Annotation.content)
            )
            return [row[0] for row in result.all()]

    async def list_event_ids_by_annotation_type(
        self,
        case_id: str,
        source_ids: list[str],
        annotation_type: str,
        origin: str = "user",
        content: str | None = None,
        content_in: list[str] | None = None,
    ) -> list[str]:
        """Return the event_ids that have at least one annotation of the given type.

        Used by the anomaly service to retrieve the analyst-defined normal set,
        and by the events API to filter to tagged/anomaly-flagged events.
        ``content`` optionally narrows to a specific annotation value (e.g. a
        specific tag label); ``content_in`` narrows to any of several values
        (OR semantics) — the two are mutually exclusive.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            conditions = [
                Annotation.case_id == case_id,
                Annotation.source_id.in_(source_ids),
                Annotation.annotation_type == annotation_type,
                Annotation.origin == origin,
            ]
            if content is not None:
                conditions.append(Annotation.content == content)
            if content_in is not None:
                conditions.append(Annotation.content.in_(content_in))
            result = await session.execute(
                select(Annotation.event_id).where(*conditions).distinct()
            )
            return [row[0] for row in result.all()]

    # ------------------------------------------------------------------
    # Users
    # ------------------------------------------------------------------

    async def create_user(
        self,
        user_id: str,
        username: str,
        password_hash: str | None = None,
        is_admin: bool = False,
        must_change_password: bool = False,
        auth_provider: str = "local",
        oidc_subject: str | None = None,
        display_name: str | None = None,
        email: str | None = None,
    ) -> User:
        """Create a new user account and return it."""
        user = User(
            id=user_id,
            username=username,
            password_hash=password_hash,
            is_admin=is_admin,
            must_change_password=must_change_password,
            auth_provider=auth_provider,
            oidc_subject=oidc_subject,
            display_name=display_name,
            email=email,
        )
        async with self.session_factory() as session:
            session.add(user)
            await session.commit()
            await session.refresh(user)
            return user

    async def get_user(self, user_id: str) -> User | None:
        """Return a user by ID, or None if not found."""
        async with self.session_factory() as session:
            return await session.get(User, user_id)

    async def get_user_by_username(self, username: str) -> User | None:
        """Return a user by (case-sensitive) username, or None if not found."""
        async with self.session_factory() as session:
            result = await session.execute(select(User).where(User.username == username))
            return result.scalar_one_or_none()

    async def get_user_by_oidc_subject(self, oidc_subject: str) -> User | None:
        """Return a user by their OIDC subject claim, or None if not provisioned yet."""
        async with self.session_factory() as session:
            result = await session.execute(select(User).where(User.oidc_subject == oidc_subject))
            return result.scalar_one_or_none()

    async def list_users(self) -> list[User]:
        """Return all users ordered by creation time."""
        async with self.session_factory() as session:
            result = await session.execute(select(User).order_by(User.created_at.asc()))
            return list(result.scalars().all())

    async def update_user(
        self,
        user_id: str,
        *,
        username: str | None = None,
        display_name: str | None = None,
        is_admin: bool | None = None,
        is_active: bool | None = None,
        must_change_password: bool | None = None,
        onboarding_completed: bool | None = None,
    ) -> User | None:
        """Patch mutable fields on a user. Returns the updated row, or None if missing."""
        values: dict[str, Any] = {"updated_at": datetime.now(UTC)}
        if username is not None:
            values["username"] = username
        if display_name is not None:
            values["display_name"] = display_name
        if is_admin is not None:
            values["is_admin"] = is_admin
        if is_active is not None:
            values["is_active"] = is_active
        if must_change_password is not None:
            values["must_change_password"] = must_change_password
        if onboarding_completed is not None:
            values["onboarding_completed"] = onboarding_completed
        async with self.session_factory() as session:
            result = await session.execute(
                update(User).where(User.id == user_id).values(**values).returning(User)
            )
            user = result.scalar_one_or_none()
            await session.commit()
            return user

    async def set_password(
        self, user_id: str, password_hash: str, must_change_password: bool = False
    ) -> None:
        """Set a user's password hash, e.g. self-service change or admin rotation."""
        async with self.session_factory() as session:
            await session.execute(
                update(User)
                .where(User.id == user_id)
                .values(
                    password_hash=password_hash,
                    must_change_password=must_change_password,
                    updated_at=datetime.now(UTC),
                )
            )
            await session.commit()

    async def touch_last_login(self, user_id: str) -> None:
        """Record the current time as the user's last successful login."""
        async with self.session_factory() as session:
            await session.execute(
                update(User).where(User.id == user_id).values(last_login_at=datetime.now(UTC))
            )
            await session.commit()

    async def delete_user(self, user_id: str, reassign_cases_to: str | None = None) -> bool:
        """Delete a user, cascading their sessions and team memberships.

        Personal cases the user owns are reassigned to ``reassign_cases_to``
        (typically the acting admin) rather than orphaned. Sessions and
        memberships cascade via FK ``ON DELETE CASCADE``. Returns True if the
        user existed and was removed.
        """
        async with self.session_factory() as session:
            user = await session.get(User, user_id)
            if user is None:
                return False
            if reassign_cases_to is not None:
                await session.execute(
                    update(Case).where(Case.owner_id == user_id).values(owner_id=reassign_cases_to)
                )
            await session.delete(user)
            await session.commit()
            return True

    async def owned_case_count(self, user_id: str) -> int:
        """Return how many cases this user owns (used to gate deletion without reassignment)."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(func.count()).select_from(Case).where(Case.owner_id == user_id)
            )
            return int(result.scalar_one())

    # ------------------------------------------------------------------
    # Sessions
    # ------------------------------------------------------------------

    async def create_session(
        self,
        session_id: str,
        user_id: str,
        expires_at: datetime,
        ip: str | None = None,
        user_agent: str | None = None,
    ) -> Session:
        """Create a new login session."""
        row = Session(
            id=session_id, user_id=user_id, expires_at=expires_at, ip=ip, user_agent=user_agent
        )
        async with self.session_factory() as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def get_session(self, session_id: str) -> Session | None:
        """Return a session by ID, or None if not found."""
        async with self.session_factory() as session:
            return await session.get(Session, session_id)

    async def touch_session(self, session_id: str) -> None:
        """Update a session's last-seen timestamp."""
        async with self.session_factory() as session:
            await session.execute(
                update(Session)
                .where(Session.id == session_id)
                .values(last_seen_at=datetime.now(UTC))
            )
            await session.commit()

    async def revoke_session(self, session_id: str) -> bool:
        """Mark a single session as revoked (used by logout). Returns True if it existed."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(Session).where(Session.id == session_id).values(revoked=True)
            )
            await session.commit()
            return result.rowcount > 0

    async def revoke_user_sessions(self, user_id: str) -> int:
        """Revoke all of a user's sessions (used on password change/rotation)."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(Session).where(Session.user_id == user_id).values(revoked=True)
            )
            await session.commit()
            return result.rowcount

    async def purge_expired_sessions(self) -> int:
        """Delete sessions past their expiry. Safe to call periodically or at login time."""
        async with self.session_factory() as session:
            result = await session.execute(
                delete(Session).where(Session.expires_at < datetime.now(UTC))
            )
            await session.commit()
            return result.rowcount

    # ------------------------------------------------------------------
    # Teams & memberships
    # ------------------------------------------------------------------

    async def create_team(self, team_id: str, name: str, description: str | None = None) -> Team:
        """Create a new investigation team."""
        team = Team(id=team_id, name=name, description=description)
        async with self.session_factory() as session:
            session.add(team)
            await session.commit()
            await session.refresh(team)
            return team

    async def get_team(self, team_id: str) -> Team | None:
        """Return a team by ID, or None if not found."""
        async with self.session_factory() as session:
            return await session.get(Team, team_id)

    async def get_team_by_name(self, name: str) -> Team | None:
        """Return a team by its (unique) name, or None if not found."""
        async with self.session_factory() as session:
            result = await session.execute(select(Team).where(Team.name == name))
            return result.scalar_one_or_none()

    async def list_teams(self) -> list[Team]:
        """Return all teams ordered by name."""
        async with self.session_factory() as session:
            result = await session.execute(select(Team).order_by(Team.name.asc()))
            return list(result.scalars().all())

    async def delete_team(self, team_id: str) -> bool:
        """Delete a team; its memberships cascade and its cases become personal.

        Returns True if the team existed and was removed.
        """
        async with self.session_factory() as session:
            team = await session.get(Team, team_id)
            if team is None:
                return False
            await session.execute(update(Case).where(Case.team_id == team_id).values(team_id=None))
            await session.delete(team)
            await session.commit()
            return True

    async def add_membership(
        self, team_id: str, user_id: str, role: str = "member"
    ) -> TeamMembership:
        """Add a user to a team with the given role."""
        membership = TeamMembership(team_id=team_id, user_id=user_id, role=role)
        async with self.session_factory() as session:
            session.add(membership)
            await session.commit()
            await session.refresh(membership)
            return membership

    async def remove_membership(self, team_id: str, user_id: str) -> bool:
        """Remove a user from a team. Returns True if the membership existed."""
        async with self.session_factory() as session:
            result = await session.execute(
                delete(TeamMembership).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
                )
            )
            await session.commit()
            return result.rowcount > 0

    async def set_membership_role(self, team_id: str, user_id: str, role: str) -> bool:
        """Change a member's role within a team. Returns True if the membership existed."""
        async with self.session_factory() as session:
            result = await session.execute(
                update(TeamMembership)
                .where(TeamMembership.team_id == team_id, TeamMembership.user_id == user_id)
                .values(role=role)
            )
            await session.commit()
            return result.rowcount > 0

    async def get_membership(self, team_id: str, user_id: str) -> TeamMembership | None:
        """Return a single membership row, or None if the user isn't on the team."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(TeamMembership).where(
                    TeamMembership.team_id == team_id, TeamMembership.user_id == user_id
                )
            )
            return result.scalar_one_or_none()

    async def list_memberships(self, team_id: str) -> list[TeamMembership]:
        """Return all memberships for a team."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(TeamMembership).where(TeamMembership.team_id == team_id)
            )
            return list(result.scalars().all())

    async def list_user_memberships(self, user_id: str) -> list[TeamMembership]:
        """Return all team memberships for a user."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(TeamMembership).where(TeamMembership.user_id == user_id)
            )
            return list(result.scalars().all())

    async def list_teams_for_user(self, user_id: str) -> list[tuple[Team, str]]:
        """Return (team, role) for every team the user belongs to, one query."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(Team, TeamMembership.role)
                .join(TeamMembership, TeamMembership.team_id == Team.id)
                .where(TeamMembership.user_id == user_id)
            )
            return list(result.all())

    async def list_members_with_users(self, team_id: str) -> list[tuple[User, str]]:
        """Return (user, role) for every member of a team, one query."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(User, TeamMembership.role)
                .join(TeamMembership, TeamMembership.user_id == User.id)
                .where(TeamMembership.team_id == team_id)
            )
            return list(result.all())

    async def list_unassigned_users(self) -> list[User]:
        """Return users with no team membership at all, one query."""
        async with self.session_factory() as session:
            result = await session.execute(
                select(User)
                .outerjoin(TeamMembership, TeamMembership.user_id == User.id)
                .where(TeamMembership.user_id.is_(None))
                .order_by(User.username.asc())
            )
            return list(result.scalars().all())

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------

    async def record_audit(
        self,
        action: str,
        user_id: str | None = None,
        username_snapshot: str | None = None,
        actor: User | None = None,
        method: str | None = None,
        path: str | None = None,
        route: str | None = None,
        case_id: str | None = None,
        target_type: str | None = None,
        target_id: str | None = None,
        status_code: int | None = None,
        ip: str | None = None,
        user_agent: str | None = None,
        detail: dict | None = None,
    ) -> None:
        """Append one audit-log row. Never raises on log-only failures upstream —
        callers should treat this as best-effort so a logging hiccup never blocks
        the underlying request.

        Pass ``actor`` (the ``User`` who performed the action) instead of
        ``user_id``/``username_snapshot`` separately — it derives both,
        removing the risk of a call site setting one but forgetting the
        other. ``user_id``/``username_snapshot`` remain for the anonymous
        cases (failed login, unauthenticated request) where there's no
        ``User`` to pass.
        """
        if actor is not None:
            user_id = actor.id
            username_snapshot = actor.username
        row = AuditLog(
            id=generate_id(f"audit_{action}"),
            user_id=user_id,
            username_snapshot=username_snapshot,
            action=action,
            method=method,
            path=path,
            route=route,
            case_id=case_id,
            target_type=target_type,
            target_id=target_id,
            status_code=status_code,
            ip=ip,
            user_agent=user_agent,
            detail=detail,
        )
        try:
            async with self.session_factory() as session:
                session.add(row)
                await session.commit()
        except Exception:
            logger.exception("Failed to record audit row (action=%s)", action)

    async def query_audit(
        self,
        user_id: str | None = None,
        case_id: str | None = None,
        action: str | None = None,
        limit: int = 500,
    ) -> list[AuditLog]:
        """Return audit rows matching the given filters, newest first."""
        conditions = []
        if user_id is not None:
            conditions.append(AuditLog.user_id == user_id)
        if case_id is not None:
            conditions.append(AuditLog.case_id == case_id)
        if action is not None:
            conditions.append(AuditLog.action == action)
        async with self.session_factory() as session:
            result = await session.execute(
                select(AuditLog).where(*conditions).order_by(AuditLog.timestamp.desc()).limit(limit)
            )
            return list(result.scalars().all())


def generate_id(base: str) -> str:
    """Return a URL-safe identifier from ``base`` with a short random suffix."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in base)
    suffix = uuid.uuid4().hex[:8]
    return f"{safe[:55]}_{suffix}"
