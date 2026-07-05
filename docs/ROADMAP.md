# TraceSignal Roadmap — Phase 2 (hardening backlog)

Phase 1 (source management, timelines, explorer, anomaly engine, auth/RBAC/audit,
visualization, converters) is complete — see
[`docs/archive/ROADMAP_PHASE1.md`](./archive/ROADMAP_PHASE1.md).

This phase consolidates the remaining findings from the 2026-07-03 repository audit.
The audit's Critical/High items were fixed directly on `fix/audit-critical-high`:

- ✅ **C1** — Dockerfile CMD pointed at a nonexistent `api.main:app`; now `--factory create_app`.
- ✅ **H1** — CSV parser read the whole file into memory (`lines = list(fh)`); now streams with
  incremental byte-offset/line tracking (`ingestion/parser.py::_RecordTrackingIterator`).
- ✅ **H2** — Airgap enforcement: `tsig-web` no longer runs `npm install` on every start
  (builds only when `dist/` is missing; `TS_FRONTEND_REBUILD=1` forces); uvicorn reloader is
  development-only; embedding model load forces `HF_HUB_OFFLINE` unless `TS_ALLOW_ONLINE` and
  fails with an actionable message instead of silently downloading.
- ✅ **H3** — Blocking ClickHouse calls in async handlers (`list_events`, histogram, bulk
  annotate, field/artifact/tag listings, embedding-field recommenders) now go through
  `run_in_threadpool`, matching viz/anomaly endpoints. Convention: **every**
  `EventQueryService` call from an `async def` handler must be threadpool-wrapped.
- ✅ **H4** — Uploads: single-pass copy+hash off the event loop
  (`ingestion/files.py::copy_and_hash`), capped by `TS_MAX_UPLOAD_BYTES`
  (default 10 GiB, 0 disables) with a 413 mid-stream rejection.

Point-in-time PR review findings are archived under `docs/archive/PR{N}_REVIEW_FINDINGS.md`
(full unrestricted finding set, one file per reviewed PR) once triaged into this backlog or
resolved — this file holds only the condensed, still-open action items.

## Milestone 1 — correctness & forensic integrity (Medium severity)


## Milestone 2 — high-leverage improvements

- [ ] **M15 residue — `list_fields_by_artifact` stays live (deliberate).** The per-source
  field-stats cache (`db/field_stats.py`, shipped) converted `field_inventory`,
  `list_fields`, and `field_coverage`; the embedding wizard's `list_fields_by_artifact`
  keeps its live scan because its cost is the randomized per-artifact value sampling that
  feeds content-aware cohesion scoring — caching only its inventory would save little.
  Revisit only if the wizard's latency becomes a complaint. HyperLogLog sketches for exact
  merged `distinct` likewise deferred (max-across-sources approximation documented in the
  module).

- [ ] **M20 — Ingest-throughput follow-ups (only if needed).** `TS_INGEST_BATCH_SIZE`
  (default 20k, one HTTP insert per batch) should carry a 100 GiB ingest fine (~5k inserts
  for ~100M rows). Revisit only if measured insufficient: ClickHouse native protocol
  (clickhouse-driver, port 9000), `async_insert`, parse/insert pipelining (parser thread
  feeding an insert thread).

- [ ] **M21 — Storage redundancy cleanup (from 2026-07-05 data-model audit,
  `docs/MODEL_REFINEMENT.md#storage-placement-audit-2026-07-05`).** Three items:
  1. Drop `Event.vector_id` (ClickHouse column) — always identical to `event_id`, use
     `event_id` directly as the Qdrant point ID.
  2. Drop `Source.embedding_model`/`Source.embedding_config` (Postgres) — dead fields, never
     written; live config is Timeline-scoped.
  3. Trim the Qdrant payload to filter-relevant fields only (`case_id`, `source_id`, `artifact`,
     `timestamp`, `tags`); resolve full event detail via a ClickHouse `event_id IN (...)` lookup
     post-search instead of mirroring the whole row. Also fixes the `tags` staleness gap
     (annotation tags added after embed never reach the Qdrant payload).

## Milestone 3 — polish

- [ ] Split `api/routers/events.py` (1500+ lines: query parsing, export streaming, anomaly
  orchestration, bulk annotation) opportunistically when next touched — not proactively.
- [ ] `ClickHouseStore._host/_port` string-splitting breaks on `https://` and creds-in-URL
  forms — use `urllib.parse`.
- [ ] Startup config sanity report: log resolved offline mode, cookie security
  (warn when `environment=production` and `auth_cookie_secure=false`), datastore targets.
- [ ] Large-file ingest regression test: bound peak memory (or assert lazy yielding) over a
  generated ~100 MB CSV, protecting the H1 fix.
- [ ] **M18 — Return `access_level` from the case API.** PR #7 cleanup #9 follow-up:
  `frontend/src/lib/caseAccess.ts` re-implements `resolve_case_access` client-side; the
  backend already computes the level per request. Needs a bulk access-resolution path in
  `list_cases_for_user` first to avoid introducing an N+1 (`docs/archive/PR7_REVIEW_FINDINGS.md`
  cleanup 9).
- [ ] **M19 — SSE invalidation misses histogram/anomaly panels.** PR #7 follow-up:
  `frontend/src/hooks/useCaseStream.ts`'s `INVALIDATE_PREFIXES` covers annotation/tag query
  keys but not histogram/anomaly-view keys — bulk anomaly-tagging by a teammate leaves those
  panels stale. Read the views' actual query-key names before extending the prefix list.

## Explicitly out of scope (decided during the audit)

- Persistent job store — in-memory is a documented deliberate choice for the single-process
  deployment model.
- CSRF tokens — SameSite=Lax cookies plus the LAN threat model are adequate for now.
- Alembic adoption — hand-rolled additive migration works at the current schema churn;
  revisit at v1.0.
- Proactive router/query-builder splits — churn risk outweighs payoff at current velocity.
