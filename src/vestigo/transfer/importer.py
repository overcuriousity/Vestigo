"""Case import: verify, remap IDs, restore Postgres rows + events + blobs.

Always restores as a NEW case owned by the importer (no merge, no conflict
path). Every Postgres row gets a fresh id; an old→new map rewrites all
references. Event ids are preserved verbatim — event queries are case-scoped
and preserved ids keep annotation→event cross-references intact. Any failure
after case creation deletes the partial case (Postgres cascade + ClickHouse
partitions) before the error propagates.

Memory: nothing here is materialized whole. Events stream batch-by-batch and
every Postgres stem streams row-by-row (``ArchiveReader.iter_ndjson``), so peak
memory scales with the largest single *row*, not the largest entity. The id
prescan streams the same stems and keeps only id strings. An archive is an
untrusted upload, so this is a bound, not a tuning detail: reading one member
whole was an out-of-memory kill any authenticated user could trigger with a
crafted NDJSON member — see ``archive.METADATA_PREFIX``, which caps the same
members from the other side.

The one thing that scales with the case is the SQLAlchemy session's identity
map, which holds a flush's worth of ORM objects; the per-stem flush bounds it
to one entity's rows. Chunk the flush if a case ever outgrows that — the
prescan already resolves the full id map up front, which is what dependent
stems need.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from itertools import batched
from pathlib import Path
from typing import Any

import pyarrow as pa
from sqlalchemy import JSON, delete, select

from vestigo.core.retention import retain_file, retention_path
from vestigo.db._arrow_schema import EVENT_ARROW_SCHEMA
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
from vestigo.transfer.archive import ArchiveFormatError, ArchiveReader, cap_warnings

_SHA256_RE = re.compile(r"[0-9a-f]{64}")

# Usernames per `IN (...)` when resolving the archive's user map (see
# _resolve_users). Keeps one query per batch well inside Postgres' parameter
# limits regardless of how many users the archive claims.
_USER_LOOKUP_BATCH = 1000

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
    """Old archive id → new local id, keyed by (kind, old id).

    Built in full by :func:`_prescan_ids` before any row is revived, then
    frozen. That is a performance requirement, not a style choice — see
    ``substitute``.
    """

    def __init__(self) -> None:
        self._map: dict[tuple[str, str], str] = {}
        self._pattern: re.Pattern[str] | None = None
        self._by_old: dict[str, str] = {}
        self._new_ids: set[str] = set()
        self._frozen = False
        # Number of times the substitution alternation has been compiled.
        # Asserted by the tests: it must stay at 1 for a whole import.
        self.compiles = 0

    def remap(self, kind: str, old: Any) -> Any:
        if old is None:
            return None
        key = (kind, str(old))
        if key not in self._map:
            if self._frozen:
                raise RuntimeError(
                    f"id map is frozen but {key} is unmapped — _prescan_ids missed a ref column"
                )
            self._set(key, self._fresh_id(kind))
        return self._map[key]

    def lookup(self, old: Any) -> Any:
        """Existing mapping for an id whose kind is not known statically.

        ``audit_log.target_id`` spans sources, timelines, annotations, teams
        and users, so the static ``refs`` mechanism cannot express it. Archive
        ids are globally unique (``generate_id`` appends a random suffix), so
        the kind is not needed to resolve one. Unmapped values pass through —
        ``target_id`` also holds ids of entities the archive never carried
        (teams, users, agent tokens) and non-id targets.
        """
        return old if old is None else self._by_old.get(str(old), old)

    def pin(self, kind: str, old: Any, new: str) -> None:
        """Force a mapping (the case id is created before the rows are read)."""
        self._set((kind, str(old)), new)

    def bulk_add(self, pairs: list[tuple[str, str]], old_ids: set[str]) -> None:
        """Add every ``(kind, old id)`` seen in the archive in one go.

        ``old_ids`` is every id string the archive mentions. A generated id
        colliding with one would be rewritten again by ``substitute``; one
        colliding with an already-generated id would be a primary-key
        violation at flush. ``generate_id`` appends only 8 hex characters, so
        at tens of thousands of rows of one kind neither is negligible —
        cheap to rule out here rather than to debug later.
        """
        for kind, old in pairs:
            key = (kind, old)
            if key not in self._map:
                self._set(key, self._fresh_id(kind, avoid=old_ids))

    def freeze(self) -> None:
        """No new mappings from here on; ``remap`` raises instead of adding."""
        self._frozen = True

    def _fresh_id(self, kind: str, avoid: set[str] | None = None) -> str:
        new = generate_id(kind)
        while new in self._new_ids or new in self._by_old or (avoid and new in avoid):
            new = generate_id(kind)
        return new

    def _set(self, key: tuple[str, str], new: str) -> None:
        self._map[key] = new
        self._by_old[key[1]] = new
        self._new_ids.add(new)
        self._pattern = None  # rebuilt lazily on the next payload rewrite

    def substitute(self, text: str) -> str:
        """Rewrite every known old id appearing anywhere in ``text``.

        One pass over the text via a single alternation, so a freshly written
        id can never be rewritten again by a later mapping. Ids also appear
        inside longer strings (filter expressions, not just whole JSON
        values), so this stays substring-level rather than matching whole
        values.

        The alternation is expensive to build and cheap to apply — measured,
        200k ids compile in ~2s and substitution is then free. Adding a
        mapping invalidates it, so the map must be complete *before* the
        revive loop starts: growing it row by row recompiled once per row and
        made import quadratic (14s for 1600 audit rows). ``freeze`` is what
        keeps that from creeping back.
        """
        if not self._by_old:
            return text
        if self._pattern is None:
            self._pattern = re.compile(
                "|".join(re.escape(old) for old in sorted(self._by_old, key=len, reverse=True))
            )
            self.compiles += 1
        return self._pattern.sub(lambda m: self._by_old[m.group(0)], text)


def _prescan_ids(reader: ArchiveReader, idmap: _IdMap) -> None:
    """Populate the id map from every stem before any row is revived.

    Two reasons this is a separate pass. Performance: ``substitute`` compiles
    one alternation over the whole map, so a map that grows during the revive
    loop recompiles per row (see ``_IdMap.substitute``). Correctness: rewriting
    embedded ids used to see only the mappings made so far, so a chart config
    embedding an annotation id survived unrewritten — charts revive before
    annotations.

    Memory is unchanged: each stem streams row by row, same as the revive loop,
    and only the id strings are retained. The cost is one extra NDJSON parse
    per stem.

    Synchronous and CPU-bound (it parses every metadata member), so callers run
    it in a worker thread. Safe to do: it is a full await, so the revive loop
    never reads the map while this is still writing it.
    """
    pairs: list[tuple[str, str]] = []
    old_ids: set[str] = set()
    for stem, _model, refs in _IMPORT_SPECS:
        for row in reader.iter_ndjson(f"postgres/{stem}.ndjson"):
            for column, kind in refs.items():
                value = row.get(column)
                if value is not None:
                    pairs.append((kind, str(value)))
                    old_ids.add(str(value))
    idmap.bulk_add(pairs, old_ids)
    idmap.freeze()


def _validated_case(data: Any) -> dict[str, Any]:
    """Type-check the archive's case record before it reaches ``create_case``.

    An archive is an untrusted upload, and these three fields are the ones
    that go straight into Postgres. Failing here surfaces as a clean
    ArchiveFormatError instead of a TypeError from inside the flush.
    """
    if not isinstance(data, dict):
        raise ArchiveFormatError("postgres/case.json is not a JSON object")
    for key in ("id", "name"):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ArchiveFormatError(f"case.{key} must be a non-empty string")
    if data.get("description") is not None and not isinstance(data["description"], str):
        raise ArchiveFormatError("case.description must be a string or null")
    return data


def _validated_user_refs(data: Any) -> dict[str, Any]:
    """Type-check the archived user id → username map and team name."""
    if not isinstance(data, dict):
        raise ArchiveFormatError("postgres/user_refs.json is not a JSON object")
    users = data.get("users")
    if users is None:
        users = {}
    if not isinstance(users, dict) or any(
        not isinstance(k, str) or not isinstance(v, str) for k, v in users.items()
    ):
        raise ArchiveFormatError("user_refs.users must map string ids to string usernames")
    if data.get("team") is not None and not isinstance(data["team"], str):
        raise ArchiveFormatError("user_refs.team must be a string or null")
    return {**data, "users": users}


def _remap_json_ids(value: Any, idmap: _IdMap) -> Any:
    """Rewrite archived ids embedded in a JSON payload (proposal events, view
    filters, chart configs) through the old→new map. The map is complete before
    the revive loop starts (``_prescan_ids``), so a payload's references are
    rewritten regardless of the order its stem revives in."""
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


def _source_ref(row: dict[str, Any]) -> tuple[str, str]:
    """``(id, file_hash)`` from an archived source row, type-checked.

    The events and blobs phases key off exactly these two fields. Both are
    NOT NULL on the model, so a row without them is a malformed archive — but
    reaching that conclusion via a ``KeyError`` raised from the middle of the
    events loop tells an operator nothing. Fail with the format error instead,
    while the revive loop is still walking the stem.
    """
    values: list[str] = []
    for column in ("id", "file_hash"):
        value = row.get(column)
        if not isinstance(value, str) or not value:
            raise ArchiveFormatError(
                f"postgres/sources.ndjson row has no usable {column} (string, non-empty)"
            )
        values.append(value)
    return values[0], values[1]


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
            # The IPC stream is attacker-supplied and insert_events_arrow hands
            # whatever it gets straight to ClickHouse. Without this, a renamed
            # column makes get_field_index return -1 (set_column then fails
            # obscurely) and a missing one silently takes a server-side default
            # instead of the value _normalize_event_row would have written.
            # Per batch, not once: an IPC stream may change schema mid-stream.
            if not batch.schema.equals(EVENT_ARROW_SCHEMA):
                raise ArchiveFormatError(
                    f"{arcname}: event schema does not match this version's event schema"
                )
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
    job_id: str | None = None,
    progress: Callable[[dict[str, Any]], None] | None = None,
) -> ImportResult:
    """Restore an archive as a new case owned by `owner`. All-or-nothing."""

    def _progress(phase: str, total: int | None = None) -> None:
        """Enter a phase, resetting the unit counters in the same write.

        ``JobStore.update`` *merges* progress dicts, so a phase that only sets
        ``phase`` inherits the previous phase's ``processed``/``total`` and the
        UI shows a percentage from the wrong denominator. Always reset both
        here, and let ``_advance`` move ``processed`` within the phase.

        ``total=None`` means "this phase does not count items" (verifying the
        archive's manifest is one operation, not N of them). The UI renders
        that as an indeterminate bar rather than as a stuck 0%.
        """
        if progress:
            progress({"phase": phase, "processed": 0, "total": total})

    def _advance(processed: int) -> None:
        if progress:
            progress({"processed": processed})

    _progress("verify")
    reader = ArchiveReader(archive_path)  # raises ArchiveFormatError on bad manifest
    # SHA-256 over every member of a multi-GiB archive: off the event loop, or
    # the whole API stalls for the length of the import. Raises before ANY write.
    await asyncio.to_thread(reader.verify_members)
    # Reads are restricted to manifest-listed members (ArchiveReader enforces
    # it), so anything else in the zip is untrusted and never reaches the
    # importer — and a member the exporter always writes cannot go missing
    # without failing the import.
    verified = reader.verified_names
    case_data = _validated_case(reader.read_json("postgres/case.json"))
    user_refs = _validated_user_refs(reader.read_json("postgres/user_refs.json"))

    counts: dict[str, int] = {"events": 0, "blobs": 0}
    warnings: list[str] = []
    idmap = _IdMap()
    new_case_id = generate_id("case")
    # Pin the case remap to the id we actually create with, then resolve every
    # other id in one pass — the revive loop must not grow the map (see
    # _prescan_ids).
    idmap.pin("case", case_data["id"], new_case_id)
    await asyncio.to_thread(_prescan_ids, reader, idmap)  # parses every stem — CPU-bound
    import_marker = {
        "job_id": job_id,
        "by": owner.username,
        "at": datetime.now(UTC).isoformat(),
        "archive_case_id": case_data["id"],
    }

    created = False
    inserted_sources: list[str] = []
    # Blobs this run put into the instance-global retention dir. Only ones that
    # were not already there — a pre-existing blob is content-addressed and
    # shared with whatever else references it, so cleanup must never touch it.
    retained_blobs: list[Path] = []
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
            # (old source id, file_hash) for the events and blobs phases,
            # collected while this loop already has the stem open rather than
            # by re-parsing sources.ndjson a third time.
            source_refs: list[tuple[str, str]] = []
            for stem, model, refs in _IMPORT_SPECS:
                rows_seen = 0
                for row in reader.iter_ndjson(f"postgres/{stem}.ndjson"):
                    rows_seen += 1
                    if stem == "sources":
                        source_refs.append(_source_ref(row))
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
                        # target_id spans every entity type, so no static ref
                        # column can cover it; resolve it kind-agnostically.
                        # Left unremapped, a restored audit trail would point
                        # at ids that exist nowhere on this instance.
                        obj.target_id = idmap.lookup(row.get("target_id"))
                        # Actor, action and timestamp are restored verbatim so
                        # the chain of custody survives the move — but nothing
                        # here vouches for them, and any authenticated user can
                        # upload an archive. Stamp every row so an auditor can
                        # tell a local action from one an archive asserted.
                        detail = obj.detail if isinstance(obj.detail, dict) else {}
                        obj.detail = {**detail, "imported": import_marker}
                    # created_by holds user ids: remap via user_refs (username
                    # → local id, importer fallback + warning). Values absent
                    # from user_refs are not user ids (e.g. system origins) —
                    # keep them verbatim, no warning.
                    created_by = getattr(obj, "created_by", None)
                    if created_by is not None and str(created_by) in user_refs["users"]:
                        obj.created_by = _local_user(created_by)
                    session.add(obj)
                counts[stem] = rows_seen
                await session.flush()
            await session.commit()
        if embedded_timelines:
            warnings.append(
                f"{embedded_timelines} timeline(s) were embedded on the source instance — "
                "vectors are not portable; re-run embedding to restore semantic search"
            )

        event_members = [n for n in verified if n.startswith("events/") and n.endswith(".arrow")]
        if event_members:
            _progress("events", total=len(event_members))
            clickhouse = clickhouse_factory()
        for old_source_id, _file_hash in source_refs:
            new_source_id = idmap.remap("source", old_source_id)
            arcname = f"events/{old_source_id}.arrow"
            if arcname in verified:
                # Track BEFORE inserting: a mid-source failure may leave a
                # partially written partition, and cleanup must drop it.
                inserted_sources.append(new_source_id)
                n = await asyncio.to_thread(
                    _insert_source_events, clickhouse, reader, arcname, new_case_id, new_source_id
                )
                counts["events"] += n
                # Counts restored members, so it tracks the `event_members`
                # denominator rather than the wider `source_refs` loop.
                _advance(len(inserted_sources))

        # Sorted, because `verified` is a set: warning order in the job result
        # (and from there the audit detail) must not vary run to run.
        blob_members = sorted(n for n in verified if n.startswith("blobs/"))
        if blob_members:
            _progress("blobs", total=len(blob_members))
        # The retention dir is instance-global, so only blobs an archived
        # source actually claims may land in it. Content is verified against
        # the member name below, which stops an existing blob being poisoned;
        # this stops an archive planting unrelated files there at all.
        referenced = {file_hash for _sid, file_hash in source_refs}
        for done, arcname in enumerate(blob_members, start=1):
            sha = arcname.removeprefix("blobs/")
            if not _SHA256_RE.fullmatch(sha):
                warnings.append(f"skipping suspicious blob member: {arcname}")
                _advance(done)
                continue
            if sha not in referenced:
                warnings.append(f"ignoring blob no source references: {sha[:12]}…")
                _advance(done)
                continue
            with tempfile.NamedTemporaryFile(delete=False) as tmp:
                tmp_path = Path(tmp.name)
            try:
                # Off the event loop: this hashes and writes the whole blob.
                digest = await asyncio.to_thread(reader.extract_to, arcname, tmp_path)
                if digest != sha:
                    # The blob's content must hash to its content-addressed
                    # name, or a crafted member would poison the
                    # instance-global retention dir (retain_file
                    # short-circuits later uploads of the real hash).
                    raise ArchiveFormatError(
                        f"blob content does not hash to its member name: {arcname}"
                    )
                dest = retention_path(sha)
                # Checked before the call, because retain_file short-circuits
                # on an existing path: only a blob this run actually created is
                # ours to remove if the import later fails.
                fresh = not dest.exists()
                await asyncio.to_thread(retain_file, tmp_path, dest)
                if fresh:
                    retained_blobs.append(dest)
                counts["blobs"] += 1
            finally:
                tmp_path.unlink(missing_ok=True)
            _advance(done)
        blobbed = {n.removeprefix("blobs/") for n in blob_members}
        if blob_members:
            # Deduped by hash, not per source row: sources share blobs, and one
            # warning per row would flood a job result that rides into the
            # audit detail JSON.
            missing = sorted(referenced - blobbed)
            for file_hash in missing:
                warnings.append(
                    f"no blob in archive for {file_hash[:12]}… — events restored, "
                    "original file absent"
                )

        if clickhouse is not None and inserted_sources:
            _progress("stats", total=len(inserted_sources))
            for done, new_source_id in enumerate(inserted_sources, start=1):
                try:
                    await refresh_source_field_stats(store, clickhouse, new_case_id, new_source_id)
                except Exception as exc:  # noqa: BLE001 — stats never fail an import
                    warnings.append(
                        f"field stats recompute failed for source {new_source_id}: {exc}"
                    )
                _advance(done)
    except Exception:
        if clickhouse is not None:
            for new_source_id in inserted_sources:
                with contextlib.suppress(Exception):  # best-effort cleanup
                    await asyncio.to_thread(
                        clickhouse.delete_source_events, new_case_id, new_source_id
                    )
        # The retention dir is instance-global and nothing else tracks these:
        # without this a repeatedly-failing import accumulates case file content
        # on disk that no Source row references. Only blobs this run created
        # are listed (see `fresh` above), so a blob shared with an existing case
        # is never removed.
        for blob_path in retained_blobs:
            with contextlib.suppress(OSError):
                blob_path.unlink(missing_ok=True)
        if created:
            await store.delete_case(new_case_id)
        raise
    finally:
        reader.close()

    return ImportResult(case_id=new_case_id, counts=counts, warnings=cap_warnings(warnings))


async def _resolve_users(
    session: Any,
    user_refs: dict[str, Any],
    owner: User,
    warnings: list[str],
) -> dict[str, str]:
    """Archived user id → local user id, via username. One query, one warning
    per username that this instance does not know (falls back to the importer).
    """
    archived: dict[str, str] = user_refs["users"]
    if not archived:
        return {}
    # Batched: an archive can declare an arbitrarily large user map, and one
    # unbounded IN (...) is a cheap way to make Postgres do too much work.
    local_ids: dict[str, str] = {}
    for batch in batched(sorted(set(archived.values())), _USER_LOOKUP_BATCH, strict=False):
        local_ids.update(
            (
                await session.execute(
                    select(User.username, User.id).where(User.username.in_(batch))
                )
            ).all()
        )
    for username in sorted(set(archived.values()) - set(local_ids)):
        warnings.append(f"user {username} not found on this instance — attributed to importer")
    return {old_id: local_ids.get(username, owner.id) for old_id, username in archived.items()}
