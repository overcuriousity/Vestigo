"""Importer tests: remap integrity, secrets exclusion, tamper abort, cleanup.

Round-trips through the real exporter against the SQLite store fixture and
the shared fakes from tests/transfer_fakes.py.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select

from tests.transfer_fakes import FakeClickHouse, _add, _event_rows
from vestigo.core.config import get_settings
from vestigo.core.retention import retention_path
from vestigo.db import postgres as pg
from vestigo.transfer.archive import ArchiveFormatError
from vestigo.transfer.exporter import export_case
from vestigo.transfer.importer import import_case


@pytest.fixture(autouse=True)
async def _schema(store):
    """The conftest store is a bare SQLite file; bring it to Alembic head."""
    await store.init_schema()


async def _rich_case(store, owner_id, annotation_author=None):
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
        created_by=annotation_author,
    )
    await _add(store, pg.SigmaRule, case_id=case.id)
    await _add(store, pg.SigmaRun, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.SourceEnrichment, case_id=case.id, source_id=src.id, timeline_id=tl.id)
    await _add(store, pg.FindingDisposition, case_id=case.id, timeline_id=tl.id, source_id=src.id)
    conv = await _add(
        store,
        pg.AgentConversation,
        case_id=case.id,
        timeline_id=tl.id,
        user_id="ghost-user-id",  # not a user on this instance → fallback
    )
    await _add(store, pg.AgentMessage, conversation_id=conv.id)
    # events entries mirror agent/tools.py: {"source_id": ..., "event_id": ...}
    await _add(
        store,
        pg.AgentProposal,
        case_id=case.id,
        conversation_id=conv.id,
        timeline_id=tl.id,
        events=[{"event_id": "evt-1", "source_id": src.id}],
    )
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


def _rewrite_member(archive, replacements: dict[str, bytes]):
    """Rewrite a zip with replaced member contents, fixing the manifest entry
    for each replacement so verify_members still passes (the attack is a
    filename/content mismatch, not manifest tampering)."""
    with zipfile.ZipFile(archive) as zin:
        items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
    items.update(replacements)
    manifest = json.loads(items["manifest.json"])
    for member in manifest["members"]:
        if member["path"] in replacements:
            data = replacements[member["path"]]
            member["sha256"] = hashlib.sha256(data).hexdigest()
            member["bytes"] = len(data)
    items["manifest.json"] = json.dumps(manifest, indent=2).encode()
    with zipfile.ZipFile(archive, "w") as zout:
        for name, data in items.items():
            zout.writestr(name, data)


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

            new_enrichment = (
                await s.execute(
                    select(pg.SourceEnrichment).where(pg.SourceEnrichment.case_id == result.case_id)
                )
            ).scalar_one()
            assert new_enrichment.timeline_id == new_tl.id

            new_disposition = (
                await s.execute(
                    select(pg.FindingDisposition).where(
                        pg.FindingDisposition.case_id == result.case_id
                    )
                )
            ).scalar_one()
            assert new_disposition.source_id == new_src.id
            assert new_disposition.timeline_id == new_tl.id

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

            new_proposal = (
                await s.execute(
                    select(pg.AgentProposal).where(pg.AgentProposal.case_id == result.case_id)
                )
            ).scalar_one()
            assert new_proposal.timeline_id == new_tl.id
            # Ids embedded in JSON payloads are remapped too.
            assert new_proposal.events == [{"event_id": "evt-1", "source_id": new_src.id}]

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


class TestArchiveMemberTrust:
    """Archive members outside the manifest's verified set are never read, and
    a blob's content must hash to its content-addressed member name before it
    may reach the instance-global retention dir."""

    async def test_blob_content_must_match_member_name(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
        get_settings.cache_clear()
        try:
            alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
            case = await _add(store, pg.Case, name="Blobbed", owner_id=alice.id)
            file_hash = "ab" * 32
            src = await _add(store, pg.Source, case_id=case.id, name="s", file_hash=file_hash)
            blob = retention_path(file_hash)
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(b"original file bytes")
            fake = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id, n=1)})
            result = await export_case(
                store,
                lambda: fake,
                case.id,
                include_blobs=True,
                exported_by="alice",
                dest_dir=tmp_path / "out",
            )
            archive = result.path
            # The importer must re-retain from the archive, not short-circuit
            # on the already-retained original.
            blob.unlink()

            # Manifest sha256 recomputed for the poisoned bytes — only the
            # content-addressed name no longer matches the content.
            _rewrite_member(archive, {f"blobs/{file_hash}": b"poisoned bytes"})

            with pytest.raises(ArchiveFormatError, match="blob"):
                await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)
            assert not retention_path(file_hash).exists()
        finally:
            get_settings.cache_clear()

    async def test_unlisted_archive_members_are_ignored(self, store, tmp_path, monkeypatch):
        monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
        get_settings.cache_clear()
        try:
            alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
            case, src, _ = await _rich_case(store, alice.id)
            archive = await _export(store, case, src, tmp_path)
            ghost_hash = "ee" * 32
            # Zip members NOT listed in manifest["members"]: unverified by
            # definition, so the importer must not read either one. The ghost
            # blob would otherwise land in the instance-global retention dir.
            with zipfile.ZipFile(archive, "a") as z:
                z.writestr("events/ghost.arrow", b"not arrow data at all")
                z.writestr(f"blobs/{ghost_hash}", b"ghost blob content")

            target = FakeClickHouse()
            result = await import_case(store, lambda: target, archive, owner=alice)

            assert result.counts["events"] == 2  # only the real source's rows
            assert len(target.inserted) == 1
            assert all("ghost" not in source_id for _, source_id in target.inserted)
            assert result.counts["blobs"] == 0
            assert not retention_path(ghost_hash).exists()
        finally:
            get_settings.cache_clear()


class TestSkippedSourceDependents:
    async def test_skipped_source_dependents_roundtrip(self, store, tmp_path):
        """An export taken mid-ingest must import clean: the skipped source's
        dependents (timeline_sources is a real FK on sources.id — Postgres
        would abort at flush on the phantom row) are filtered at export time."""
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case = await _add(store, pg.Case, name="Partial", owner_id=alice.id)
        ready = await _add(store, pg.Source, case_id=case.id, name="ready-src", file_hash="ab" * 32)
        ingesting = await _add(
            store,
            pg.Source,
            case_id=case.id,
            name="mid-src",
            file_hash="cd" * 32,
            status="ingesting",
        )
        tl = await _add(store, pg.Timeline, case_id=case.id, name="tl", is_default=True)
        await _add(store, pg.TimelineSource, timeline_id=tl.id, source_id=ready.id)
        await _add(store, pg.TimelineSource, timeline_id=tl.id, source_id=ingesting.id)
        await _add(
            store,
            pg.Annotation,
            case_id=case.id,
            source_id=ingesting.id,
            event_id="evt-x",
            annotation_type="comment",
            content="x",
        )
        fake = FakeClickHouse({(case.id, ready.id): _event_rows(case.id, ready.id, n=1)})
        exported = await export_case(
            store,
            lambda: fake,
            case.id,
            include_blobs=False,
            exported_by="alice",
            dest_dir=tmp_path,
        )

        result = await import_case(store, lambda: FakeClickHouse(), exported.path, owner=alice)

        async with store.session_factory() as s:
            new_sources = (
                await s.execute(select(pg.Source).where(pg.Source.case_id == result.case_id))
            ).scalars().all()
            assert [src.name for src in new_sources] == ["ready-src"]
            new_tl = (
                await s.execute(select(pg.Timeline).where(pg.Timeline.case_id == result.case_id))
            ).scalar_one()
            joins = (
                await s.execute(
                    select(pg.TimelineSource).where(pg.TimelineSource.timeline_id == new_tl.id)
                )
            ).scalars().all()
            # Exactly the ready source's join row — no phantom id minted by
            # the idmap for the skipped source.
            assert [j.source_id for j in joins] == [new_sources[0].id]
            anns = (
                await s.execute(
                    select(pg.Annotation).where(pg.Annotation.case_id == result.case_id)
                )
            ).scalars().all()
            assert anns == []


class TestCreatedByAttribution:
    """created_by holds user ids: mapped via user_refs by username with
    importer fallback; non-user values (system origins) pass through verbatim."""

    async def _restored_annotation(self, store, case_id):
        async with store.session_factory() as s:
            return (
                await s.execute(select(pg.Annotation).where(pg.Annotation.case_id == case_id))
            ).scalar_one()

    async def test_created_by_maps_through_username(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        bob = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, bob.id, annotation_author=alice.id)
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=bob)

        new_ann = await self._restored_annotation(store, result.case_id)
        assert new_ann.created_by == alice.id
        assert not any("alice" in w for w in result.warnings)

    async def test_created_by_falls_back_to_importer_with_warning(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        bob = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, bob.id, annotation_author=alice.id)
        archive = await _export(store, case, src, tmp_path)
        # alice is gone from this instance before the archive lands.
        async with store.session_factory() as s:
            await s.execute(delete(pg.User).where(pg.User.id == alice.id))
            await s.commit()

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=bob)

        new_ann = await self._restored_annotation(store, result.case_id)
        assert new_ann.created_by == bob.id
        assert any("alice" in w for w in result.warnings)

    async def test_non_user_created_by_kept_verbatim(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id, annotation_author="system")
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)

        new_ann = await self._restored_annotation(store, result.case_id)
        assert new_ann.created_by == "system"
        assert not any("system" in w for w in result.warnings)
