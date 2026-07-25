"""Case export: snapshot Postgres state + ClickHouse events into a .vestigo archive.

The Postgres snapshot is generic: every exported model is read with a plain
ORM select and serialized by column introspection, so new columns ride along
without exporter changes. Events stream per source through the existing
iter_source_events primitive into an Arrow IPC member.

Memory: events stream, but the Postgres snapshot materializes every row of
every entity as dicts before the archive is written, so peak memory scales
with the largest single entity (usually annotations or audit_log). Acceptable
for the single-process deployment this targets; chunk the per-stem loop if a
case ever outgrows it.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import select

from vestigo import __version__
from vestigo.core.retention import retention_path
from vestigo.db._arrow_schema import EVENT_ARROW_SCHEMA
from vestigo.db._dt import NULL_TS_SENTINEL
from vestigo.db.postgres import (
    AgentConversation,
    AgentMessage,
    AgentProposal,
    Annotation,
    AuditLog,
    BaselineDefinition,
    Case,
    DetectorRun,
    FindingDisposition,
    PostgresStore,
    SavedChart,
    SigmaRule,
    SigmaRun,
    Source,
    SourceEnrichment,
    Team,
    Timeline,
    TimelineEnricher,
    TimelineSource,
    User,
    View,
)
from vestigo.transfer.archive import FORMAT_VERSION, ArchiveWriter

# (file stem, model, scope): "case" = WHERE case_id == ..., "timeline" =
# WHERE timeline_id IN (case's timelines), "conversation" = WHERE
# conversation_id IN (case's conversations). Insertion order = export order.
_EXPORT_ENTITIES: list[tuple[str, type, str]] = [
    ("sources", Source, "case"),
    ("timelines", Timeline, "case"),
    ("timeline_sources", TimelineSource, "timeline"),
    ("timeline_enrichers", TimelineEnricher, "timeline"),
    ("views", View, "case"),
    ("saved_charts", SavedChart, "case"),
    ("baseline_definitions", BaselineDefinition, "case"),
    ("detector_runs", DetectorRun, "case"),
    ("finding_dispositions", FindingDisposition, "case"),
    ("annotations", Annotation, "case"),
    ("sigma_rules", SigmaRule, "case"),
    ("sigma_runs", SigmaRun, "case"),
    ("source_enrichments", SourceEnrichment, "case"),
    ("agent_conversations", AgentConversation, "case"),
    ("agent_messages", AgentMessage, "conversation"),
    ("agent_proposals", AgentProposal, "case"),
    ("audit_log", AuditLog, "case"),
]


@dataclass
class ExportResult:
    path: Path
    bytes: int
    counts: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


def _row_to_dict(obj: Any) -> dict[str, Any]:
    """Serialize any ORM row by column introspection (datetimes → ISO)."""
    out: dict[str, Any] = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        if isinstance(value, datetime):
            value = value.isoformat()
        out[col.name] = value
    return out


def _ndjson(rows: list[dict[str, Any]]) -> bytes:
    return "".join(json.dumps(r) + "\n" for r in rows).encode()


def _dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    return datetime.fromisoformat(str(value))


def _hash_str(value: Any) -> str:
    """FixedString columns come back as NUL-padded bytes; normalize to str."""
    if value is None:
        return ""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).decode("ascii", errors="replace").rstrip("\x00")
    return str(value)


def _normalize_event_row(row: dict[str, Any]) -> dict[str, Any]:
    """Map an iter_source_events row onto EVENT_ARROW_SCHEMA dtypes."""
    ts = row.get("timestamp")
    return {
        "event_id": str(row["event_id"]),
        "case_id": row["case_id"],
        "source_id": row["source_id"],
        "source_file": row.get("source_file") or "",
        "byte_offset": row.get("byte_offset") or 0,
        "line_number": row.get("line_number") or 0,
        "content_hash": _hash_str(row.get("content_hash")),
        "file_hash": _hash_str(row.get("file_hash")),
        "parser_name": row.get("parser_name") or "",
        "parser_version": row.get("parser_version") or "",
        "ingest_time": _dt(row["ingest_time"]) if row.get("ingest_time") else NULL_TS_SENTINEL,
        "message": row.get("message") or "",
        "timestamp": _dt(ts) if ts else NULL_TS_SENTINEL,
        "timestamp_desc": row.get("timestamp_desc") or "",
        "artifact": row.get("artifact") or "",
        "artifact_long": row.get("artifact_long") or "",
        "display_name": row.get("display_name") or "",
        "tags": list(row.get("tags") or []),
        "attributes": {str(k): str(v) for k, v in (row.get("attributes") or {}).items()},
        "embedding_model": row.get("embedding_model") or "",
        "embedding_config_hash": _hash_str(row.get("embedding_config_hash")),
    }


def _write_source_events(clickhouse: Any, case_id: str, source_id: str, dest: Path) -> int:
    """Sync: stream one source's events into an Arrow IPC file. Row count."""
    total = 0
    with pa.OSFile(str(dest), "wb") as sink:
        writer = pa.ipc.new_stream(sink, EVENT_ARROW_SCHEMA)
        for rows in clickhouse.iter_source_events(case_id, source_id, batch_size=10_000):
            normalized = [_normalize_event_row(r) for r in rows]
            for batch in pa.Table.from_pylist(normalized, schema=EVENT_ARROW_SCHEMA).to_batches():
                writer.write_batch(batch)
                total += batch.num_rows
        writer.close()
    return total


def _orphan_reference_warnings(
    stems: dict[str, list[dict[str, Any]]], skipped_ids: set[str]
) -> list[str]:
    """Warn about ids of skipped sources still embedded in JSON payloads.

    FK columns pointing at a skipped source are filtered out, but ids buried in
    JSON (chart configs, detector params, proposal payloads) are not — nothing
    enforces them, so they would ride along and resolve to nothing after
    import. Warn rather than drop: a dangling reference beats silently deleting
    an analyst's chart. Serialization-level scan, so no per-model knowledge.
    """
    if not skipped_ids:
        return []
    out: list[str] = []
    for stem, rows in stems.items():
        for row in rows:
            text = json.dumps(row, default=str)
            for sid in sorted(skipped_ids):
                if sid in text:
                    out.append(
                        f"{stem} {row.get('id')} references excluded source {sid}"
                        " — payload exported as-is"
                    )
    return out


async def _snapshot_postgres(store: PostgresStore, case_id: str) -> dict[str, Any]:
    """One session; returns case dict, per-stem row lists, and user refs.

    Reads run under REPEATABLE READ on Postgres so an ingest or annotation
    landing mid-export cannot produce a torn snapshot — an archive has to be a
    point-in-time record to be forensically meaningful. SQLite (tests) rejects
    that isolation level, and its single-writer model makes it moot, so the
    setting is applied per-dialect.
    """
    async with store.session_factory() as session:
        if session.bind is not None and session.bind.dialect.name == "postgresql":
            await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        case = (await session.execute(select(Case).where(Case.id == case_id))).scalar_one()
        timeline_ids = (
            (await session.execute(select(Timeline.id).where(Timeline.case_id == case_id)))
            .scalars()
            .all()
        )
        conversation_ids = (
            (
                await session.execute(
                    select(AgentConversation.id).where(AgentConversation.case_id == case_id)
                )
            )
            .scalars()
            .all()
        )
        stems: dict[str, list[dict[str, Any]]] = {}
        for stem, model, scope in _EXPORT_ENTITIES:
            if scope == "case":
                cond = model.case_id == case_id
            elif scope == "timeline":
                cond = model.timeline_id.in_(timeline_ids)
            else:  # conversation
                cond = model.conversation_id.in_(conversation_ids)
            rows = (await session.execute(select(model).where(cond))).scalars().all()
            stems[stem] = [_row_to_dict(r) for r in rows]

        user_ids = {case.owner_id} if case.owner_id else set()
        user_ids |= {r["user_id"] for r in stems["agent_conversations"] if r.get("user_id")}
        # created_by stores user ids (annotations, dispositions, sigma, …) —
        # collect them so authors map by username on import. Values that are
        # not user ids (system origins) simply resolve to no row below.
        for rows in stems.values():
            user_ids |= {r["created_by"] for r in rows if r.get("created_by")}
        users: dict[str, str] = {}
        if user_ids:
            pairs = (
                await session.execute(select(User.id, User.username).where(User.id.in_(user_ids)))
            ).all()
            users = dict(pairs)
        team_name = None
        if case.team_id:
            team_name = (
                await session.execute(select(Team.name).where(Team.id == case.team_id))
            ).scalar_one_or_none()
        return {
            "case": _row_to_dict(case),
            "stems": stems,
            "user_refs": {"users": users, "team": team_name},
        }


async def export_case(
    store: PostgresStore,
    clickhouse_factory: Callable[[], Any],
    case_id: str,
    *,
    include_blobs: bool,
    exported_by: str,
    dest_dir: Path,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> ExportResult:
    """Build the archive for one case. ClickHouse is only constructed when
    the case has sources (keeps empty-case export — and unit tests — CH-free)."""

    def _progress(phase: str) -> None:
        if progress:
            progress({"phase": phase})

    dest_dir.mkdir(parents=True, exist_ok=True)
    _progress("postgres")
    snapshot = await _snapshot_postgres(store, case_id)
    stems = snapshot["stems"]
    warnings: list[str] = []
    # Mid-ingest sources (status != "ready") must not ship: their events are
    # still being written, so both the NDJSON rows and the events/blobs loops
    # skip them. Analysts re-export after ingest completes.
    ready_sources = []
    skipped_ids: set[str] = set()
    for source in stems["sources"]:
        if source.get("status") == "ready":
            ready_sources.append(source)
        else:
            skipped_ids.add(source["id"])
            warnings.append(
                f"source {source['name']} not ready (status={source.get('status')})"
                " — excluded from export"
            )
    stems["sources"] = ready_sources
    # Dependents of a skipped source must not ship either: TimelineSource.
    # source_id is a real ForeignKey (ondelete CASCADE), so a join row
    # referencing a skipped source would abort the import at flush on
    # Postgres (SQLite lets it dangle silently). Rows whose source_id is
    # None (value-scoped dispositions) stay.
    ready_ids = {s["id"] for s in ready_sources}
    for stem in ("timeline_sources", "annotations", "finding_dispositions", "source_enrichments"):
        stems[stem] = [
            r for r in stems[stem] if r.get("source_id") is None or r["source_id"] in ready_ids
        ]
    warnings.extend(_orphan_reference_warnings(stems, skipped_ids))
    counts: dict[str, int] = {stem: len(rows) for stem, rows in stems.items()}
    counts["events"] = 0
    counts["blobs"] = 0

    archive_path = dest_dir / f"export-{case_id}.vestigo"
    writer = ArchiveWriter(archive_path)
    writer.add_bytes("postgres/case.json", json.dumps(snapshot["case"], indent=2).encode())
    writer.add_bytes(
        "postgres/user_refs.json", json.dumps(snapshot["user_refs"], indent=2).encode()
    )
    for stem, rows in stems.items():
        writer.add_bytes(f"postgres/{stem}.ndjson", _ndjson(rows))

    sources = stems["sources"]
    if sources:
        _progress("events")
        clickhouse = clickhouse_factory()
        for source in sources:
            dest = dest_dir / f"events-{source['id']}.arrow"
            try:
                n = await asyncio.to_thread(
                    _write_source_events, clickhouse, case_id, source["id"], dest
                )
                writer.add_file(f"events/{source['id']}.arrow", dest)
                counts["events"] += n
            finally:
                dest.unlink(missing_ok=True)
        if include_blobs:
            _progress("blobs")
            seen: set[str] = set()
            for source in sources:
                file_hash = source["file_hash"]
                if file_hash in seen:
                    # Content-addressed: two sources can share a blob; emit once.
                    continue
                seen.add(file_hash)
                blob = retention_path(file_hash)
                if blob.exists():
                    writer.add_file(f"blobs/{file_hash}", blob)
                    counts["blobs"] += 1
                else:
                    warnings.append(
                        f"source blob missing on disk: {source['name']} ({file_hash[:12]}…)"
                    )

    _progress("manifest")
    writer.finish(
        {
            "format_version": FORMAT_VERSION,
            "vestigo_version": __version__,
            "exported_at": datetime.now(UTC).isoformat(),
            "exported_by": exported_by,
            "case": {"id": case_id, "name": snapshot["case"]["name"]},
            "include_blobs": include_blobs,
            "counts": counts,
        }
    )
    return ExportResult(
        path=archive_path,
        bytes=archive_path.stat().st_size,
        counts=counts,
        warnings=warnings,
    )
