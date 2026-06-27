"""Tests for Annotation model and PostgresStore annotation methods."""

from __future__ import annotations

import pytest
import pytest_asyncio

from tracevector.db.postgres import Annotation, PostgresStore


@pytest_asyncio.fixture()
async def store(tmp_path):
    """In-memory SQLite store for fast, dependency-free annotation tests."""
    db_path = tmp_path / "test_annotations.db"
    url = f"sqlite+aiosqlite:///{db_path}"
    s = PostgresStore(url=url)
    await s.init_schema()
    # Seed a minimal case and timeline so FKs don't block (no FK constraints in SQLite by default).
    return s


# ---------------------------------------------------------------------------
# Annotation.to_dict shape
# ---------------------------------------------------------------------------


def test_annotation_to_dict_shape():
    """to_dict must return all keys the frontend Annotation interface expects."""
    from datetime import UTC, datetime

    ann = Annotation(
        id="ann_abc123",
        case_id="case1",
        timeline_id="tl1",
        event_id="evt1",
        annotation_type="tag",
        content="malware",
        created_by=None,
        origin="user",
        details=None,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    d = ann.to_dict()
    assert set(d.keys()) == {
        "id",
        "event_id",
        "annotation_type",
        "content",
        "created_at",
        "created_by",
        "origin",
        "details",
    }
    assert d["id"] == "ann_abc123"
    assert d["event_id"] == "evt1"
    assert d["annotation_type"] == "tag"
    assert d["content"] == "malware"
    assert d["created_by"] is None
    assert d["origin"] == "user"
    assert d["details"] is None
    assert "2024-01-01" in d["created_at"]


# ---------------------------------------------------------------------------
# CRUD round-trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_annotation(store: PostgresStore):
    """Create an annotation and retrieve it with list_annotations."""
    ann = await store.create_annotation(
        case_id="c1",
        timeline_id="t1",
        event_id="e1",
        annotation_id="ann_001",
        annotation_type="tag",
        content="suspicious",
    )
    assert ann.id == "ann_001"
    assert ann.annotation_type == "tag"
    assert ann.content == "suspicious"

    results = await store.list_annotations("c1", "t1", "e1")
    assert len(results) == 1
    assert results[0].id == "ann_001"


@pytest.mark.asyncio
async def test_list_annotations_empty(store: PostgresStore):
    results = await store.list_annotations("c1", "t1", "nonexistent_event")
    assert results == []


@pytest.mark.asyncio
async def test_list_annotations_ordering(store: PostgresStore):
    """list_annotations returns annotations oldest-first."""
    await store.create_annotation(
        case_id="c1",
        timeline_id="t1",
        event_id="e2",
        annotation_id="ann_b",
        annotation_type="comment",
        content="second",
    )
    await store.create_annotation(
        case_id="c1",
        timeline_id="t1",
        event_id="e2",
        annotation_id="ann_a",
        annotation_type="tag",
        content="first",
    )
    results = await store.list_annotations("c1", "t1", "e2")
    assert len(results) == 2
    # SQLite insert order matches created_at ordering here.
    contents = [r.content for r in results]
    assert "second" in contents and "first" in contents


@pytest.mark.asyncio
async def test_list_timeline_annotations(store: PostgresStore):
    """list_timeline_annotations returns all annotations for a timeline."""
    for i, event_id in enumerate(["e3", "e4", "e4"]):
        await store.create_annotation(
            case_id="c2",
            timeline_id="t2",
            event_id=event_id,
            annotation_id=f"ann_tl_{i}",
            annotation_type="tag",
            content=f"label_{i}",
        )
    # Unrelated timeline — must not appear.
    await store.create_annotation(
        case_id="c2",
        timeline_id="other",
        event_id="e99",
        annotation_id="ann_other",
        annotation_type="tag",
        content="noise",
    )

    results = await store.list_timeline_annotations("c2", "t2")
    assert len(results) == 3
    ids = {r.id for r in results}
    assert "ann_tl_0" in ids and "ann_tl_1" in ids and "ann_tl_2" in ids
    assert "ann_other" not in ids


@pytest.mark.asyncio
async def test_delete_annotation_existing(store: PostgresStore):
    await store.create_annotation(
        case_id="c3",
        timeline_id="t3",
        event_id="e5",
        annotation_id="ann_del",
        annotation_type="comment",
        content="to be deleted",
    )
    deleted = await store.delete_annotation("c3", "e5", "ann_del")
    assert deleted is True
    results = await store.list_annotations("c3", "t3", "e5")
    assert results == []


@pytest.mark.asyncio
async def test_delete_annotation_nonexistent(store: PostgresStore):
    deleted = await store.delete_annotation("c_none", "e_none", "ann_none")
    assert deleted is False


@pytest.mark.asyncio
async def test_to_dict_round_trip(store: PostgresStore):
    """to_dict on a persisted annotation matches the Annotation interface."""
    ann = await store.create_annotation(
        case_id="c4",
        timeline_id="t4",
        event_id="e6",
        annotation_id="ann_dict",
        annotation_type="comment",
        content="looks like C2 traffic",
        created_by="analyst@example.com",
    )
    d = ann.to_dict()
    assert d["event_id"] == "e6"
    assert d["annotation_type"] == "comment"
    assert d["content"] == "looks like C2 traffic"
    assert d["created_by"] == "analyst@example.com"
    assert d["created_at"] is not None
    assert d["origin"] == "user"
    assert d["details"] is None


# ---------------------------------------------------------------------------
# System annotation (origin / details)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_system_annotation_origin_and_details(store: PostgresStore):
    """System annotations store origin='system' and structured details."""
    details = {
        "method": "centroid-distance",
        "distance": 0.87,
        "rank": 1,
        "of": 50,
        "sample_size": 5000,
        "embedding_config_hash": "abc123",
    }
    ann = await store.create_annotation(
        case_id="c5",
        timeline_id="t5",
        event_id="e7",
        annotation_id="ann_sys",
        annotation_type="outlier",
        content="Outlier — distance 0.87",
        origin="system",
        details=details,
    )
    assert ann.origin == "system"
    assert ann.details is not None
    assert ann.details["rank"] == 1
    d = ann.to_dict()
    assert d["origin"] == "system"
    assert d["details"]["distance"] == 0.87


@pytest.mark.asyncio
async def test_delete_annotation_cannot_delete_system(store: PostgresStore):
    """delete_annotation must not remove system-origin annotations."""
    await store.create_annotation(
        case_id="c6",
        timeline_id="t6",
        event_id="e8",
        annotation_id="ann_sys2",
        annotation_type="outlier",
        content="system content",
        origin="system",
    )
    # delete_annotation only removes origin='user' rows.
    deleted = await store.delete_annotation("c6", "e8", "ann_sys2")
    assert deleted is False
    results = await store.list_annotations("c6", "t6", "e8")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_bulk_create_annotations(store: PostgresStore):
    """bulk_create_annotations inserts multiple rows atomically."""
    rows = [
        {
            "annotation_id": f"bulk_{i}",
            "case_id": "c7",
            "timeline_id": "t7",
            "event_id": f"e{i}",
            "annotation_type": "outlier",
            "content": f"Outlier rank {i}",
            "origin": "system",
            "details": {"rank": i},
        }
        for i in range(5)
    ]
    count = await store.bulk_create_annotations(rows)
    assert count == 5
    results = await store.list_timeline_annotations("c7", "t7")
    assert len(results) == 5
    assert all(r.origin == "system" for r in results)


@pytest.mark.asyncio
async def test_delete_system_annotations(store: PostgresStore):
    """delete_system_annotations removes only system outlier rows."""
    # System outlier annotations.
    await store.bulk_create_annotations(
        [
            {
                "annotation_id": f"sys_{i}",
                "case_id": "c8",
                "timeline_id": "t8",
                "event_id": f"e{i}",
                "annotation_type": "outlier",
                "content": "outlier",
                "origin": "system",
            }
            for i in range(3)
        ]
    )
    # Human tag that must survive.
    await store.create_annotation(
        case_id="c8",
        timeline_id="t8",
        event_id="e0",
        annotation_id="human_tag",
        annotation_type="tag",
        content="suspicious",
        origin="user",
    )

    deleted_count = await store.delete_system_annotations("c8", "t8", "outlier")
    assert deleted_count == 3

    remaining = await store.list_timeline_annotations("c8", "t8")
    assert len(remaining) == 1
    assert remaining[0].id == "human_tag"


@pytest.mark.asyncio
async def test_delete_system_annotations_idempotent(store: PostgresStore):
    """delete_system_annotations is a no-op when nothing matches."""
    count = await store.delete_system_annotations("c_empty", "t_empty", "outlier")
    assert count == 0
