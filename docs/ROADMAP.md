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

- [ ] **M5 — Dependency diet.** `torchvision`, `onnxruntime`, `jinja2` are declared but never
  imported; `alembic` is unused (migrations are hand-rolled additive ALTERs in
  `postgres.py::init_schema`). Remove them. Then consider moving `torch`/
  `sentence-transformers` to an optional `embeddings` extra with graceful capability
  degradation (health endpoint flag, clear error on embed endpoints) so the base install
  drops ~2 GB.
- [ ] **Container smoke test in CI.** Build the image, `docker compose up`, curl
  `/api/health`. Would have caught C1 before it shipped.
- [ ] **M15 — Precompute per-source field stats at ingest time.** Four call sites do a live
  full-scan ClickHouse aggregation over `events` on every read — `db/anomaly_stats.py`'s
  `field_inventory` (backs both the Visualize page's field dropdown and the anomaly wizard's
  field recommender), `db/queries.py::list_fields` (Explorer ColumnPicker),
  `db/queries.py::field_coverage` (timeline-creation wizard, scans up to 20k rows/source with
  sample values every time the wizard opens), and `db/queries.py::list_fields_by_artifact`.
  Since sources are immutable once ingested, none of this needs to be live: compute once per
  source right after ingestion (same trigger point `_trigger_automatic_enrichments` uses),
  cache in Postgres keyed by `source_id`, and merge cheaply per timeline. `coverage` merges
  exactly via addition; exact `distinct` needs a sketch (HyperLogLog) or a cheap approximation
  (e.g. max-across-sources) since it only feeds a UI hint. Short-term mitigation already
  shipped: `VisualizePage` shows a "can take a while" hint under the spinner and the field
  dropdown scrolls instead of overflowing (`ui/Select.tsx`).
- [ ] **M16 — Enricher follow-ups (fresh branch after PR #54 merges).** The 2026-07-04
  cleanup batch on `feat/enricher-subsystem` resolved the bulk of the PR #54 review residue
  (#9–#13 generic asset abstraction + de-GeoIP'd frontend, #15–#19 reuse, #24–#26
  simplification, #28/#30/#31 efficiency, #32/#33 minors; #20 documented won't-fix). Full
  finding set + status in `docs/archive/PR54_REVIEW_FINDINGS.md`. Deliberately deferred:
  - Staging-format redesign: staging is one Postgres row per (event, attr, output_field) —
    a row-per-event JSON-map format would shrink staging ~3x and simplify the apply join.
  - #34: derived-key cardinality can balloon the ColumnPicker on wide/vendor-inconsistent
    datasets (`src_ip:geo_country`, `source_ip:geo_country`, ...) — needs a grouping/limit
    design in the ColumnPicker.

- [ ] **M20 — Ingest-throughput follow-ups (only if needed).** `TS_INGEST_BATCH_SIZE`
  (default 20k, one HTTP insert per batch) should carry a 100 GiB ingest fine (~5k inserts
  for ~100M rows). Revisit only if measured insufficient: ClickHouse native protocol
  (clickhouse-driver, port 9000), `async_insert`, parse/insert pipelining (parser thread
  feeding an insert thread).
- [ ] **M17 — Job authorization via case RBAC.** PR #7 review #9 follow-up (guard itself was
  fixed): jobs are only guarded by `created_by == user.id or is_admin`; a `Job.case_id` +
  `resolve_case_access` check would let case members see each other's jobs and align job
  visibility with the rest of the RBAC model. Flagged in `docs/PROGRESS.md` at the time;
  full context in `docs/archive/PR7_REVIEW_FINDINGS.md` #9.

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
