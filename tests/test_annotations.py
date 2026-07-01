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
    # Seed a minimal case and source so FKs don't block (no FK constraints in SQLite by default).
    yield s
    await s.engine.dispose()


# ---------------------------------------------------------------------------
# Annotation.to_dict shape
# ---------------------------------------------------------------------------


def test_annotation_to_dict_shape():
    """to_dict must return all keys the frontend Annotation interface expects."""
    from datetime import UTC, datetime

    ann = Annotation(
        id="ann_abc123",
        case_id="case1",
        source_id="source1",
        event_id="evt1",
        annotation_type="tag",
        content="malware",
        created_by=None,
        origin="user",
        details=None,
        pinned=False,
        created_at=datetime(2024, 1, 1, tzinfo=UTC),
    )
    d = ann.to_dict()
    assert set(d.keys()) == {
        "id",
        "event_id",
        "source_id",
        "annotation_type",
        "content",
        "created_at",
        "created_by",
        "origin",
        "details",
        "pinned",
        "detector",
    }
    assert d["id"] == "ann_abc123"
    assert d["event_id"] == "evt1"
    assert d["source_id"] == "source1"
    assert d["annotation_type"] == "tag"
    assert d["content"] == "malware"
    assert d["created_by"] is None
    assert d["origin"] == "user"
    assert d["details"] is None
    assert d["pinned"] is False
    assert "2024-01-01" in d["created_at"]


# ---------------------------------------------------------------------------
# CRUD round-trips
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_and_list_annotation(store: PostgresStore):
    """Create an annotation and retrieve it with list_annotations."""
    ann = await store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="e1",
        annotation_id="ann_001",
        annotation_type="tag",
        content="suspicious",
    )
    assert ann.id == "ann_001"
    assert ann.annotation_type == "tag"
    assert ann.content == "suspicious"

    results = await store.list_annotations("c1", "s1", "e1")
    assert len(results) == 1
    assert results[0].id == "ann_001"


@pytest.mark.asyncio
async def test_list_annotations_empty(store: PostgresStore):
    results = await store.list_annotations("c1", "s1", "nonexistent_event")
    assert results == []


@pytest.mark.asyncio
async def test_list_annotations_ordering(store: PostgresStore):
    """list_annotations returns annotations oldest-first."""
    await store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="e2",
        annotation_id="ann_b",
        annotation_type="comment",
        content="second",
    )
    await store.create_annotation(
        case_id="c1",
        source_id="s1",
        event_id="e2",
        annotation_id="ann_a",
        annotation_type="tag",
        content="first",
    )
    results = await store.list_annotations("c1", "s1", "e2")
    assert len(results) == 2
    # SQLite insert order matches created_at ordering here.
    contents = [r.content for r in results]
    assert "second" in contents and "first" in contents


@pytest.mark.asyncio
async def test_list_source_annotations(store: PostgresStore):
    """list_source_annotations returns all annotations for one or more sources."""
    for i, event_id in enumerate(["e3", "e4", "e4"]):
        await store.create_annotation(
            case_id="c2",
            source_id="s2",
            event_id=event_id,
            annotation_id=f"ann_s_{i}",
            annotation_type="tag",
            content=f"label_{i}",
        )
    # Unrelated source — must not appear.
    await store.create_annotation(
        case_id="c2",
        source_id="other",
        event_id="e99",
        annotation_id="ann_other",
        annotation_type="tag",
        content="noise",
    )

    results = await store.list_source_annotations("c2", ["s2"])
    assert len(results) == 3
    ids = {r.id for r in results}
    assert "ann_s_0" in ids and "ann_s_1" in ids and "ann_s_2" in ids
    assert "ann_other" not in ids


@pytest.mark.asyncio
async def test_delete_annotation_existing(store: PostgresStore):
    await store.create_annotation(
        case_id="c3",
        source_id="s3",
        event_id="e5",
        annotation_id="ann_del",
        annotation_type="comment",
        content="to be deleted",
    )
    deleted = await store.delete_annotation("c3", "e5", "ann_del")
    assert deleted is True
    results = await store.list_annotations("c3", "s3", "e5")
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
        source_id="s4",
        event_id="e6",
        annotation_id="ann_dict",
        annotation_type="comment",
        content="looks like C2 traffic",
        created_by="analyst@example.com",
    )
    d = ann.to_dict()
    assert d["event_id"] == "e6"
    assert d["source_id"] == "s4"
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
        "detector": "value_novelty",
        "method": "self-baseline",
        "field": "artifact",
        "value": "suspicious.exe",
        "count": 1,
        "total_events": 5000,
        "surprise": 8.517,
    }
    ann = await store.create_annotation(
        case_id="c5",
        source_id="s5",
        event_id="e7",
        annotation_id="ann_sys",
        annotation_type="anomaly",
        content="Rare value — artifact='suspicious.exe' (count 1, surprise 8.52)",
        origin="system",
        details=details,
    )
    assert ann.origin == "system"
    assert ann.details is not None
    assert ann.details["count"] == 1
    d = ann.to_dict()
    assert d["origin"] == "system"
    assert d["details"]["surprise"] == 8.517


@pytest.mark.asyncio
async def test_delete_annotation_cannot_delete_system(store: PostgresStore):
    """delete_annotation must not remove system-origin annotations."""
    await store.create_annotation(
        case_id="c6",
        source_id="s6",
        event_id="e8",
        annotation_id="ann_sys2",
        annotation_type="anomaly",
        content="system content",
        origin="system",
    )
    # delete_annotation only removes origin='user' rows.
    deleted = await store.delete_annotation("c6", "e8", "ann_sys2")
    assert deleted is False
    results = await store.list_annotations("c6", "s6", "e8")
    assert len(results) == 1


@pytest.mark.asyncio
async def test_bulk_create_annotations(store: PostgresStore):
    """bulk_create_annotations inserts multiple rows atomically."""
    rows = [
        {
            "annotation_id": f"bulk_{i}",
            "case_id": "c7",
            "source_id": "s7",
            "event_id": f"e{i}",
            "annotation_type": "anomaly",
            "content": f"Outlier rank {i}",
            "origin": "system",
            "details": {"rank": i},
        }
        for i in range(5)
    ]
    count = await store.bulk_create_annotations(rows)
    assert count == 5
    results = await store.list_source_annotations("c7", ["s7"])
    assert len(results) == 5
    assert all(r.origin == "system" for r in results)


@pytest.mark.asyncio
async def test_delete_system_annotations(store: PostgresStore):
    """delete_system_annotations removes only system anomaly rows."""
    # System anomaly annotations.
    await store.bulk_create_annotations(
        [
            {
                "annotation_id": f"sys_{i}",
                "case_id": "c8",
                "source_id": "s8",
                "event_id": f"e{i}",
                "annotation_type": "anomaly",
                "content": "rare value detected",
                "origin": "system",
            }
            for i in range(3)
        ]
    )
    # Human tag that must survive.
    await store.create_annotation(
        case_id="c8",
        source_id="s8",
        event_id="e0",
        annotation_id="human_tag",
        annotation_type="tag",
        content="suspicious",
        origin="user",
    )

    deleted_count = await store.delete_system_annotations("c8", ["s8"], "anomaly")
    assert deleted_count == 3

    remaining = await store.list_source_annotations("c8", ["s8"])
    assert len(remaining) == 1
    assert remaining[0].id == "human_tag"


@pytest.mark.asyncio
async def test_delete_system_annotations_idempotent(store: PostgresStore):
    """delete_system_annotations is a no-op when nothing matches."""
    count = await store.delete_system_annotations("c8", ["s8"], "anomaly")
    assert count == 0


@pytest.mark.asyncio
async def test_delete_system_annotations_preserves_pinned(store: PostgresStore):
    """A pinned system annotation (from the per-event 'Persist' action) must
    survive the bulk 'Tag N as anomaly' re-run's clear-and-replace, even if a
    later detector pass no longer surfaces that finding."""
    await store.create_annotation(
        case_id="c9",
        source_id="s9",
        event_id="pinned-evt",
        annotation_id="pinned_ann",
        annotation_type="anomaly",
        content="manually confirmed finding",
        origin="system",
        pinned=True,
    )
    await store.bulk_create_annotations(
        [
            {
                "annotation_id": "bulk_ann",
                "case_id": "c9",
                "source_id": "s9",
                "event_id": "bulk-evt",
                "annotation_type": "anomaly",
                "content": "bulk-tagged finding",
                "origin": "system",
                "pinned": False,
            }
        ]
    )

    deleted_count = await store.delete_system_annotations("c9", ["s9"], "anomaly")
    assert deleted_count == 1  # only the non-pinned bulk row

    remaining = await store.list_source_annotations("c9", ["s9"])
    assert len(remaining) == 1
    assert remaining[0].id == "pinned_ann"
    assert remaining[0].pinned is True


@pytest.mark.asyncio
async def test_list_pinned_event_ids(store: PostgresStore):
    """list_pinned_event_ids returns only events with a pinned system
    anomaly annotation — used by the bulk tag endpoint to avoid writing a
    second, duplicate row for an event that already has a pinned one."""
    await store.create_annotation(
        case_id="c10",
        source_id="s10",
        event_id="pinned-evt",
        annotation_id="pinned_ann2",
        annotation_type="anomaly",
        content="confirmed",
        origin="system",
        pinned=True,
    )
    await store.create_annotation(
        case_id="c10",
        source_id="s10",
        event_id="unpinned-evt",
        annotation_id="unpinned_ann",
        annotation_type="anomaly",
        content="not confirmed",
        origin="system",
        pinned=False,
    )

    pinned_ids = await store.list_pinned_event_ids("c10", ["s10"], "anomaly")
    assert pinned_ids == ["pinned-evt"]
