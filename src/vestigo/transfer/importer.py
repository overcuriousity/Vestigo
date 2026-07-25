"""Case import: verify, remap IDs, restore Postgres rows + events + blobs.

Always restores as a NEW case owned by the importer (no merge, no conflict
path). Every Postgres row gets a fresh id; an old→new map rewrites all
references. Event ids are preserved verbatim — event queries are case-scoped
and preserved ids keep annotation→event cross-references intact. Any failure
after case creation deletes the partial case (Postgres cascade + ClickHouse
partitions) before the error propagates.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import delete, select

from vestigo.core.retention import retain_file, retention_path
from vestigo.db.field_stats import refresh_source_field_stats
from vestigo.db.postgres import (
    AgentConversation,
    AgentMessage,
    AgentProposal,
    Annotation,
    AuditLog,
    BaselineDefinition,
    DetectorRun,
    FindingDisposition,
    PostgresStore,
    SavedChart,
    SigmaRule,
    SigmaRun,
    Source,
    SourceEnrichment,
    Timeline,
    TimelineEnricher,
    TimelineSource,
    User,
    View,
    generate_id,
)
from vestigo.transfer.archive import ArchiveReader

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

# Insertion order = dependency order. Refs map column → remap kind.
_IMPORT_SPECS: list[tuple[str, type, dict[str, str]]] = [
    ("sources", Source, {"id": "source", "case_id": "case"}),
    ("timelines", Timeline, {"id": "timeline", "case_id": "case"}),
    ("timeline_sources", TimelineSource, {"timeline_id": "timeline", "source_id": "source"}),
    (
        "timeline_enrichers",
        TimelineEnricher,
        {"id": "timeline_enricher", "timeline_id": "timeline"},
    ),
    ("views", View, {"id": "view", "case_id": "case"}),
    ("saved_charts", SavedChart, {"id": "chart", "case_id": "case", "timeline_id": "timeline"}),
    (
        "baseline_definitions",
        BaselineDefinition,
        {"id": "baseline", "case_id": "case", "timeline_id": "timeline"},
    ),
    (
        "detector_runs",
        DetectorRun,
        {"id": "detector_run", "case_id": "case", "timeline_id": "timeline"},
    ),
    (
        "finding_dispositions",
        FindingDisposition,
        {
            "id": "disposition",
            "case_id": "case",
            "timeline_id": "timeline",
            "source_id": "source",
        },
    ),
    ("annotations", Annotation, {"id": "annotation", "case_id": "case", "source_id": "source"}),
    ("sigma_rules", SigmaRule, {"id": "sigma_rule", "case_id": "case"}),
    ("sigma_runs", SigmaRun, {"id": "sigma_run", "case_id": "case", "timeline_id": "timeline"}),
    (
        "source_enrichments",
        SourceEnrichment,
        {
            "id": "source_enrichment",
            "case_id": "case",
            "source_id": "source",
            "timeline_id": "timeline",
        },
    ),
    (
        "agent_conversations",
        AgentConversation,
        {"id": "conversation", "case_id": "case", "timeline_id": "timeline"},
    ),
    ("agent_messages", AgentMessage, {"id": "message", "conversation_id": "conversation"}),
    (
        "agent_proposals",
        AgentProposal,
        {
            "id": "proposal",
            "case_id": "case",
            "conversation_id": "conversation",
            "timeline_id": "timeline",
        },
    ),
    ("audit_log", AuditLog, {"id": "audit", "case_id": "case"}),
]

_TIMELINE_EMBEDDING_COLUMNS = (
    "embedding_model",
    "embedding_config",
    "embedding_config_hash",
    "embedded_source_ids",
    "embedded_at",
)


@dataclass
class ImportResult:
    case_id: str
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


class _IdMap:
    def __init__(self) -> None:
        self._map: dict[tuple[str, str], str] = {}

    def remap(self, kind: str, old: Any) -> Any:
        if old is None:
            return None
        key = (kind, str(old))
        if key not in self._map:
            self._map[key] = generate_id(kind)
        return self._map[key]


def _revive(model: type, row: dict[str, Any], idmap: _IdMap, refs: dict[str, str]) -> Any:
    """Build an ORM object from an archived row with remapped refs."""
    values: dict[str, Any] = {}
    for col in model.__table__.columns:
        value = row.get(col.name)
        if col.name in refs:
            value = idmap.remap(refs[col.name], value)
        if isinstance(value, str):
            # Some column types raise NotImplementedError on python_type —
            # treat those as pass-through (leave the archived value as-is).
            try:
                python_type = col.type.python_type
            except NotImplementedError:
                python_type = None
            if python_type is datetime:
                value = datetime.fromisoformat(value)
        values[col.name] = value
    return model(**values)


def _insert_source_events(
    clickhouse: Any, reader: ArchiveReader, arcname: str, new_case_id: str, new_source_id: str
) -> int:
    """Sync: rewrite case_id/source_id in every batch and insert. Row count."""
    total = 0
    with reader.open_member(arcname) as f:
        ipc = pa.ipc.open_stream(f)
        for batch in ipc:
            batch = batch.set_column(
                batch.schema.get_field_index("case_id"),
                "case_id",
                pa.array([new_case_id] * batch.num_rows, type=pa.string()),
            )
            batch = batch.set_column(
                batch.schema.get_field_index("source_id"),
                "source_id",
                pa.array([new_source_id] * batch.num_rows, type=pa.string()),
            )
            total += clickhouse.insert_events_arrow(batch)
    return total


async def import_case(
    store: PostgresStore,
    clickhouse_factory: Callable[[], Any],
    archive_path: Path,
    *,
    owner: User,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> ImportResult:
    """Restore an archive as a new case owned by `owner`. All-or-nothing."""

    def _progress(phase: str) -> None:
        if progress:
            progress({"phase": phase})

    _progress("verify")
    reader = ArchiveReader(archive_path)  # raises ArchiveFormatError on bad manifest
    reader.verify_members()  # raises before ANY write
    case_data = reader.read_json("postgres/case.json")
    user_refs = reader.read_json("postgres/user_refs.json")
    member_names = set(reader.member_names())

    counts: dict[str, int] = {"events": 0, "blobs": 0}
    warnings: list[str] = []
    idmap = _IdMap()
    new_case_id = generate_id("case")
    # Pin the case remap to the id we actually create with.
    idmap._map[("case", str(case_data["id"]))] = new_case_id

    created = False
    inserted_sources: list[str] = []
    clickhouse = None
    try:
        await store.create_case(
            new_case_id,
            case_data["name"],
            case_data.get("description"),
            owner_id=owner.id,
            team_id=None,
        )
        created = True

        _progress("postgres")
        # Username → local user id, for conversation attribution mapping.
        async with store.session_factory() as session:
            # create_case() seeds a placeholder default timeline; the archive
            # carries the case's real timelines (including its default), so
            # the placeholder must go or the restore would double it.
            await session.execute(delete(Timeline).where(Timeline.case_id == new_case_id))
            for stem, model, refs in _IMPORT_SPECS:
                rows = reader.read_ndjson(f"postgres/{stem}.ndjson")
                counts[stem] = len(rows)
                for row in rows:
                    obj = _revive(model, row, idmap, refs)
                    if isinstance(obj, Timeline):
                        for colname in _TIMELINE_EMBEDDING_COLUMNS:
                            setattr(obj, colname, None)
                    elif isinstance(obj, AgentConversation):
                        obj.user_id = await _map_user(
                            session, user_refs, row.get("user_id"), owner, warnings
                        )
                    elif isinstance(obj, AuditLog):
                        obj.user_id = None  # username_snapshot carries attribution
                    session.add(obj)
                await session.flush()
            await session.commit()

        source_rows = reader.read_ndjson("postgres/sources.ndjson")
        event_members = [
            n for n in member_names if n.startswith("events/") and n.endswith(".arrow")
        ]
        if event_members:
            _progress("events")
            clickhouse = clickhouse_factory()
        for row in source_rows:
            new_source_id = idmap.remap("source", row["id"])
            arcname = f"events/{row['id']}.arrow"
            if arcname in member_names:
                # Track BEFORE inserting: a mid-source failure may leave a
                # partially written partition, and cleanup must drop it.
                inserted_sources.append(new_source_id)
                n = await asyncio.to_thread(
                    _insert_source_events, clickhouse, reader, arcname, new_case_id, new_source_id
                )
                counts["events"] += n

        blob_members = [n for n in member_names if n.startswith("blobs/")]
        if blob_members:
            _progress("blobs")
        for arcname in blob_members:
            sha = arcname.removeprefix("blobs/")
            if not _SHA256_RE.fullmatch(sha):
                warnings.append(f"skipping suspicious blob member: {arcname}")
                continue
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                reader.extract_to(arcname, tmp_path)
                await asyncio.to_thread(retain_file, tmp_path, retention_path(sha))
                counts["blobs"] += 1
            finally:
                tmp_path.unlink(missing_ok=True)
        blobbed = {n.removeprefix("blobs/") for n in blob_members}
        for row in source_rows:
            if blob_members and row["file_hash"] not in blobbed:
                warnings.append(
                    f"no blob in archive for source {row['name']} — events restored, original file absent"
                )

        if clickhouse is not None and inserted_sources:
            _progress("stats")
            for new_source_id in inserted_sources:
                try:
                    await refresh_source_field_stats(store, clickhouse, new_case_id, new_source_id)
                except Exception as exc:  # noqa: BLE001 — stats never fail an import
                    warnings.append(
                        f"field stats recompute failed for source {new_source_id}: {exc}"
                    )
    except Exception:
        if clickhouse is not None:
            for new_source_id in inserted_sources:
                with contextlib.suppress(Exception):  # best-effort cleanup
                    await asyncio.to_thread(
                        clickhouse.delete_source_events, new_case_id, new_source_id
                    )
        if created:
            await store.delete_case(new_case_id)
        raise
    finally:
        reader.close()

    return ImportResult(case_id=new_case_id, counts=counts, warnings=warnings)


async def _map_user(
    session: Any,
    user_refs: dict[str, Any],
    old_user_id: Any,
    owner: User,
    warnings: list[str],
) -> str:
    """Old user id → username → local user id; importer fallback + warning."""
    username = (user_refs.get("users") or {}).get(str(old_user_id)) if old_user_id else None
    if username:
        local = (
            await session.execute(select(User).where(User.username == username))
        ).scalar_one_or_none()
        if local is not None:
            return local.id
    warnings.append(
        f"user {username or old_user_id} not found on this instance — attributed to importer"
    )
    return owner.id
