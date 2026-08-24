"""Importer tests: remap integrity, secrets exclusion, tamper abort, cleanup.

Round-trips through the real exporter against the SQLite store fixture and
the shared fakes from tests/transfer_fakes.py.
"""

from __future__ import annotations

import hashlib
import json
import zipfile
from datetime import UTC, datetime

import pyarrow as pa
import pytest
from sqlalchemy import delete, func, select

from tests.transfer_fakes import FakeClickHouse, ProgressRecorder, _add, _event_rows
from vestigo.core.config import get_settings
from vestigo.core.retention import retention_path
from vestigo.db import postgres as pg
from vestigo.db._arrow_schema import EVENT_ARROW_SCHEMA
from vestigo.transfer import archive as archive_mod
from vestigo.transfer import importer
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
        # The source timeline was embedded; its vectors are not portable.
        assert any("re-run embedding" in w for w in result.warnings)

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

    async def test_event_embedding_markers_cleared(self, store, tmp_path):
        """Vectors don't travel, so restored events must not claim a model."""
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        rows = _event_rows(case.id, src.id, n=1)
        rows[0]["embedding_model"] = "bge-small"
        rows[0]["embedding_config_hash"] = "cd" * 32
        fake = FakeClickHouse({(case.id, src.id): rows})
        archive = (
            await export_case(
                store,
                lambda: fake,
                case.id,
                include_blobs=False,
                exported_by="alice",
                dest_dir=tmp_path,
            )
        ).path

        target = FakeClickHouse()
        await import_case(store, lambda: target, archive, owner=alice)

        restored = next(iter(target.inserted.values()))
        assert restored[0]["embedding_model"] == ""
        assert restored[0]["embedding_config_hash"] == ""

    async def test_ids_embedded_in_strings_are_remapped(self, store, tmp_path):
        """Ids appear inside longer strings (filter expressions), not only as
        whole JSON values — the rewrite is substring-level on purpose."""
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, tl = await _rich_case(store, alice.id)
        async with store.session_factory() as s:
            view = (await s.execute(select(pg.View).where(pg.View.case_id == case.id))).scalar_one()
            view.view_filter = {"query": f"source_id:{src.id} AND timeline:{tl.id}"}
            await s.commit()
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)

        async with store.session_factory() as s:
            new_view = (
                await s.execute(select(pg.View).where(pg.View.case_id == result.case_id))
            ).scalar_one()
            new_src = (
                await s.execute(select(pg.Source).where(pg.Source.case_id == result.case_id))
            ).scalar_one()
            new_tl = (
                await s.execute(select(pg.Timeline).where(pg.Timeline.case_id == result.case_id))
            ).scalar_one()
        assert new_view.view_filter["query"] == f"source_id:{new_src.id} AND timeline:{new_tl.id}"


class TestRejection:
    async def test_tampered_archive_aborts_before_writes(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        cases_before = await _count(store, pg.Case)

        with zipfile.ZipFile(archive) as zin:
            items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
        # Same length as the original, so this exercises the hash rather than
        # the manifest's size cross-check.
        original = items["postgres/sources.ndjson"]
        items["postgres/sources.ndjson"] = b"X" * (len(original) - 1) + b"\n"
        with zipfile.ZipFile(archive, "w") as zout:
            for name, data in items.items():
                zout.writestr(name, data)

        with pytest.raises(ArchiveFormatError, match="hash mismatch"):
            await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)
        assert await _count(store, pg.Case) == cases_before

    async def test_shrunken_member_rejected_before_writes(self, store, tmp_path):
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

        with pytest.raises(ArchiveFormatError, match="does not match the manifest"):
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

    async def _two_blob_archive(self, store, alice, tmp_path, blobs):
        """Archive of a case with one source (and one blob) per entry in `blobs`."""
        case = await _add(store, pg.Case, name="Blobbed", owner_id=alice.id)
        rows = {}
        for i, (file_hash, content) in enumerate(blobs):
            src = await _add(store, pg.Source, case_id=case.id, name=f"s{i}", file_hash=file_hash)
            rows[(case.id, src.id)] = _event_rows(case.id, src.id, n=1)
            blob = retention_path(file_hash)
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(content)
        fake = FakeClickHouse(rows)
        result = await export_case(
            store,
            lambda: fake,
            case.id,
            include_blobs=True,
            exported_by="alice",
            dest_dir=tmp_path / "out",
        )
        return result.path

    @staticmethod
    def _blob(seed: bytes) -> tuple[str, bytes]:
        content = hashlib.sha256(seed).hexdigest().encode()
        return hashlib.sha256(content).hexdigest(), content

    @staticmethod
    def _fail_on_second_retain(monkeypatch):
        """Retain one blob, then blow up — the only way to reach the cleanup
        path with a blob already in the instance-global retention dir."""
        real = importer.retain_file
        calls = []

        def _retain(tmp, dest):
            calls.append(dest)
            if len(calls) > 1:
                raise RuntimeError("disk went away")
            real(tmp, dest)

        monkeypatch.setattr(importer, "retain_file", _retain)

    async def test_a_blob_this_run_retained_is_removed_on_failure(
        self, store, tmp_path, monkeypatch
    ):
        """The retention dir is instance-global and nothing else tracks these:
        a repeatedly-failing import would otherwise accumulate case file content
        on disk that no Source row references."""
        monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
        get_settings.cache_clear()
        try:
            alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
            blobs = [self._blob(b"one"), self._blob(b"two")]
            archive = await self._two_blob_archive(store, alice, tmp_path, blobs)
            # Neither blob is on this instance any more, so the import retains
            # both — the first succeeds, the second fails.
            for file_hash, _ in blobs:
                retention_path(file_hash).unlink()
            self._fail_on_second_retain(monkeypatch)

            with pytest.raises(RuntimeError, match="disk went away"):
                await import_case(store, FakeClickHouse, archive, owner=alice)
            assert not any(retention_path(h).exists() for h, _ in blobs)
        finally:
            get_settings.cache_clear()

    async def test_a_pre_existing_blob_survives_a_failure(self, store, tmp_path, monkeypatch):
        """The important half: blobs are content-addressed and shared, so a
        failed import must never delete one another case still references."""
        monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
        get_settings.cache_clear()
        try:
            alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
            shared, doomed = self._blob(b"shared"), self._blob(b"doomed")
            archive = await self._two_blob_archive(store, alice, tmp_path, [shared, doomed])
            # `shared` stays put — it is the blob the exporting case still uses,
            # so retain_file short-circuits on it and the import never owns it.
            retention_path(doomed[0]).unlink()
            self._fail_on_second_retain(monkeypatch)

            with pytest.raises(RuntimeError, match="disk went away"):
                await import_case(store, FakeClickHouse, archive, owner=alice)
            assert retention_path(shared[0]).read_bytes() == shared[1]
        finally:
            get_settings.cache_clear()


class TestEventSchemaTrust:
    """The Arrow member is attacker-supplied and insert_events_arrow hands
    whatever it gets straight to ClickHouse."""

    def _reschema(self, archive, arcname, schema, rows):
        sink = pa.BufferOutputStream()
        writer = pa.ipc.new_stream(sink, schema)
        writer.write_batch(pa.RecordBatch.from_pylist(rows, schema=schema))
        writer.close()
        _rewrite_member(archive, {arcname: sink.getvalue().to_pybytes()})

    async def test_mismatched_event_schema_aborts_the_import(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        cases_before = await _count(store, pg.Case)

        # A renamed column: get_field_index would return -1, and the missing
        # real column would silently take a ClickHouse server-side default.
        fields = [
            pa.field("case_id_typo" if f.name == "case_id" else f.name, f.type)
            for f in EVENT_ARROW_SCHEMA
        ]
        bogus = pa.schema(fields)
        self._reschema(archive, f"events/{src.id}.arrow", bogus, [])

        fake = FakeClickHouse()
        with pytest.raises(ArchiveFormatError, match="event schema"):
            await import_case(store, lambda: fake, archive, owner=alice)
        assert await _count(store, pg.Case) == cases_before
        assert not fake.inserted


class TestMalformedSources:
    """sources.ndjson drives the events and blobs phases; a row missing the two
    fields they key off must fail as a format error, not a KeyError from deep
    inside the import."""

    @pytest.mark.parametrize("column", ["id", "file_hash"])
    async def test_source_row_without_a_required_field(self, store, tmp_path, column):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        cases_before = await _count(store, pg.Case)

        with zipfile.ZipFile(archive) as z:
            rows = [json.loads(line) for line in z.read("postgres/sources.ndjson").splitlines()]
        for row in rows:
            row.pop(column, None)
        _rewrite_member(
            archive,
            {"postgres/sources.ndjson": b"".join(json.dumps(r).encode() + b"\n" for r in rows)},
        )

        with pytest.raises(ArchiveFormatError, match=f"no usable {column}"):
            await import_case(store, FakeClickHouse, archive, owner=alice)
        assert await _count(store, pg.Case) == cases_before


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
                (await s.execute(select(pg.Source).where(pg.Source.case_id == result.case_id)))
                .scalars()
                .all()
            )
            assert [src.name for src in new_sources] == ["ready-src"]
            new_tl = (
                await s.execute(select(pg.Timeline).where(pg.Timeline.case_id == result.case_id))
            ).scalar_one()
            joins = (
                (
                    await s.execute(
                        select(pg.TimelineSource).where(pg.TimelineSource.timeline_id == new_tl.id)
                    )
                )
                .scalars()
                .all()
            )
            # Exactly the ready source's join row — no phantom id minted by
            # the idmap for the skipped source.
            assert [j.source_id for j in joins] == [new_sources[0].id]
            anns = (
                (
                    await s.execute(
                        select(pg.Annotation).where(pg.Annotation.case_id == result.case_id)
                    )
                )
                .scalars()
                .all()
            )
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

    async def test_missing_user_warns_once_not_per_row(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        bob = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, bob.id, annotation_author=alice.id)
        for i in range(5):
            await _add(
                store,
                pg.Annotation,
                case_id=case.id,
                source_id=src.id,
                event_id=f"evt-{i}",
                annotation_type="comment",
                content="note",
                created_by=alice.id,
            )
        archive = await _export(store, case, src, tmp_path)
        async with store.session_factory() as s:
            await s.execute(delete(pg.User).where(pg.User.id == alice.id))
            await s.commit()

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=bob)

        assert len([w for w in result.warnings if "alice" in w]) == 1

    async def test_non_user_created_by_kept_verbatim(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id, annotation_author="system")
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)

        new_ann = await self._restored_annotation(store, result.case_id)
        assert new_ann.created_by == "system"
        assert not any("system" in w for w in result.warnings)


class TestIdMapScaling:
    """The substitution alternation is expensive to build and cheap to apply,
    so it must be built exactly once. Growing the map during the revive loop
    recompiled it per row and made import quadratic (14s for 1600 audit rows).
    """

    async def test_alternation_compiles_once_for_a_whole_import(self, store, tmp_path, monkeypatch):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        for i in range(200):
            await _add(
                store,
                pg.AuditLog,
                case_id=case.id,
                action="source.upload",
                username_snapshot="alice",
                detail={"note": f"row {i}", "target": src.id},
            )
        archive = await _export(store, case, src, tmp_path)

        seen = []
        original = importer._IdMap.freeze

        def _spy(self):
            seen.append(self)
            original(self)

        monkeypatch.setattr(importer._IdMap, "freeze", _spy)
        await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)

        assert len(seen) == 1
        assert seen[0].compiles == 1

    async def test_frozen_map_rejects_an_unmapped_ref(self):
        idmap = importer._IdMap()
        idmap.bulk_add([("source", "source_old")], {"source_old"})
        idmap.freeze()
        assert idmap.remap("source", "source_old")  # known key still resolves
        with pytest.raises(RuntimeError, match="frozen"):
            idmap.remap("source", "source_never_seen")

    async def test_generated_ids_avoid_archive_ids(self, monkeypatch):
        """A generated id colliding with an archived one would be rewritten
        again by substitute; colliding with another generated id would be a
        primary-key violation."""
        minted = iter(["source_collide", "source_dupe", "source_dupe", "source_ok"])
        monkeypatch.setattr(importer, "generate_id", lambda kind: next(minted))
        idmap = importer._IdMap()
        idmap.bulk_add([("source", "a"), ("source", "b")], {"source_collide"})
        assert idmap.remap("source", "a") == "source_dupe"
        assert idmap.remap("source", "b") == "source_ok"


class TestAuditRestore:
    async def test_target_id_resolves_to_the_new_entity(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        await _add(
            store,
            pg.AuditLog,
            case_id=case.id,
            action="source.upload",
            target_type="source",
            target_id=src.id,
        )
        # A target the archive never carries must survive untouched.
        await _add(
            store,
            pg.AuditLog,
            case_id=case.id,
            action="team.member.add",
            target_type="team",
            target_id="team_elsewhere",
        )
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)

        async with store.session_factory() as s:
            new_src = (
                await s.execute(select(pg.Source).where(pg.Source.case_id == result.case_id))
            ).scalar_one()
            rows = (
                (await s.execute(select(pg.AuditLog).where(pg.AuditLog.case_id == result.case_id)))
                .scalars()
                .all()
            )
        by_action = {r.action: r for r in rows}
        assert by_action["source.upload"].target_id == new_src.id
        assert by_action["source.upload"].target_id != src.id
        assert by_action["team.member.add"].target_id == "team_elsewhere"

    async def test_every_restored_row_is_stamped_as_imported(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        bob = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        # detail=None in the archive must get a fresh dict, not crash.
        await _add(store, pg.AuditLog, case_id=case.id, action="case.read", detail=None)
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(
            store, lambda: FakeClickHouse(), archive, owner=bob, job_id="job-1"
        )

        async with store.session_factory() as s:
            rows = (
                (await s.execute(select(pg.AuditLog).where(pg.AuditLog.case_id == result.case_id)))
                .scalars()
                .all()
            )
        assert rows
        for row in rows:
            marker = row.detail["imported"]
            assert marker["job_id"] == "job-1"
            assert marker["by"] == "bob"
            assert marker["archive_case_id"] == case.id
            assert marker["at"]
            # The archive's own attribution is preserved beside the stamp.
            assert row.user_id is None


class TestForwardReferences:
    async def test_payload_reference_to_a_later_stem_is_rewritten(self, store, tmp_path):
        """saved_charts revives before annotations, so a chart config embedding
        an annotation id only resolves because the map is built up front."""
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, tl = await _rich_case(store, alice.id)
        ann = await _add(
            store,
            pg.Annotation,
            case_id=case.id,
            source_id=src.id,
            event_id="evt-9",
            annotation_type="comment",
            content="pinned",
        )
        async with store.session_factory() as s:
            chart = (
                await s.execute(select(pg.SavedChart).where(pg.SavedChart.case_id == case.id))
            ).scalar_one()
            chart.config = {"pinned_annotation": ann.id}
            await s.commit()
        archive = await _export(store, case, src, tmp_path)

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)

        async with store.session_factory() as s:
            new_chart = (
                await s.execute(
                    select(pg.SavedChart).where(pg.SavedChart.case_id == result.case_id)
                )
            ).scalar_one()
            new_ann = (
                await s.execute(
                    select(pg.Annotation).where(
                        pg.Annotation.case_id == result.case_id, pg.Annotation.event_id == "evt-9"
                    )
                )
            ).scalar_one()
        assert new_chart.config["pinned_annotation"] == new_ann.id
        assert new_chart.config["pinned_annotation"] != ann.id


class TestMalformedArchives:
    """case.json and user_refs.json go straight into Postgres, so they are
    type-checked before anything is created."""

    @pytest.mark.parametrize(
        ("member", "payload", "message"),
        [
            ("postgres/case.json", {"id": "case_a", "name": 42}, "case.name"),
            ("postgres/case.json", {"id": "case_a", "name": "  "}, "case.name"),
            ("postgres/case.json", {"name": "ok"}, "case.id"),
            (
                "postgres/case.json",
                {"id": "case_a", "name": "ok", "description": ["nope"]},
                "case.description",
            ),
            ("postgres/user_refs.json", ["not", "a", "dict"], "not a JSON object"),
            ("postgres/user_refs.json", {"users": {"u1": 5}}, "user_refs.users"),
            ("postgres/user_refs.json", {"users": {}, "team": 7}, "user_refs.team"),
        ],
    )
    async def test_bad_metadata_aborts_before_the_case_is_created(
        self, store, tmp_path, member, payload, message
    ):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        _rewrite_member(archive, {member: json.dumps(payload).encode()})
        before = await _count(store, pg.Case)

        with pytest.raises(ArchiveFormatError, match=message):
            await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)

        assert await _count(store, pg.Case) == before


class TestWarningBounds:
    async def test_warnings_are_capped_with_a_summary_line(self, store, tmp_path):
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        case, src, _ = await _rich_case(store, alice.id)
        # Each unknown created_by user yields one warning.
        for i in range(archive_mod.MAX_WARNINGS + 10):
            await _add(
                store,
                pg.Annotation,
                case_id=case.id,
                source_id=src.id,
                event_id=f"evt-{i}",
                annotation_type="comment",
                content="note",
                created_by=f"ghost-{i}",
            )
            await _add(store, pg.User, id=f"ghost-{i}", username=f"ghost-{i}", is_active=True)
        archive = await _export(store, case, src, tmp_path)
        async with store.session_factory() as s:
            await s.execute(delete(pg.User).where(pg.User.username.like("ghost-%")))
            await s.commit()

        result = await import_case(store, lambda: FakeClickHouse(), archive, owner=alice)

        assert len(result.warnings) == archive_mod.MAX_WARNINGS + 1
        assert result.warnings[-1].startswith("…and ")


class TestBlobFiltering:
    async def test_blob_no_source_references_is_not_retained(self, store, tmp_path, monkeypatch):
        """The retention dir is instance-global; an archive must not be able to
        plant files there that none of its sources claim."""
        monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retention"))
        get_settings.cache_clear()
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        # Content-addressed: the retained blob has to hash to the source's
        # file_hash, or the importer rejects it for a different reason.
        content = b"real source bytes"
        real_hash = hashlib.sha256(content).hexdigest()
        case = await _add(store, pg.Case, name="Blobs", owner_id=alice.id)
        src = await _add(store, pg.Source, case_id=case.id, name="src", file_hash=real_hash)
        blob = retention_path(real_hash)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(content)

        fake = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id, n=1)})
        exported = await export_case(
            store,
            lambda: fake,
            case.id,
            include_blobs=True,
            exported_by="alice",
            dest_dir=tmp_path,
        )
        stowaway = b"unreferenced payload"
        sha = hashlib.sha256(stowaway).hexdigest()
        _rewrite_member(exported.path, {f"blobs/{sha}": stowaway})
        # The stowaway has to be manifest-listed to be read at all.
        with zipfile.ZipFile(exported.path) as zin:
            items = {i.filename: zin.read(i.filename) for i in zin.infolist()}
        manifest = json.loads(items["manifest.json"])
        manifest["members"].append({"path": f"blobs/{sha}", "sha256": sha, "bytes": len(stowaway)})
        items["manifest.json"] = json.dumps(manifest, indent=2).encode()
        with zipfile.ZipFile(exported.path, "w") as zout:
            for name, data in items.items():
                zout.writestr(name, data)

        result = await import_case(store, lambda: FakeClickHouse(), exported.path, owner=alice)

        assert not retention_path(sha).exists()
        assert any("no source references" in w for w in result.warnings)
        assert result.counts["blobs"] == 1  # the legitimate one still landed


class TestProgress:
    async def test_phases_reset_counters_and_complete(self, store, tmp_path, monkeypatch):
        """Every phase entry must reset `processed`/`total` in the same write.

        `JobStore.update` merges progress dicts, so a phase that published only
        its name would inherit the previous phase's denominator and the UI
        would render a percentage against the wrong total.
        """
        monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
        get_settings.cache_clear()
        try:
            alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
            case = await _add(store, pg.Case, name="Progress", owner_id=alice.id)
            rows = {}
            for i in range(2):
                file_hash = hashlib.sha256(f"blob{i}".encode()).hexdigest()
                src = await _add(
                    store, pg.Source, case_id=case.id, name=f"s{i}", file_hash=file_hash
                )
                rows[(case.id, src.id)] = _event_rows(case.id, src.id, n=1)
                blob = retention_path(file_hash)
                blob.parent.mkdir(parents=True, exist_ok=True)
                blob.write_bytes(f"blob{i}".encode())
            exported = await export_case(
                store,
                lambda: FakeClickHouse(rows),
                case.id,
                include_blobs=True,
                exported_by="alice",
                dest_dir=tmp_path / "out",
            )
            recorder = ProgressRecorder()

            result = await import_case(
                store,
                lambda: FakeClickHouse(),
                exported.path,
                owner=alice,
                progress=recorder,
            )

            assert result.counts["events"] == 2
            assert recorder.phases == ["verify", "postgres", "events", "blobs", "stats"]
            for write in recorder.writes:
                if "phase" in write:
                    assert write["processed"] == 0, write
                    assert "total" in write, write
            totals = {w["phase"]: w["total"] for w in recorder.writes if "phase" in w}
            assert totals["events"] == 2
            assert totals["blobs"] == 2
            assert totals["stats"] == 2
            # No accumulated snapshot overshoots its denominator — the check
            # that a stale `total` from the previous phase would fail.
            for snap in recorder.snapshots:
                if snap.get("total"):
                    assert snap["processed"] <= snap["total"], snap
            for phase in ("events", "blobs", "stats"):
                assert any(
                    s.get("phase") == phase and s["processed"] == s["total"] > 0
                    for s in recorder.snapshots
                ), f"{phase} never reached its total"
        finally:
            get_settings.cache_clear()


class TestConverterScripts:
    async def test_roundtrip_carries_converter_scripts_and_raw_blob(
        self, store, tmp_path, monkeypatch
    ):
        """Scripts travel with the case, ids remap, sources re-link, the raw file is restored."""
        monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
        get_settings.cache_clear()
        try:
            alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
            bob = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
            case = await _add(store, pg.Case, name="Conv", owner_id=alice.id)
            raw_bytes = b"raw log bytes"
            raw_hash = hashlib.sha256(raw_bytes).hexdigest()
            parent = await store.create_converter_script(
                case_id=case.id,
                name="x2vestigo",
                version=1,
                raw_file_hash=raw_hash,
                raw_filename="x.log",
                model="m",
                provider_endpoint="e",
                prompt_hash="p",
                sample_hash="s",
                sample_excerpt="S",
                hint=None,
                created_by=alice.id,
                status="failed",
            )
            child = await store.create_converter_script(
                case_id=case.id,
                name="x2vestigo",
                version=2,
                raw_file_hash=raw_hash,
                raw_filename="x.log",
                model="m",
                provider_endpoint="e",
                prompt_hash="p2",
                sample_hash="s",
                sample_excerpt="S",
                hint="retry",
                created_by=alice.id,
                parent_id=parent.id,
                status="working",
            )
            await store.update_converter_script(
                child.id, source_code="print(2)\n", attempts=[{"n": 1, "phase": "sample"}]
            )
            src = await _add(
                store,
                pg.Source,
                case_id=case.id,
                name="s",
                file_hash="ab" * 32,
                converter_script_id=child.id,
            )
            blob = retention_path(raw_hash)
            blob.parent.mkdir(parents=True, exist_ok=True)
            blob.write_bytes(raw_bytes)
            fake = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id, n=1)})
            exported = await export_case(
                store,
                lambda: fake,
                case.id,
                include_blobs=True,
                exported_by="alice",
                dest_dir=tmp_path / "out",
            )
            assert exported.counts["converter_scripts"] == 2
            assert exported.counts["blobs"] == 1  # the raw input (source blob is absent on disk)
            blob.unlink()

            result = await import_case(store, lambda: FakeClickHouse(), exported.path, owner=bob)
            assert result.counts["converter_scripts"] == 2
            scripts = await store.list_converter_scripts(result.case_id)
            by_version = {s.version: s for s in scripts}
            assert set(by_version) == {1, 2}
            new_child = await store.get_converter_script(result.case_id, by_version[2].id)
            assert new_child is not None
            assert new_child.id != child.id and new_child.parent_id == by_version[1].id
            assert new_child.source_code == "print(2)\n"
            assert new_child.attempts == [{"n": 1, "phase": "sample"}]
            assert new_child.created_by == alice.id  # resolved by username like every created_by
            new_src = (await store.list_sources(result.case_id))[0]
            assert new_src.converter_script_id == new_child.id
            assert retention_path(raw_hash).read_bytes() == raw_bytes
        finally:
            get_settings.cache_clear()

    async def test_archive_without_converter_stem_imports(self, store, tmp_path):
        """An archive from a version before generated converters has no such member."""
        alice = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
        bob = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
        case, src, _tl = await _rich_case(store, alice.id)
        archive = await _export(store, case, src, tmp_path)
        stripped = tmp_path / "old.vestigo"
        with zipfile.ZipFile(archive) as zin, zipfile.ZipFile(stripped, "w") as zout:
            manifest = json.loads(zin.read("manifest.json"))
            manifest["members"] = [
                m for m in manifest["members"] if m["path"] != "postgres/converter_scripts.ndjson"
            ]
            for item in zin.infolist():
                if item.filename == "postgres/converter_scripts.ndjson":
                    continue
                if item.filename == "manifest.json":
                    continue
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("manifest.json", json.dumps(manifest))
        result = await import_case(store, lambda: FakeClickHouse(), stripped, owner=bob)
        assert result.counts.get("converter_scripts", 0) == 0
        assert await store.list_converter_scripts(result.case_id) == []
