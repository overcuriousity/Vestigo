"""Exporter tests: Postgres snapshot completeness + archive assembly.

Uses the SQLite `store` fixture from conftest and the shared FakeClickHouse
from tests/transfer_fakes.py — no live services.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from tests.transfer_fakes import FakeClickHouse, _add, _event_rows
from vestigo.core.retention import retention_path
from vestigo.db import postgres as pg
from vestigo.db._dt import NULL_TS_SENTINEL
from vestigo.transfer.archive import FORMAT_VERSION, ArchiveReader
from vestigo.transfer.exporter import export_case


@pytest.fixture(autouse=True)
async def _schema(store):
    """The conftest store is a bare SQLite file; bring it to Alembic head."""
    await store.init_schema()


async def _rich_case(store, owner_id):
    """A case with one of every exported entity."""
    case = await _add(store, pg.Case, name="Export Me", owner_id=owner_id)
    src = await _add(store, pg.Source, case_id=case.id, name="src", file_hash="ab" * 32)
    tl = await _add(store, pg.Timeline, case_id=case.id, name="tl", is_default=True)
    await _add(store, pg.TimelineSource, timeline_id=tl.id, source_id=src.id)
    await _add(store, pg.TimelineEnricher, timeline_id=tl.id)
    await _add(store, pg.View, case_id=case.id, name="v", query="", view_filter={})
    await _add(store, pg.SavedChart, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.BaselineDefinition, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.DetectorRun, case_id=case.id, timeline_id=tl.id)
    await _add(store, pg.FindingDisposition, case_id=case.id, timeline_id=tl.id)
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
        store, pg.AgentConversation, case_id=case.id, timeline_id=tl.id, user_id=owner_id
    )
    await _add(store, pg.AgentMessage, conversation_id=conv.id)
    await _add(store, pg.AgentProposal, case_id=case.id, conversation_id=conv.id)
    await _add(store, pg.AuditLog, case_id=case.id)
    return case, src, tl


async def test_export_roundtrip_all_entities(store, tmp_path):
    owner = await _add(store, pg.User, username="alice", is_admin=False, is_active=True)
    case, src, tl = await _rich_case(store, owner.id)
    fake_ch = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id)})

    result = await export_case(
        store,
        lambda: fake_ch,
        case.id,
        include_blobs=False,
        exported_by="alice",
        dest_dir=tmp_path,
    )

    assert result.path.exists() and result.bytes > 0
    reader = ArchiveReader(result.path)
    m = reader.manifest
    assert m["format_version"] == FORMAT_VERSION
    assert m["case"] == {"id": case.id, "name": "Export Me"}
    assert m["exported_by"] == "alice"
    assert m["include_blobs"] is False
    reader.verify_members()
    for stem in (
        "sources",
        "timelines",
        "timeline_sources",
        "timeline_enrichers",
        "views",
        "saved_charts",
        "baseline_definitions",
        "detector_runs",
        "finding_dispositions",
        "annotations",
        "sigma_rules",
        "sigma_runs",
        "source_enrichments",
        "agent_conversations",
        "agent_messages",
        "agent_proposals",
        "audit_log",
    ):
        rows = reader.read_ndjson(f"postgres/{stem}.ndjson")
        assert len(rows) == 1, f"{stem}: expected 1 row, got {len(rows)}"
    assert reader.read_json("postgres/case.json")["name"] == "Export Me"
    assert reader.read_json("postgres/user_refs.json")["users"] == {owner.id: "alice"}
    # Events: one Arrow member with both rows, hashes as plain strings.
    names = reader.member_names()
    assert f"events/{src.id}.arrow" in names
    assert result.counts["events"] == 2
    reader.close()


async def test_export_without_sources_never_touches_clickhouse(store, tmp_path):
    owner = await _add(store, pg.User, username="bob", is_admin=False, is_active=True)
    case = await _add(store, pg.Case, name="Empty", owner_id=owner.id)

    def _forbidden():
        raise AssertionError("ClickHouse factory must not be called")

    result = await export_case(
        store,
        _forbidden,
        case.id,
        include_blobs=True,
        exported_by="bob",
        dest_dir=tmp_path,
    )
    assert result.counts["sources"] == 0
    assert result.counts["events"] == 0


async def test_export_blobs(store, tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
    from vestigo.core.config import get_settings

    get_settings.cache_clear()
    try:
        owner = await _add(store, pg.User, username="carol", is_admin=False, is_active=True)
        file_hash = "cd" * 32
        case = await _add(store, pg.Case, name="Blobs", owner_id=owner.id)
        src = await _add(store, pg.Source, case_id=case.id, name="s", file_hash=file_hash)
        blob = retention_path(file_hash)
        blob.parent.mkdir(parents=True, exist_ok=True)
        blob.write_bytes(b"original file bytes")
        fake_ch = FakeClickHouse({(case.id, src.id): _event_rows(case.id, src.id, n=1)})

        result = await export_case(
            store,
            lambda: fake_ch,
            case.id,
            include_blobs=True,
            exported_by="carol",
            dest_dir=tmp_path / "out",
        )
        reader = ArchiveReader(result.path)
        assert f"blobs/{file_hash}" in reader.member_names()
        reader.verify_members()
        reader.close()
        assert result.counts["blobs"] == 1
    finally:
        get_settings.cache_clear()


async def test_export_missing_blob_warns_not_fails(store, tmp_path, monkeypatch):
    monkeypatch.setenv("VESTIGO_SOURCE_RETENTION_PATH", str(tmp_path / "retained"))
    from vestigo.core.config import get_settings

    get_settings.cache_clear()
    try:
        owner = await _add(store, pg.User, username="dave", is_admin=False, is_active=True)
        case = await _add(store, pg.Case, name="NoBlob", owner_id=owner.id)
        await _add(store, pg.Source, case_id=case.id, name="s", file_hash="ef" * 32)
        result = await export_case(
            store,
            lambda: FakeClickHouse(),
            case.id,
            include_blobs=True,
            exported_by="dave",
            dest_dir=tmp_path / "out",
        )
        assert len(result.warnings) == 1
        assert result.counts["blobs"] == 0
    finally:
        get_settings.cache_clear()


async def test_export_skips_sources_not_ready(store, tmp_path):
    """A mid-ingest source (status != ready) must not ship: neither in the
    sources NDJSON rows nor in the events loop — with a warning naming it.
    Its dependents (timeline_sources is a real FK on sources.id) must be
    filtered too, or the archive aborts at import flush on Postgres."""
    owner = await _add(store, pg.User, username="frank", is_admin=False, is_active=True)
    case = await _add(store, pg.Case, name="Partial", owner_id=owner.id)
    ready = await _add(store, pg.Source, case_id=case.id, name="ready-src", file_hash="ab" * 32)
    ingesting = await _add(
        store, pg.Source, case_id=case.id, name="mid-src", file_hash="cd" * 32, status="ingesting"
    )
    tl = await _add(store, pg.Timeline, case_id=case.id, name="tl", is_default=True)
    # Every uploaded source gets a default-timeline join row at upload start.
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
    await _add(store, pg.FindingDisposition, case_id=case.id, source_id=ingesting.id)
    await _add(store, pg.FindingDisposition, case_id=case.id, source_id=None)  # stays
    await _add(store, pg.SourceEnrichment, case_id=case.id, source_id=ingesting.id)
    # A chart whose config embeds the skipped source id: no FK enforces this,
    # so it ships and must at least be reported.
    chart = await _add(
        store,
        pg.SavedChart,
        case_id=case.id,
        timeline_id=tl.id,
        config={"source_ids": [ingesting.id]},
    )
    fake_ch = FakeClickHouse(
        {
            (case.id, ready.id): _event_rows(case.id, ready.id, n=1),
            (case.id, ingesting.id): _event_rows(case.id, ingesting.id, n=1),
        }
    )

    result = await export_case(
        store,
        lambda: fake_ch,
        case.id,
        include_blobs=False,
        exported_by="frank",
        dest_dir=tmp_path,
    )

    assert result.counts["sources"] == 1
    assert result.counts["events"] == 1
    assert any("mid-src" in w for w in result.warnings)
    assert any(
        f"saved_charts {chart.id} references excluded source {ingesting.id}" in w
        for w in result.warnings
    )
    reader = ArchiveReader(result.path)
    rows = reader.read_ndjson("postgres/sources.ndjson")
    assert [r["name"] for r in rows] == ["ready-src"]
    names = reader.member_names()
    assert f"events/{ready.id}.arrow" in names
    assert not any(ingesting.id in n for n in names)
    # Dependents of the skipped source are filtered; the None-source
    # (value-scoped) disposition stays.
    tl_rows = reader.read_ndjson("postgres/timeline_sources.ndjson")
    assert [r["source_id"] for r in tl_rows] == [ready.id]
    assert reader.read_ndjson("postgres/annotations.ndjson") == []
    disp_rows = reader.read_ndjson("postgres/finding_dispositions.ndjson")
    assert [r["source_id"] for r in disp_rows] == [None]
    assert reader.read_ndjson("postgres/source_enrichments.ndjson") == []
    reader.close()


async def test_export_event_null_timestamp_and_nul_padded_hash(store, tmp_path):
    """Rows without a parseable timestamp re-encode as NULL_TS_SENTINEL;
    FixedString NUL padding is stripped from hash fields (ClickHouse dtypes)."""
    owner = await _add(store, pg.User, username="erin", is_admin=False, is_active=True)
    case = await _add(store, pg.Case, name="Edge", owner_id=owner.id)
    src = await _add(store, pg.Source, case_id=case.id, name="s", file_hash="ab" * 32)
    row = _event_rows(case.id, src.id, n=1)[0]
    row["timestamp"] = None
    row["content_hash"] = b"abc" + b"\x00" * 61
    fake_ch = FakeClickHouse({(case.id, src.id): [row]})

    result = await export_case(
        store,
        lambda: fake_ch,
        case.id,
        include_blobs=False,
        exported_by="erin",
        dest_dir=tmp_path,
    )

    reader = ArchiveReader(result.path)
    with reader.open_member(f"events/{src.id}.arrow") as f:
        table = pa.ipc.open_stream(f).read_all()
    reader.close()
    assert table.num_rows == 1
    out = table.to_pylist()[0]
    assert out["timestamp"] == NULL_TS_SENTINEL
    assert out["content_hash"] == "abc"
