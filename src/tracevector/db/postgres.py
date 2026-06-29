"""PostgreSQL connection and metadata models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Index, String, func, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

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


class Timeline(Base):
    """A timeline (data source) inside a case."""

    __tablename__ = "timelines"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(4096), nullable=True)
    parser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Per-source field selection chosen by the analyst in the embedding wizard.
    # Shape: {"version": 1, "sources": {"<source>": ["message", "attr:key", ...]}}
    # None when embeddings were run with the legacy all-fields behaviour.
    embedding_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    event_count: Mapped[int] = mapped_column(default=0)
    vector_count: Mapped[int] = mapped_column(default=0)
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
            "case_id": self.case_id,
            "name": self.name,
            "description": self.description,
            "parser": self.parser,
            "embedding_model": self.embedding_model,
            "embedding_config": self.embedding_config,
            "event_count": self.event_count,
            "vector_count": self.vector_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


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


class TimelineUpload(Base):
    """A source file that has been uploaded and ingested for a timeline.

    The composite unique index on (case_id, timeline_id, file_hash) guarantees
    that the same file cannot be ingested twice for the same timeline.
    """

    __tablename__ = "timeline_uploads"
    __table_args__ = (
        Index(
            "ix_timeline_uploads_case_timeline_hash",
            "case_id",
            "timeline_id",
            "file_hash",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    filename: Mapped[str | None] = mapped_column(String(255), nullable=True)
    parser: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_count: Mapped[int] = mapped_column(default=0)
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
            "file_hash": self.file_hash,
            "filename": self.filename,
            "parser": self.parser,
            "event_count": self.event_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


class Annotation(Base):
    """A tag or comment annotation attached to a single event.

    The ``origin`` column distinguishes human annotations (``"user"``, the
    default) from machine-generated ones (``"system"``).  System annotations are
    written by the outlier-detection pipeline and carry structured math in the
    ``details`` JSON column; they are presented differently in the UI and cannot
    be deleted through the normal annotation delete endpoint.
    """

    __tablename__ = "annotations"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    timeline_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
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

        On PostgreSQL, also runs idempotent ``ALTER TABLE`` statements so that
        existing development databases gain the ``origin`` and ``details``
        columns added to ``Annotation`` without requiring Alembic migrations.
        These statements are skipped on SQLite (used in tests) because SQLite
        does not support ``IF NOT EXISTS`` column guards or the ``JSONB`` type.
        """
        from sqlalchemy import text

        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
            # Guard: only needed on PostgreSQL; create_all already handles new DBs.
            dialect = conn.dialect.name
            if dialect == "postgresql":
                await conn.execute(
                    text(
                        "ALTER TABLE annotations "
                        "ADD COLUMN IF NOT EXISTS origin VARCHAR(16) NOT NULL DEFAULT 'user'"
                    )
                )
                await conn.execute(
                    text("ALTER TABLE annotations ADD COLUMN IF NOT EXISTS details JSONB")
                )
                await conn.execute(
                    text("ALTER TABLE timelines ADD COLUMN IF NOT EXISTS embedding_config JSONB")
                )

    async def get_case(self, case_id: str) -> Case | None:
        """Return a case by ID, or None if not found."""
        async with self.session_factory() as session:
            return await session.get(Case, case_id)

    async def create_case(self, case_id: str, name: str, description: str | None = None) -> Case:
        """Create a new case."""
        case = Case(id=case_id, name=name, description=description)
        async with self.session_factory() as session:
            session.add(case)
            await session.commit()
            await session.refresh(case)
            return case

    async def list_cases(self) -> list[Case]:
        """Return all cases ordered by creation time."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(select(Case).order_by(Case.created_at.desc()))
            return list(result.scalars().all())

    async def get_timeline(self, case_id: str, timeline_id: str) -> Timeline | None:
        """Return a timeline by case and timeline IDs."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Timeline).where(
                    Timeline.case_id == case_id,
                    Timeline.id == timeline_id,
                )
            )
            return result.scalar_one_or_none()

    async def create_timeline(
        self,
        case_id: str,
        timeline_id: str,
        name: str,
        description: str | None = None,
        parser: str | None = None,
        embedding_model: str | None = None,
    ) -> Timeline:
        """Create a new timeline within a case."""
        timeline = Timeline(
            id=timeline_id,
            case_id=case_id,
            name=name,
            description=description,
            parser=parser,
            embedding_model=embedding_model,
        )
        async with self.session_factory() as session:
            session.add(timeline)
            await session.commit()
            await session.refresh(timeline)
            return timeline

    async def update_timeline_counts(
        self,
        case_id: str,
        timeline_id: str,
        event_count: int | None = None,
        vector_count: int | None = None,
    ) -> None:
        """Update stored event/vector counts for a timeline.

        ``event_count`` is treated as a delta and added atomically to the
        stored value (preventing lost updates under concurrent uploads).
        ``vector_count`` is set to the supplied absolute value.
        Pass ``None`` for a count that should not be changed.
        """
        values: dict = {"updated_at": datetime.now(UTC)}
        if vector_count is not None:
            values["vector_count"] = vector_count
        async with self.session_factory() as session:
            if event_count is not None:
                # Atomic increment avoids the read-then-write race when two
                # uploads complete concurrently for the same timeline.
                await session.execute(
                    update(Timeline)
                    .where(Timeline.id == timeline_id, Timeline.case_id == case_id)
                    .values(
                        event_count=Timeline.event_count + event_count,
                        **{k: v for k, v in values.items()},
                    )
                )
            elif values:
                await session.execute(
                    update(Timeline)
                    .where(Timeline.id == timeline_id, Timeline.case_id == case_id)
                    .values(**values)
                )
            await session.commit()

    async def get_timeline_upload_by_hash(
        self,
        case_id: str,
        timeline_id: str,
        file_hash: str,
    ) -> TimelineUpload | None:
        """Return an existing upload row for the same file hash, if any."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(TimelineUpload).where(
                    TimelineUpload.case_id == case_id,
                    TimelineUpload.timeline_id == timeline_id,
                    TimelineUpload.file_hash == file_hash,
                )
            )
            return result.scalar_one_or_none()

    async def create_timeline_upload(
        self,
        case_id: str,
        timeline_id: str,
        upload_id: str,
        file_hash: str,
        filename: str | None,
        event_count: int,
        parser: str | None = None,
    ) -> TimelineUpload:
        """Record an uploaded source file for a timeline."""
        upload = TimelineUpload(
            id=upload_id,
            case_id=case_id,
            timeline_id=timeline_id,
            file_hash=file_hash,
            filename=filename,
            parser=parser,
            event_count=event_count,
        )
        async with self.session_factory() as session:
            session.add(upload)
            await session.commit()
            await session.refresh(upload)
            return upload

    async def delete_timeline_uploads_for_timeline(
        self,
        case_id: str,
        timeline_id: str,
    ) -> int:
        """Remove all upload records for a timeline. Returns deleted row count."""
        from sqlalchemy import delete

        async with self.session_factory() as session:
            result = await session.execute(
                delete(TimelineUpload).where(
                    TimelineUpload.case_id == case_id,
                    TimelineUpload.timeline_id == timeline_id,
                )
            )
            await session.commit()
            return result.rowcount

    async def update_timeline_embedding_config(
        self,
        case_id: str,
        timeline_id: str,
        embedding_config: dict,
    ) -> None:
        """Persist the analyst's per-source field selection on the timeline."""
        timeline = await self.get_timeline(case_id, timeline_id)
        if timeline is None:
            return
        timeline.embedding_config = embedding_config
        timeline.updated_at = datetime.now(UTC)
        async with self.session_factory() as session:
            session.add(timeline)
            await session.commit()

    async def list_event_ids_by_annotation_type(
        self,
        case_id: str,
        timeline_id: str,
        annotation_type: str,
        origin: str = "user",
    ) -> list[str]:
        """Return the event_ids that have at least one annotation of the given type.

        Used by the anomaly service to retrieve the analyst-defined normal set.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Annotation.event_id).where(
                    Annotation.case_id == case_id,
                    Annotation.timeline_id == timeline_id,
                    Annotation.annotation_type == annotation_type,
                    Annotation.origin == origin,
                )
            )
            return [row[0] for row in result.all()]

    async def list_timelines(self, case_id: str) -> list[Timeline]:
        """Return all timelines for a case ordered by creation time."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Timeline)
                .where(Timeline.case_id == case_id)
                .order_by(Timeline.created_at.desc())
            )
            return list(result.scalars().all())

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

    async def delete_timeline(self, case_id: str, timeline_id: str) -> bool:
        """Delete a timeline row.

        Returns True if a row was removed, False if it did not exist.
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
                return False
            await session.delete(timeline)
            await session.commit()
            return True

    async def delete_case(self, case_id: str) -> bool:
        """Delete a case and all its timeline rows in one transaction.

        Returns True if the case existed and was removed, False otherwise.
        """
        from sqlalchemy import delete

        async with self.session_factory() as session:
            case = await session.get(Case, case_id)
            if case is None:
                return False
            await session.execute(delete(Timeline).where(Timeline.case_id == case_id))
            await session.delete(case)
            await session.commit()
            return True

    async def list_annotations(
        self, case_id: str, timeline_id: str, event_id: str
    ) -> list[Annotation]:
        """Return annotations for a single event, oldest first."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Annotation)
                .where(
                    Annotation.case_id == case_id,
                    Annotation.timeline_id == timeline_id,
                    Annotation.event_id == event_id,
                )
                .order_by(Annotation.created_at.asc())
            )
            return list(result.scalars().all())

    async def list_timeline_annotations(self, case_id: str, timeline_id: str) -> list[Annotation]:
        """Return all annotations for a timeline (used for bulk table chips)."""
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(Annotation)
                .where(
                    Annotation.case_id == case_id,
                    Annotation.timeline_id == timeline_id,
                )
                .order_by(Annotation.created_at.asc())
            )
            return list(result.scalars().all())

    async def create_annotation(
        self,
        case_id: str,
        timeline_id: str,
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
            timeline_id=timeline_id,
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
                timeline_id=row["timeline_id"],
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
        self, case_id: str, timeline_id: str, annotation_type: str
    ) -> int:
        """Delete all system-origin annotations of a given type for a timeline.

        Used before re-writing outlier tags so that a fresh "Tag outliers" run
        does not accumulate duplicate machine annotations.  Returns the count of
        deleted rows.
        """
        from sqlalchemy import delete

        async with self.session_factory() as session:
            result = await session.execute(
                delete(Annotation).where(
                    Annotation.case_id == case_id,
                    Annotation.timeline_id == timeline_id,
                    Annotation.annotation_type == annotation_type,
                    Annotation.origin == "system",
                )
            )
            await session.commit()
            return result.rowcount

    async def delete_annotation(self, case_id: str, event_id: str, annotation_id: str) -> bool:
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


def generate_id(base: str) -> str:
    """Return a URL-safe identifier from ``base`` with a short random suffix."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in base)
    suffix = uuid.uuid4().hex[:8]
    return f"{safe[:55]}_{suffix}"
