"""PostgreSQL connection and metadata models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    delete,
    func,
    select,
    text,
    update,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship, selectinload

from tracevector.core.config import get_settings


class Base(DeclarativeBase):
    """SQLAlchemy declarative base for TraceVector metadata."""


class Case(Base):
    """An investigation case."""

    __tablename__ = "cases"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
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


class Source(Base):
    """One ingested file in a case.

    The Source is the atomic unit of forensic provenance and immutability.
    Events and vectors are scoped by ``source_id`` so a Source can be reused
    across multiple Timelines without duplicating data.
    """

    __tablename__ = "sources"
    __table_args__ = (
        Index("ix_sources_case_id_file_hash", "case_id", "file_hash", unique=True),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(default=0)
    parser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_version: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_count: Mapped[int] = mapped_column(default=0)
    vector_count: Mapped[int] = mapped_column(default=0)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Per-source field selection chosen by the analyst in the embedding wizard.
    # Shape: {"version": 1, "artifacts": {"<artifact>": ["message", "attr:key", ...]}}
    embedding_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
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
            "embedding_model": self.embedding_model,
            "embedding_config": self.embedding_config,
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

    # --- Embedding state (all nullable; None → not yet embedded) -------------
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    embedding_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    embedding_config_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Snapshot of source_ids at embed time; used to derive staleness.
    embedded_source_ids: Mapped[list | None] = mapped_column(JSON, nullable=True)
    embedded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

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
        }


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
        """Create metadata tables if they do not exist.

        Also runs additive ALTER TABLE migrations for columns added after the
        initial schema creation (Postgres ``IF NOT EXISTS`` prevents errors on
        re-runs).
        """
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Additive migrations — only supported on PostgreSQL; SQLite (used
            # in tests) handles all columns via create_all on fresh databases.
            if conn.dialect.name == "postgresql":
                for stmt in (
                    "ALTER TABLE timelines ADD COLUMN IF NOT EXISTS embedding_model VARCHAR(255)",
                    "ALTER TABLE timelines ADD COLUMN IF NOT EXISTS embedding_config JSON",
                    "ALTER TABLE timelines ADD COLUMN IF NOT EXISTS embedding_config_hash VARCHAR(128)",
                    "ALTER TABLE timelines ADD COLUMN IF NOT EXISTS embedded_source_ids JSON",
                    "ALTER TABLE timelines ADD COLUMN IF NOT EXISTS embedded_at TIMESTAMPTZ",
                    # Model-refactor migration: annotations scoped by source_id instead
                    # of timeline_id.  Add the new column and back-fill from the old one
                    # so existing annotations remain visible after upgrade.
                    "ALTER TABLE annotations ADD COLUMN IF NOT EXISTS source_id VARCHAR(64)",
                    "UPDATE annotations SET source_id = timeline_id WHERE source_id IS NULL",
                ):
                    await conn.execute(text(stmt))

    async def get_case(self, case_id: str) -> Case | None:
        """Return a case by ID, or None if not found."""
        async with self.session_factory() as session:
            return await session.get(Case, case_id)

    async def create_case(self, case_id: str, name: str, description: str | None = None) -> Case:
        """Create a new case and its default timeline."""
        case = Case(id=case_id, name=name, description=description)
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
        """Return all cases ordered by creation time."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(select(Case).order_by(Case.created_at.desc()))
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
                select(Source)
                .where(Source.case_id == case_id)
                .order_by(Source.created_at.desc())
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
    ) -> Source:
        """Create a new source record within a case."""
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

    async def update_source_embedding_config(
        self,
        case_id: str,
        source_id: str,
        embedding_model: str | None = None,
        embedding_config: dict | None = None,
    ) -> None:
        """Persist the analyst's per-source field selection on the source."""
        async with self.session_factory() as session:
            values: dict = {"updated_at": datetime.now(UTC)}
            if embedding_model is not None:
                values["embedding_model"] = embedding_model
            if embedding_config is not None:
                values["embedding_config"] = embedding_config
            result = await session.execute(
                update(Source)
                .where(Source.case_id == case_id, Source.id == source_id)
                .values(**values)
            )
            if result.rowcount == 0:
                return
            await session.commit()

    async def delete_source(self, case_id: str, source_id: str) -> bool:
        """Delete a source row.

        Returns True if a row was removed, False if it did not exist.
        """
        from sqlalchemy import select

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
            await session.delete(source)
            await session.commit()
            return True

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
    ) -> Timeline:
        """Create a new timeline within a case and optionally attach sources."""
        timeline = Timeline(
            id=timeline_id,
            case_id=case_id,
            name=name,
            description=description,
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
            await session.delete(timeline)
            await session.commit()
            return True

    async def delete_case(self, case_id: str) -> bool:
        """Delete a case and all its timelines and sources in one transaction.

        Returns True if the case existed and was removed, False otherwise.
        """
        from sqlalchemy import delete

        async with self.session_factory() as session:
            case = await session.get(Case, case_id)
            if case is None:
                return False
            await session.execute(delete(Timeline).where(Timeline.case_id == case_id))
            await session.execute(delete(Source).where(Source.case_id == case_id))
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
    ) -> int:
        """Delete all system-origin annotations of a given type for sources.

        Used before re-writing outlier tags so that a fresh "Tag outliers" run
        does not accumulate duplicate machine annotations.  Returns the count of
        deleted rows.
        """
        from sqlalchemy import delete

        async with self.session_factory() as session:
            result = await session.execute(
                delete(Annotation).where(
                    Annotation.case_id == case_id,
                    Annotation.source_id.in_(source_ids),
                    Annotation.annotation_type == annotation_type,
                    Annotation.origin == "system",
                )
            )
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
    ) -> list[str]:
        """Return the event_ids that have at least one annotation of the given type.

        Used by the anomaly service to retrieve the analyst-defined normal set.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Annotation.event_id)
                .where(
                    Annotation.case_id == case_id,
                    Annotation.source_id.in_(source_ids),
                    Annotation.annotation_type == annotation_type,
                    Annotation.origin == origin,
                )
                .distinct()
            )
            return [row[0] for row in result.all()]


def generate_id(base: str) -> str:
    """Return a URL-safe identifier from ``base`` with a short random suffix."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in base)
    suffix = uuid.uuid4().hex[:8]
    return f"{safe[:55]}_{suffix}"
