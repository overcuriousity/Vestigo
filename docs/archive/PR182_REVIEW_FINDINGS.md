# Review: PR #182 — case export/import (X1)

*Reviewed 2026-07-25 against branch `feat/case-export-import` (PR #182, base `main`, 49 files,
+7893/−80). Focus: correctness, project conventions, performance, test coverage, security.*

The PR adds the `.vestigo` case archive format (zip: `manifest.json` + `postgres/*.ndjson` +
`events/<source_id>.arrow` + optional `blobs/<sha256>`), an exporter that snapshots Postgres by
column introspection under REPEATABLE READ and streams ClickHouse events to Arrow IPC, an
importer that verifies every member by SHA-256, remaps every id and restores as a **new** case
owned by the importer (all-or-nothing, with cleanup of partial Postgres/ClickHouse/blob state),
three endpoints behind the JobStore with an instance-wide concurrency cap, and the frontend
export/import dialogs plus an "imported" badge on restored audit rows.

The archive threat model is taken seriously and written down: two-tier size caps (total expanded
plus a per-metadata-member ceiling), declared sizes cross-checked against the zip directory, a
streaming re-check that catches a lying local header, member-name validation, reads restricted to
manifest-listed members, blob content verified against its content-addressed name, and blobs no
source references rejected. `create_if_under` closes a real check-then-create TOCTOU in the job
store. Nothing in the review contradicts that work — the findings below are the edges it did not
reach.

**Status: 2 resolved before merge, 6 open.** The two resolved ones were CI-blocking; the rest were
judged non-blocking by the maintainer and merged as-is (`009e50c`, 2026-07-25).

## Resolved before merge

| # | Status | What happened |
|---|--------|----------------|
| A | ✅ Fixed | **`py/path-injection` in the download handler** (2 CodeQL high alerts). `GET /cases/{case_id}/export/{job_id}/download` passed the raw URL path segment to `new_archive_path`, which builds a path under the transfer temp root from it. Not exploitable — the job-store lookup that follows rejects anything that is not a real job id — but that is a property of the store's keys, not a check. Fixed in two layers: `archive.is_job_id`/`new_archive_path` now refuse a non-job-id outright and the router 404s before touching the filesystem (`e5d58f0`), and the path is built from the *stored* `job.id` rather than the URL segment, which removes the taint flow entirely (`0cf46a4`). Tests: `test_download_rejects_non_job_id`, `test_new_archive_path_refuses_non_job_id`. |
| B | ✅ Fixed | **`ruff format --check` failing CI.** Pre-existing on `main` for four docs files (`INPUT_FORMATS.md`, two `2026-07-19-agent-*` plans, `2026-07-21-agent-tool-result-fidelity-design.md`); this PR added a fifth (`2026-07-24-case-export-import.md`). Ruff formats Python blocks inside markdown, and none of the five had been run through it. Fixed by running `ruff format` over the repo (`e5d58f0`) — which also turned the backend check green on `main`. |

## Open — medium

1. **A failed import is never audited.** `transfer.py::_run_import_job`'s `except` only logs and
   marks the job failed, while `_run_export_job` records a `case.export` audit row with
   `status_code=500` on failure. The `AuthAuditMiddleware` row covers the `202` on
   `POST /api/cases/import`, not the reason the restore was rejected. For a tool whose audit trail
   is a chain-of-custody artifact, a rejected archive upload — the shape a hostile or corrupt
   archive takes — is precisely the event worth recording. Mirror the export handler's
   `record_audit` call (including its "never let auditing fail the job" nesting).

2. **`_insert_source_events` lets an untrusted Arrow stream size its own batches.** The importer
   opens the member with `reader.open_member` (deliberately unbounded — `verify_members` has
   already confirmed byte-identity) and hands it to `pa.ipc.open_stream`. Byte-identity is not
   *shape*: `verify_members` says nothing about how the member's bytes are divided into record
   batches, so a single enormous batch inside a legitimately-sized member is materialized whole in
   the worker thread. Bounded only by the member's size, which is bounded only by the 200 GiB
   total expansion cap. Cap `batch.num_rows`/`batch.nbytes` per batch, or extend the per-member
   ceiling to `events/` the way `postgres/` already has one.

3. **The frontend buffers the whole archive in memory to download it.**
   `frontend/src/api/transfer.ts::downloadExport` calls `fetchBlobGet` and then
   `triggerDownload(blob, …)`, so a multi-GiB export is fully resident in the tab before a byte
   reaches disk. Auth is session-cookie based, so a plain anchor/`window.location` navigation to
   the download URL streams straight to disk and additionally honors the server's
   `Content-Disposition` — which the client currently ignores in favor of re-deriving the filename
   from `case.name`.

4. **Orphan `events/*.arrow` members are skipped silently.** The events loop walks `source_refs`
   and inserts only where `f"events/{old_source_id}.arrow" in verified`; an event member matching
   no source row is never mentioned. Orphan *blobs* do warn (`ignoring blob no source
   references: …`). A truncated or crafted archive therefore restores quietly incomplete. Warn on
   the leftovers for symmetry.

## Open — low

5. **`ImportCaseDialog` does not reset state on reopen.** It renders
   `<Dialog open={open} onOpenChange={setOpen}>`, while `ExportCaseDialog` clears `jobId`/`error`
   /`downloadedRef` in its `onOpenChange`. Reopening after an import that produced warnings shows
   the previous run's warning list and a "Go to case" button pointing at the previously imported
   case. Clear `jobId`, `file` and `error` when `next` is true.

6. **Archive format forward-compatibility gap.** `_prescan_ids` and the revive loop both require
   `postgres/<stem>.ndjson` for every entry in `_IMPORT_SPECS`; a missing member raises
   `ArchiveFormatError` from `ArchiveReader._declared_size`. Adding an entity to `_IMPORT_SPECS` in
   a later release therefore makes that release unable to read archives written by the current one,
   with `FORMAT_VERSION` still at `1`. Either treat a missing stem as empty (the backward-compatible
   reading) or make a `FORMAT_VERSION` bump mandatory on every `_IMPORT_SPECS` addition — and say
   which, in a comment on the list, since the next person to add an entity is the one who needs to
   know.

## Noted, not filed as work

- `import_case` restores only `id`/`name`/`description` from `postgres/case.json`;
  `created_at`/`updated_at` are dropped and the new case gets fresh ones. Defensible (the import
  date *is* the real date for a restored case) but undocumented — worth a line in the module
  docstring either way.
- `_validated_case` checks non-empty strings but not lengths, so an over-long `name` (>255) fails
  inside the flush rather than as a clean `ArchiveFormatError`. The all-or-nothing cleanup path
  handles it correctly; this is error-message quality, not correctness. Same for every other string
  column in `_revive`.
- `sweep_stale()` runs from `_run_export_job` with the 24 h TTL while other exports may be in
  flight, so an export running longer than 24 h could have its own working directory swept. There
  is a similarly narrow race between a very early export and the age-independent startup sweep in
  `_sweep_stale_transfer_archives`. Both are unreachable in practice at
  `transfer_max_concurrent=2`; recorded only so the assumption is written down.
- `exporter._snapshot_postgres`'s docstring calls the result point-in-time, but the ClickHouse
  event reads happen after the Postgres transaction closes and outside it. Safe only because
  non-`ready` sources are excluded from the export; the docstring should say so, since that is what
  the guarantee actually rests on.

## Test coverage assessment

Good: archive format (deflate bombs, duplicate members, lying local headers, unsafe member names,
version rejection), export filtering and warning aggregation, import id remapping with a
`compiles == 1` regression assert protecting the quadratic fix, blob-poisoning rejection,
cleanup-on-failure, API-level concurrency cap and admission race, plus a ClickHouse-marked
round-trip (`tests/test_transfer_roundtrip_clickhouse.py`). Gaps line up with the findings above:
no test for a missing `postgres/*` stem (#6), none for an oversized Arrow record batch (#2), none
asserting an audit row on a failed import (#1).
