# X1 — Case Export/Import (`.vestigo` archive) — Design

Date: 2026-07-24. Status: approved design (brainstorming round complete).
Roadmap: `docs/ROADMAP.md` Milestone 9 / X1. Bundled work: ClickHouse pytest
marker (Milestone 2 residue).

## Goal

Any case — evidence, events, and all analyst work — leaves the instance as one
file and comes back intact, on the same or a different instance. Archive/restore
(backup) and cross-instance transfer are equal goals.

Non-goals this round: merging into an existing case, whole-instance backup,
migrating archives across the future S1+E1 data-model change (pinned as a
versioning rule instead — see §7).

## 1. Approach

API + background jobs, single-file zip archive. Reuses the in-memory JobStore
(deliberate single-process choice), the existing `iter_source_events` /
`insert_events_arrow` ClickHouse primitives, and the agent-conversation export
manifest pattern. No new dependencies: stdlib `zipfile` (per-member compression,
random access for manifest reads, Zip64 for >4 GiB) + pyarrow (already a
dependency). Rejected: CLI-only (bypasses the RBAC/audit model the roadmap
requires), DB-native dumps (no ID remap inside SQL text, brittle across schema
versions).

## 2. Archive format (`.vestigo`, zip)

```
manifest.json
postgres/case.json                  # single object
postgres/sources.ndjson             # one JSON object per line, to_dict() shape
postgres/timelines.ndjson
postgres/timeline_sources.ndjson
postgres/timeline_enrichers.ndjson
postgres/views.ndjson
postgres/saved_charts.ndjson
postgres/baseline_definitions.ndjson
postgres/detector_runs.ndjson
postgres/finding_dispositions.ndjson
postgres/annotations.ndjson
postgres/sigma_rules.ndjson
postgres/sigma_runs.ndjson
postgres/source_enrichments.ndjson
postgres/agent_conversations.ndjson
postgres/agent_messages.ndjson
postgres/agent_proposals.ndjson
postgres/audit_log.ndjson           # case-scoped entries only
postgres/user_refs.json             # usernames / team name strings only
events/<source_id>.arrow            # Arrow IPC stream, one file per source
blobs/<sha256>                      # optional, original source files
```

`manifest.json`:

```json
{
  "format_version": 1,
  "vestigo_version": "1.6.1",
  "exported_at": "<ISO-8601 UTC>",
  "exported_by": "<username>",
  "case": {"id": "<original case id>", "name": "<case name>"},
  "include_blobs": true,
  "counts": {"sources": 3, "events": 1234567, "annotations": 42, "...": "..."},
  "members": [{"path": "postgres/sources.ndjson", "sha256": "...", "bytes": 1234}]
}
```

`members[]` carries a SHA-256 per member; import verifies every member before
writing anything (forensic integrity + tamper detection).

`events/<source_id>.arrow` contains the full `_EVENT_COLUMNS` set **minus** the
server-materialized `search_blob` / `template_hash` (recomputed by ClickHouse on
insert). NDJSON members are deflate-compressed; `.arrow` and `blobs/` members
are stored uncompressed (already compact; avoids wasted CPU on multi-GB
exports).

### Excluded by design

| Excluded | Why |
|---|---|
| `source_field_stats`, viz cache | pure caches, recomputed on import |
| Qdrant embeddings | recomputed on import (timelines reset to un-embedded; re-embed is a user action on the target) |
| `enrichment_results_staging`, `enrichment_job_runs` | transient in-flight state |
| `agent_tokens`, `sessions`, `users`, `teams` rows, password material | secrets — never exported |
| `enricher_global_configs` | instance-wide, holds API keys |

`user_refs.json` exists so import can map attribution: `{"users": ["alice",
"bob"], "team": "ir-team"}` — strings only, no ids, no hashes.

## 3. Export flow

- `POST /api/cases/{case_id}/export?include_blobs=true|false` (default
  `false`: the events-only archive is the lean portable form; blobs roughly
  double the size — the UI surfaces the choice explicitly)
- Gate: `require_case_manage` — export is bulk case-data exfiltration.
- Audit: `case.export` (case id, actor, include_blobs, byte total on completion).
- JobStore kind `case_export`; progress per phase (`postgres`, `events`,
  `blobs`, `manifest`).

Steps inside the job:

1. Snapshot Postgres entities via direct ORM selects in the exporter (generic
   column serialization — no new `PostgresStore` methods; audit rows included
   as a case-scoped entity).
2. Per source: stream `iter_source_events(case_id, source_id)` → Arrow IPC
   file member.
3. `include_blobs`: add each source's retained blob via `_retention_path`
   (hardlink-or-copy semantics unchanged; missing blob = warning, not failure).
4. Hash every member as written; write `manifest.json` last.
5. Archive lands in a per-job temp dir; `job.result = {bytes, counts, warnings}`.

Download: `GET /api/cases/{case_id}/export/{job_id}/download` (same MANAGE gate)
streams the file as `attachment; filename=<case>-<date>.vestigo`, then deletes
it (`BackgroundTask`). Orphaned temp archives are swept at startup — the job
store is in-memory, so a restart already forfeits pending downloads.

## 4. Import flow

- `POST /api/cases/import` (multipart upload). Whether blobs are restored is
  determined by the archive's `include_blobs` flag, not by the request.
- Gate: any authenticated user; the importer becomes case owner. (Import
  creates data only under the importer's own ownership.)
- Audit: `case.import` (actor, archive case name, counts, warnings).
- JobStore kind `case_import`.

Steps:

1. Receive upload to temp (existing `receive_upload_to_tmp`; archives with
   blobs can exceed the 10 GiB `max_upload_bytes` default — documented,
   operator-configurable).
2. Read `manifest.json`; reject `format_version > 1` with a clear error.
3. Verify every member's SHA-256; any mismatch aborts before any write.
4. Create the new case (fresh id; archived name kept verbatim — case names are
   not unique-constrained).
5. Insert Postgres rows in dependency order (case → sources → timelines →
   timeline_sources/timeline_enrichers → views/charts/baselines/detector_runs/
   dispositions/annotations → sigma → agent tables → source_enrichments →
   audit), applying the old→new ID map (§5). Users map by username; unknown
   usernames fall back to the importer and are listed in `job.result.warnings`.
6. Per source: read `events/<source_id>.arrow`, rewrite the `case_id` and
   `source_id` columns to the new ids in each record batch, insert via
   `insert_events_arrow`. Event rows keep `file_hash` and all provenance
   columns unchanged.
7. Copy `blobs/<sha256>` members into the retention dir (respecting the
   instance-global sharing rule — `source_hash_in_use` guards blobs shared
   with other cases). Sources whose blob is absent still import (events are
   complete); blob download on the target simply 404s — recorded as a warning.
8. Post-ingest parity: run the same per-source field-stats computation a fresh
   ingest runs. Timeline embedding columns are reset to the un-embedded state.
9. `job.result = {case_id, counts, warnings}`; temp upload removed.

Failure policy: import is all-or-nothing per case — on any error after case
creation, the partial case is deleted via the existing `delete_case` cascade
(Qdrant no-op since nothing was embedded) and the job fails with the stage
named in `error`.

## 5. ID and access strategy (decided 2026-07-24)

- **Restore is always as-new-case.** No merge into an existing case, no
  conflict path: `(case_id, file_hash)` uniqueness can never collide under a
  fresh case id. This deliberately simplifies the roadmap's
  "restore-as-new-case vs. conflict-abort" fork to the first branch.
- **Entity IDs**: every Postgres row gets a fresh id via the existing
  `generate_id`; an in-memory old→new map rewrites all references
  (`case_id`, `timeline_id`, `source_id`, `conversation_id`, `event_id` in
  annotations stays — see next bullet, user refs via `user_refs`).
- **Event IDs are preserved verbatim.** `derive_event_id` is case-id-bound,
  but all event queries are case-scoped, so imported events keep their
  original `event_id` with zero collision risk — and annotation→event
  cross-references survive for free. The roadmap's "UUIDv5 dedup anchor"
  wording only matters for into-existing-case merges, which this design does
  not support; deviation is deliberate.
- **Access: importer-owned only.** The imported case is a personal case owned
  by the importer (MANAGE); admins retain their universal MANAGE. No other
  user or team gets any grant, regardless of the source instance's
  ownership/team. Exported owner/team appear only as strings in
  `user_refs.json` for attribution mapping. Granting access after import uses
  the existing team-assign/share flow — an explicit, audited action.
- **Attribution ≠ access.** Annotation/disposition/conversation authors map
  by username when the user exists on the target; otherwise they fall back to
  the importer with a warning. This is display data and confers no permissions.

## 6. API surface

| Endpoint | Gate | Job kind | Audit |
|---|---|---|---|
| `POST /api/cases/{case_id}/export` | `require_case_manage` | `case_export` | `case.export` |
| `GET /api/cases/{case_id}/export/{job_id}/download` | `require_case_manage` | — | not separately audited (plain GET, excluded from the middleware; the `case.export` row already records the exfiltration event) |
| `POST /api/cases/import` | authenticated | `case_import` | `case.import` |

Job status via the existing `GET /api/jobs/{job_id}` (visibility rules already
cover creator/admin/case-READ; import jobs are visible to creator + admin).

New router: `src/vestigo/api/routers/transfer.py`, registered in `api/main.py`.

## 7. Versioning

`format_version: 1`. Import rejects anything newer with an actionable error.
When the S1+E1 data-model migration lands, the archive format goes to v2 and
the importer grows a v1→v2 migration — the same "keep v1 readable" discipline
as the Parquet interchange. This pins the sequencing question the roadmap
raises (X1 vs. S1+E1) without blocking X1 on that design round.

## 8. Module layout

New package `src/vestigo/transfer/`:

- `archive.py` — manifest model (pydantic), zip writer/reader, per-member
  hashing + verification. No Vestigo store imports; pure format code.
- `exporter.py` — Postgres snapshot + event streaming + blob collection →
  archive. Depends on `PostgresStore`, `ClickHouseStore`, retention helpers.
- `importer.py` — verification, ID remap, ordered inserts, Arrow column
  rewrite, blob placement, stats recompute, cleanup.

Touched existing files: `src/vestigo/core/retention.py` (new home for the
content-addressed blob helpers, moved from `api/routers/cases.py` — no
`postgres.py` changes), `src/vestigo/api/main.py` (router include, temp-sweep at
startup), `pyproject.toml` (pytest marker), `docs/ROADMAP.md` + `CHANGELOG.md`
(on completion).

Frontend (minimal): Export button in case settings (start job → poll →
auto-download when ready); Import upload on the case list (start job → poll →
navigate to the new case). No new dependencies; follows the existing job-polling
pattern used by ingest.

## 9. Testing

Unit (SQLite-backed `PostgresStore`, no ClickHouse):

- manifest write/read round-trip; member-hash tamper → abort before writes
- ID-remap referential integrity: export a case exercising every entity type,
  import, walk every Postgres-side reference (proposals→conversations,
  dispositions→timelines, charts→timelines, …) — all resolve; the
  annotation→event cross-reference is asserted in the ClickHouse round-trip
  below (event ids are preserved verbatim)
- secrets exclusion: archive bytes contain no `password_hash`, no token hashes,
  no enricher API keys
- RBAC: export 403 for CONTRIBUTE, 404 for non-member; import 401 anonymous
- audit rows written for `case.export` / `case.import`
- `include_blobs` on/off; missing blob → warning, import still succeeds
- import failure mid-way → partial case fully deleted

ClickHouse round-trip (requires dev stack):

- ingest small fixture → export → import → per-source event counts and
  `content_hash` multisets equal; `iter_source_events` equality modulo
  `case_id`/`source_id` rewrite

Bundled marker work (roadmap Milestone 2 residue):

- register `clickhouse` marker in `pyproject.toml`
  `[tool.pytest.ini_options] markers`
- apply `pytestmark = pytest.mark.clickhouse` to every `tests/*_clickhouse.py`
  file that skips when the dev stack is absent (11 files found; roadmap said
  ten — all get the marker)
- result: `pytest -m clickhouse` selects them; absence of the marker in a plain
  run is assertable in CI

## 10. Resolved decisions

| Question | Decision |
|---|---|
| Scope | Full X1: export + import, same/cross-instance |
| Bundled work | ClickHouse pytest marker only; S1+E1 stays parked |
| Execution | API + JobStore jobs, not CLI-only |
| Container | zip (per-member compression, random access, Zip64) |
| Restore target | always a new case; no merge |
| Event IDs | preserved verbatim (case-scoped queries make it safe) |
| Imported-case access | importer-owned personal case only; no auto-grants |
| Attribution | username mapping, importer fallback + warning |
| Secrets | never exported (tokens, passwords, enricher keys) |
| Format vs. S1+E1 | pin v1 now; v2 + migration when S1+E1 lands |
