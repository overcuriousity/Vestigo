"""PostgreSQL connection and metadata models."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import DateTime, JSON, String, func
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
        """Return a serializable dictionary matching the SavedView frontend interface."""
        return {
            "id": self.id,
            "case_id": self.case_id,
            "name": self.name,
            "query": self.query,
            "filter": self.view_filter or {},
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
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
        """Create metadata tables if they do not exist."""
        async with self.engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

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

        Pass ``None`` for a count that should not be changed.
        """
        timeline = await self.get_timeline(case_id, timeline_id)
        if timeline is None:
            return
        if event_count is not None:
            timeline.event_count = event_count
        if vector_count is not None:
            timeline.vector_count = vector_count
        timeline.updated_at = datetime.now(UTC)
        async with self.session_factory() as session:
            session.add(timeline)
            await session.commit()

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
                select(View)
                .where(View.case_id == case_id)
                .order_by(View.created_at.desc())
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
        from sqlalchemy import delete, select

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
        from sqlalchemy import delete, select

        async with self.session_factory() as session:
            case = await session.get(Case, case_id)
            if case is None:
                return False
            await session.execute(delete(Timeline).where(Timeline.case_id == case_id))
            await session.delete(case)
            await session.commit()
            return True


def generate_id(base: str) -> str:
    """Return a URL-safe identifier from ``base`` with a short random suffix."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in base)
    suffix = uuid.uuid4().hex[:8]
    return f"{safe[:55]}_{suffix}"
