"""Importer tests: remap integrity, secrets exclusion, tamper abort, cleanup.

Round-trips through the real exporter against the SQLite store fixture and
the shared fakes from tests/transfer_fakes.py.
"""

from __future__ import annotations

import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from tests.transfer_fakes import FakeClickHouse, _add, _event_rows
from vestigo.db import postgres as pg
from vestigo.transfer.archive import ArchiveFormatError
from vestigo.transfer.exporter import export_case
from vestigo.transfer.importer import import_case


@pytest.fixture(autouse=True)
async def _schema(store):
    """The conftest store is a bare SQLite file; bring it to Alembic head."""
    await store.init_schema()


async def _rich_case(store, owner_id):
    case = await _add(store, pg.Case, name="Roundtrip", owner_id=owner_id)
    src = await _add(store, pg.Source, case_id=case.id, name="src", file_hash="ab" * 32)
    tl = await _add(
        store,
        pg.Timeline,
        case_id=case.id,
        name="tl",
        is_default=True,
        embedding_model="some-model",
        embedding_config={"dim": 8},
        embedding_config_hash="h",
        embedded_source_ids=[src.id],
        embedded_at=datetime.now(UTC),
    )
    await _add(store, pg.TimelineSource, timeline_id=tl.id, source_id=src.id)
    await _add(store, pg.View, case_id=case.id, name="v", query="", view_filter={})
    await _add(store, pg.SavedChart, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.DetectorRun, case_id=case.id, timeline_id=tl.id)
    await _add(
        store,
        pg.Annotation,
        case_id=case.id,
        source_id=src.id,
        event_id="evt-1",
        annotation_type="comment",
        content="note",
    )
    await _add(store, pg.SigmaRule, case_id=case.id)
    await _add(store, pg.SigmaRun, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.SourceEnrichment, case_id=case.id, source_id=src.id)
    conv = await _add(
        store,
        pg.AgentConversation,
        case_id=case.id,
        timeline_id=tl.id,
        user_id="ghost-user-id",  # not a user on this instance → fallback
    )
    await _add(store, pg.AgentMessage, conversation_id=conv.id)
    await _add(store, pg.AgentProposal, case_id=case.id, conversation_id=conv.id)
    await _add(store, pg.AuditLog, case_id=case.id, user_id=owner_id, username_snapshot="alice")
    return case, src, tl


async def _export(store, case, src, tmp_path, rows=2):
    fake = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id, n=rows)})
    result = await export_case(
        store,
        lambda: fake,
        case.id,
        include_blobs=False,
        exported_by="alice",
        dest_dir=tmp_path,
    )
    return result.path


async def _count(store, model) -> int:
    async with store.session_factory() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class TestRoundTrip:
    async def test_remap_referential_integrity(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        bob = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
        case, src, tl = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=bob)

        assert result.case_id != case.id
        new_case = await store.get_case(result.case_id)
        assert new_case.name == "Roundtrip"
        assert new_case.owner_id == bob.id
        assert new_case.team_id is None

        async with store.session_factory() as s:
            new_tl = (
                await s.execute(select(pg.Timeline).where(pg.Timeline.case_id == result.case_id))
            ).scalar_one()
            # Embedding state reset on import.
            assert new_tl.embedding_model is None
            assert new_tl.embedding_config is None
            assert new_tl.embedded_source_ids is None

            new_chart = (
                await s.execute(
                    select(pg.SavedChart).where(pg.SavedChart.case_id == result.case_id)
                )
            ).scalar_one()
            assert new_chart.timeline_id == new_tl.id

            new_ann = (
                await s.execute(
                    select(pg.Annotation).where(pg.Annotation.case_id == result.case_id)
                )
            ).scalar_one()
            new_src = (
                await s.execute(select(pg.Source).where(pg.Source.case_id == result.case_id))
            ).scalar_one()
            assert new_ann.source_id == new_src.id
            assert new_src.id != src.id
            assert new_ann.event_id == "evt-1"  # event IDs preserved verbatim

            new_conv = (
                await s.execute(
                    select(pg.AgentConversation).where(
                        pg.AgentConversation.case_id == result.case_id
                    )
                )
            ).scalar_one()
            assert new_conv.user_id == bob.id  # unknown "ghost-user-id" → importer fallback
            new_msg = (
                await s.execute(
                    select(pg.AgentMessage).where(pg.AgentMessage.conversation_id == new_conv.id)
                )
            ).scalar_one()
            assert new_msg is not None

            new_audit = (
                await s.execute(select(pg.AuditLog).where(pg.AuditLog.case_id == result.case_id))
            ).scalar_one()
            assert new_audit.user_id is None
            assert new_audit.username_snapshot == "alice"

        assert any("ghost" in w or "user" in w.lower() for w in result.warnings)

    async def test_events_rewritten_to_new_ids(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        target = FakeClickHouse()

        result = await import_case(store, lambda: target, archive, owner=alice)

        keys = list(target.inserted.keys())
        assert len(keys) == 1
        new_case_id, new_source_id = keys[0]
        assert new_case_id == result.case_id and new_source_id != src.id
        rows = target.inserted[keys[0]]
        assert len(rows) == 2
        assert {r["message"] for r in rows} == {"line 0", "line 1"}
        assert result.counts["events"] == 2


class TestRejection:
    async def test_tampered_archive_aborts_before_writes(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        cases_before = await _count(store, pg.Case)

        with zipfile.ZipFile(archive) as zin:
            items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
        items["postgres/sources.ndjson"] = b'{"corrupted": true}\n'
        with zipfile.ZipFile(archive, "w") as zout:
            for name, data in items.items():
                zout.writestr(name, data)

        with pytest.raises(ArchiveFormatError, match="hash mismatch"):
            await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)
        assert await _count(store, pg.Case) == cases_before

    async def test_no_secrets_in_archive(self, store, tmp_path):
        alice = await _add(
            store,
            pg.User,
            username="alice",
            is_admin=False,
            is_active=True,
            password_hash="$2b$12$somebcrypthashvalue",
        )
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)

        with zipfile.ZipFile(archive) as z:
            names = z.namelist()
            assert not any("agent_tokens" in n or "sessions" in n for n in names)
            blob = b"".join(z.read(n) for n in names if n.endswith((".ndjson", ".json")))
        assert b"password_hash" not in blob
        assert b"$2b$" not in blob


class TestFailureCleanup:
    async def test_mid_import_failure_deletes_partial_case(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        cases_before = await _count(store, pg.Case)

        class ExplodingClickHouse(FakeClickHouse):
            def insert_events_arrow(self, batch):
                raise RuntimeError("clickhouse went away")

        fake = ExplodingClickHouse()
        with pytest.raises(RuntimeError, match="went away"):
            await import_case(store, lambda: fake, archive, owner=alice)
        assert await _count(store, pg.Case) == cases_before
        assert fake.deleted, "event partition cleanup must run for inserted sources"
