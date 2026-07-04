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

- [ ] **M4 — Compose network hygiene.** Reference `docker-compose.yml` publishes Postgres
  (default creds), ClickHouse (default user, no password) and Qdrant (no auth) to the host —
  app-layer RBAC is bypassable by anyone with network reach. Keep backing services on the
  compose-internal network by default; document a dev override file that exposes them.

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
- [ ] **M16 — Enricher subsystem cleanup pass (PR #54).** Lower-severity design/reuse/
  efficiency items to fold in when next touching this code — full detail and rationale in
  `docs/archive/PR54_REVIEW_FINDINGS.md` #9–#34. Resolved by the 2026-07-04
  enrichment-into-attributes redesign: #14/#27 (hydration bolt-on + per-page query — gone,
  enrichment lives in `events.attributes`), #22 (duplicated DROP PARTITION), #23 (duck-typed
  `close()`), #29 (sequential field-key query — removed). Still open:
  - GeoIP is special-cased throughout the frontend/admin instead of the enricher abstraction
    being load-bearing (hardcoded admin card, hardcoded field-key prefixes in
    `countryFlag.ts`, GeoIP-only badge logic baked into the generic Explorer cell renderer,
    asset-upload bolted onto the generic config endpoint pattern) — #9–#13.
  - Reuse: hand-rolled IPv4 regex in `privateIp.ts` (backend now uses stdlib `ipaddress` for
    validation but the regex remains as the eligibility pattern); manual
    `asyncio.create_task` + tracking set instead of `BackgroundTasks`; pagination loop
    duplicated from `EmbeddingPipeline`; temp-file upload boilerplate duplicated between
    `admin.py`/`cases.py`; orphan reconciliation duplicated from `api/main.py`'s ingest
    cleanup — #15–#20.
  - Simplification: `output_fields` duplicated as literal dict keys, "effective config"
    computed twice with diverging logic, status endpoint triggering a full availability
    sweep — #24–#26.
  - Efficiency: sequential per-source `count_events` calls; sequential (not concurrent)
    eligibility queries; `check_availability` opens the whole database just to prove it's
    readable — #28, #30, #31.
  - Minor: `isPrivateIpv6` misses some valid representations; undocumented `sorted(keys)`
    behavior change; derived-key cardinality can balloon the ColumnPicker on wide datasets —
    #32–#34.
  - New (from the redesign): staging is one Postgres row per (event, attr, output_field) —
    a row-per-event JSON-map format would shrink staging ~3x and simplify the apply join.

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
