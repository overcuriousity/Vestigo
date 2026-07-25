"""Case import: verify, remap IDs, restore Postgres rows + events + blobs.

Always restores as a NEW case owned by the importer (no merge, no conflict
path). Every Postgres row gets a fresh id; an old→new map rewrites all
references. Event ids are preserved verbatim — event queries are case-scoped
and preserved ids keep annotation→event cross-references intact. Any failure
after case creation deletes the partial case (Postgres cascade + ClickHouse
partitions) before the error propagates.

Memory: events stream batch-by-batch, but each Postgres stem is read and
revived whole before it is flushed, so peak memory scales with the largest
single entity (usually annotations or audit_log). That is fine for the
single-process deployment this targets; if a case ever outgrows it, chunk the
per-stem loop rather than reworking the id remap, which needs the full map
resolved before dependent stems are revived.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import JSON, delete, select

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
from vestigo.transfer.archive import ArchiveFormatError, ArchiveReader

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
    """Old archive id → new local id, keyed by (kind, old id)."""

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], str] = {}
        self._pattern: re.Pattern[str] | None = None
        self._by_old: dict[str, str] = {}

    def remap(self, kind: str, old: Any) -> Any:
        if old is None:
            return None
        key = (kind, str(old))
        if key not in self._map:
            self._set(key, generate_id(kind))
        return self._map[key]

    def pin(self, kind: str, old: Any, new: str) -> None:
        """Force a mapping (the case id is created before the rows are read)."""
        self._set((kind, str(old)), new)

    def _set(self, key: tuple[str, str], new: str) -> None:
        self._map[key] = new
        self._by_old[key[1]] = new
        self._pattern = None  # rebuilt lazily on the next payload rewrite

    def substitute(self, text: str) -> str:
        """Rewrite every known old id appearing anywhere in ``text``.

        One pass over the text via a single alternation, so a freshly written
        id can never be rewritten again by a later mapping — ``generate_id``
        only appends 8 hex characters, which is not enough entropy to dismiss
        that cascade. Ids also appear inside longer strings (filter
        expressions, not just whole JSON values), so this stays substring-level
        rather than matching whole values.
        """
        if not self._by_old:
            return text
        if self._pattern is None:
            self._pattern = re.compile(
                "|".join(re.escape(old) for old in sorted(self._by_old, key=len, reverse=True))
            )
        return self._pattern.sub(lambda m: self._by_old[m.group(0)], text)


def _remap_json_ids(value: Any, idmap: _IdMap) -> Any:
    """Rewrite archived ids embedded in a JSON payload (proposal events, view
    filters, chart configs) through the old→new map. Only mappings known so far
    apply — sources and timelines revive before the views/charts/proposals that
    embed their ids."""
    return json.loads(idmap.substitute(json.dumps(value)))


def _revive(model: type, row: dict[str, Any], idmap: _IdMap, refs: dict[str, str]) -> Any:
    """Build an ORM object from an archived row with remapped refs."""
    values: dict[str, Any] = {}
    for col in model.__table__.columns:
        value = row.get(col.name)
        if col.name in refs:
            value = idmap.remap(refs[col.name], value)
        elif isinstance(col.type, JSON) and value is not None:
            value = _remap_json_ids(value, idmap)
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
    """Sync: rewrite case_id/source_id in every batch and insert. Row count.

    The embedding markers are blanked in the same pass: Qdrant vectors are not
    portable, so a restored event claiming an embedding model would describe
    vectors this instance does not have.
    """
    total = 0
    with reader.open_member(arcname) as f:
        ipc = pa.ipc.open_stream(f)
        for batch in ipc:
            for column, value in (
                ("case_id", new_case_id),
                ("source_id", new_source_id),
                ("embedding_model", ""),
                ("embedding_config_hash", ""),
            ):
                batch = batch.set_column(
                    batch.schema.get_field_index(column),
                    column,
                    pa.array([value] * batch.num_rows, type=pa.string()),
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
    # Reads are restricted to manifest-listed members (ArchiveReader enforces
    # it), so anything else in the zip is untrusted and never reaches the
    # importer — and a member the exporter always writes cannot go missing
    # without failing the import.
    verified = reader.verified_names
    case_data = reader.read_json("postgres/case.json")
    user_refs = reader.read_json("postgres/user_refs.json")

    counts: dict[str, int] = {"events": 0, "blobs": 0}
    warnings: list[str] = []
    idmap = _IdMap()
    new_case_id = generate_id("case")
    # Pin the case remap to the id we actually create with.
    idmap.pin("case", case_data["id"], new_case_id)

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
        async with store.session_factory() as session:
            # Archived user ids → local ones, resolved in one query up front:
            # per-row lookups would be tens of thousands of round-trips inside
            # the import transaction, and would repeat the same warning once
            # per row instead of once per missing user.
            user_map = await _resolve_users(session, user_refs, owner, warnings)
            unknown_users: set[str] = set()

            def _local_user(old_user_id: Any) -> str:
                """Archived user id → local id, warning once per unknown id.

                Reaches here for ids the archive carried no username for (the
                account was already deleted upstream), which ``_resolve_users``
                cannot see.
                """
                key = str(old_user_id)
                if key in user_map:
                    return user_map[key]
                if key not in unknown_users:
                    unknown_users.add(key)
                    warnings.append(
                        f"user {key} not found on this instance — attributed to importer"
                    )
                return owner.id

            # create_case() seeds a placeholder default timeline; the archive
            # carries the case's real timelines (including its default), so
            # the placeholder must go or the restore would double it.
            await session.execute(delete(Timeline).where(Timeline.case_id == new_case_id))
            embedded_timelines = 0
            for stem, model, refs in _IMPORT_SPECS:
                rows = reader.read_ndjson(f"postgres/{stem}.ndjson")
                counts[stem] = len(rows)
                for row in rows:
                    obj = _revive(model, row, idmap, refs)
                    if isinstance(obj, Timeline):
                        if row.get("embedding_config_hash"):
                            embedded_timelines += 1
                        for colname in _TIMELINE_EMBEDDING_COLUMNS:
                            setattr(obj, colname, None)
                    elif isinstance(obj, AgentConversation):
                        obj.user_id = _local_user(row.get("user_id"))
                    elif isinstance(obj, AuditLog):
                        obj.user_id = None  # username_snapshot carries attribution
                    # created_by holds user ids: remap via user_refs (username
                    # → local id, importer fallback + warning). Values absent
                    # from user_refs are not user ids (e.g. system origins) —
                    # keep them verbatim, no warning.
                    created_by = getattr(obj, "created_by", None)
                    if created_by is not None and str(created_by) in (user_refs.get("users") or {}):
                        obj.created_by = _local_user(created_by)
                    session.add(obj)
                await session.flush()
            await session.commit()
        if embedded_timelines:
            warnings.append(
                f"{embedded_timelines} timeline(s) were embedded on the source instance — "
                "vectors are not portable; re-run embedding to restore semantic search"
            )

        source_rows = reader.read_ndjson("postgres/sources.ndjson")
        event_members = [n for n in verified if n.startswith("events/") and n.endswith(".arrow")]
        if event_members:
            _progress("events")
            clickhouse = clickhouse_factory()
        for row in source_rows:
            new_source_id = idmap.remap("source", row["id"])
            arcname = f"events/{row['id']}.arrow"
            if arcname in verified:
                # Track BEFORE inserting: a mid-source failure may leave a
                # partially written partition, and cleanup must drop it.
                inserted_sources.append(new_source_id)
                n = await asyncio.to_thread(
                    _insert_source_events, clickhouse, reader, arcname, new_case_id, new_source_id
                )
                counts["events"] += n

        blob_members = [n for n in verified if n.startswith("blobs/")]
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
                digest = reader.extract_to(arcname, tmp_path)
                if digest != sha:
                    # The blob's content must hash to its content-addressed
                    # name, or a crafted member would poison the
                    # instance-global retention dir (retain_file
                    # short-circuits later uploads of the real hash).
                    raise ArchiveFormatError(
                        f"blob content does not hash to its member name: {arcname}"
                    )
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


async def _resolve_users(
    session: Any,
    user_refs: dict[str, Any],
    owner: User,
    warnings: list[str],
) -> dict[str, str]:
    """Archived user id → local user id, via username. One query, one warning
    per username that this instance does not know (falls back to the importer).
    """
    archived: dict[str, str] = user_refs.get("users") or {}
    if not archived:
        return {}
    local_ids = dict(
        (
            await session.execute(
                select(User.username, User.id).where(User.username.in_(set(archived.values())))
            )
        ).all()
    )
    for username in sorted(set(archived.values()) - set(local_ids)):
        warnings.append(f"user {username} not found on this instance — attributed to importer")
    return {old_id: local_ids.get(username, owner.id) for old_id, username in archived.items()}
