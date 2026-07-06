# TraceSignal Implementation Progress

Last updated: 2026-07-06 (session 24 — Phase-2 batch: M22 (a)(c)(d) + M19, four commits.
(a) `_ParameterizedQueryBuilder.add_in_list`/`add_not_in_list` gained a `cast_to_string`
flag: String columns (`source_id`, `artifact`) now emit `column IN {p:Array(String)}` so
ClickHouse can use primary-index/partition pruning, which the previous unconditional
`has(..., toString(col))` form defeated; only UUID `event_id` keeps the cast (error 386
NO_COMMON_TYPE). (c) derived-range histograms collapse the serial min/max range scan +
bucket scan into one round trip: a scalar CTE computes (min, max) and the interval
server-side, `intDiv(toUnixTimestamp(ts), iv) * iv` reproduces toStartOfInterval's epoch
alignment; payload interval/min/max come from the query result, never a Python
recomputation (toUnixTimestamp truncates DateTime64(3) to seconds). Verified live against
ClickHouse 24-alpine incl. empty-match and min==max edges; explicit start/end path
unchanged. (d) `find_value_novelty`'s fields=None path no longer runs the live
`field_inventory` map scan (the ARRAY-JOIN family that OOM'd ClickHouse pre-session-23):
the novelty router branch resolves the candidate inventory from the per-source
field-stats cache (`ensure_source_field_stats` + `merged_inventory`, same template as
`/anomalies/fields`) and passes it via new optional `inventory`/`inventory_total` params;
canonical-mapping aggregates stay live, surprise-score denominator unchanged. (M19)
`useCaseStream`'s `INVALIDATE_PREFIXES` now covers histogram, anomalies-novelty/-frequency,
and the viz-modal keys (field-histogram/-total, field-terms) so a teammate's bulk
anomaly-tagging refreshes those panels over SSE; predicate extracted as exported
`shouldInvalidate` with new vitest coverage. Also fixed dead local invalidations:
BulkActionBar/useAnnotationMutations invalidated `["anomalies", caseId]` which matches no
query (real prefixes are anomalies-novelty/-frequency), and the anomaly views' tag
mutations only invalidated `annotations` — even the analyst's own bulk actions left
panels stale. Note: 4 pre-existing test failures on this machine
(test_embeddings_capability ×2, test_rbac_api, test_uploads) fail on a clean tree too —
environment-dependent, unrelated to this batch.)

Previous (session 23 — large-source performance + value-novelty OOM fix.
Root cause of the unresponsive Explorer on a 5.5 GiB CloudTrail ingest: wide flattened
sources store every unioned column on every event, so each of 2.8M events carried 672
attribute map entries of which ~639 were empty strings — 73 GiB uncompressed in the
`attributes` column. Every map-scanning query paid for it: broad text search 3.3s/scan
(×4 scans per filter interaction), and the value-novelty field inventory's
`ARRAY JOIN mapKeys` + `attributes[key]` re-lookup exploded to ~2 billion rows and
OOM-killed ClickHouse at 56 GiB (the 500s on GET /anomalies). Fixes: (1)
`Event.to_clickhouse_row` drops empty attribute values at ingest — semantically
transparent since a ClickHouse Map returns '' for absent keys; (2) the inventory query
uses a paired keys/values ARRAY JOIN, pre-filters `val != ''`, approximate `uniq()`
instead of `uniqExact`, and external-GROUP-BY spill + a 12 GB query memory cap; (3)
`EventQueryService.query` runs the first-page COUNT and page fetch concurrently; (4)
`ClickHouseStore.init_schema` is cached per instance (was 3 DDL round-trips on every
query). Existing data cleaned via a one-off `ALTER TABLE events UPDATE attributes =
mapFilter((k,v) -> v != '', attributes)` mutation on the running deployment — attributes
went 73.3→5.05 GiB uncompressed, broad search 3.3s→0.37s, field inventory OOM→0.87s,
match counts verified identical. Also fixed the stranded infinite scroll: EventGrid's
load-more only fired from the onScroll handler and was gated on `!isFetching`, so
reaching the bottom while a page fetch was in flight skipped it — with the scrollbar
already pinned, no further scroll event ever came ("scrolled to bottom, nothing
happens"). A virtualizer-driven effect now re-checks when a fetch settles and keeps
loading while the tail rows are in view.)

Previous (session 22 — onboarding tour. First-login guided overlay walking
the core workflow in 11 action-driven steps: create case → open it → upload dialog →
converter-script hint → upload → default "All sources" timeline → Explorer column picker →
open event details → filter in/out buttons → Visualize link → done. Custom spotlight
implementation (no tour library): `frontend/src/lib/tourSteps.ts` (step schema:
route-gated, `[data-tour]` selector anchors, advance = manual Next | app event | route
change), `stores/tour.ts` (non-persisted state machine + `tourEvent()` fire-and-forget
helper), `components/tour/TourOverlay.tsx` (box-shadow spotlight, `pointer-events: none` so
the highlighted control stays clickable; card needs explicit `pointer-events: auto` +
pointerdown stopPropagation because an open Radix modal Dialog sets `pointer-events: none`
on body and dismisses on outside-pointerdown) and `TourProvider.tsx` (auto-start, completion
PATCH; uses `qc.setQueryData` instead of invalidate — an invalidate lets `useCurrentUser`
re-sync the stale cached user mid-refetch and instantly restart the tour). Persistence is a
new server-side `users.onboarding_completed` bool (guarded ALTER migration, `to_dict`,
`update_user`, PATCH /me) — refresh mid-tour restarts from step 1 by design; existing users
backfill to false and see the (always skippable) tour once. Settings page gained a "Restart
onboarding tour" section. Verified end-to-end with a headless-Playwright drive of all 11
steps including a real CSV ingest, finish/skip persistence across reloads, and the
settings restart path. Bug fixes on the way: UploadDialog kept a stale duplicate-warning /
error across close/reopen (missing `mutation.reset()`); Settings audit-trail download had
no error handling (silent unhandled rejection).)

Previous (session 21 — M21 storage redundancy cleanup, all three items from
the 2026-07-05 storage placement audit. (1) `Event.vector_id` removed everywhere (dataclass,
ClickHouse DDL/column lists/SELECTs, `_columns.py`, API/frontend event shape) — it was
unconditionally `str(event_id)`; Qdrant point IDs now use `event_id` directly. Existing
ClickHouse tables keep the orphaned column harmlessly (CREATE IF NOT EXISTS; inserts name
columns explicitly). (2) Dead `Source.embedding_model`/`Source.embedding_config` Postgres
columns deleted (never written; live config is Timeline-scoped) — also removed the orphaned
`_run_embedding_job` in `cases.py` (zero callers, vestigial source-level embed path from the
same era) and reworked `MethodologyPanel.tsx` to read the Timeline's embedding model/config
(it previously read the always-null Source fields, so it always showed the fallback text).
(3) Qdrant payload trimmed from a full row mirror to filter-relevant fields only
(`case_id`, `source_id`, `artifact`, `timestamp`) in both `Event.to_qdrant_payload` and the
embed pipeline's `_qdrant_payload`; full event detail resolves post-search via the existing
ClickHouse `get_events_by_ids` hydration. `tags` dropped from the payload (nothing filtered
on it natively; annotation tags mutate after embed and would silently go stale). Existing
collections keep fat payloads until re-embedded — payload shape is not part of
`EmbeddingConfig.config_hash`, so no identity change.)

Previous (session 20 — PR #65 review fixes. `Source.created_by` for CLI
ingests now stores `resolved_user.id` instead of `resolved_user.username`, matching every web
call site (`api/routers/cases.py`) — the mismatch would have silently broken any future
id-based creator lookup. `tsig embed` gained the `--user` attribution + `cli.embed.source`
audit-log row it was missing (PROGRESS.md previously claimed embed got "the same validation for
consistency" — it hadn't gotten audit parity). `tsig ingest`'s pre-scan `total_size` walk
(a second full directory `rglob`/`stat` pass, redundant with `IngestionPipeline`'s own byte
count and racy against directories that change between the two scans) is removed; the banner
no longer prints a size, the progress box reports it once ingestion starts. `tsig ingest`'s
three separate `asyncio.run()` calls collapsed into one. `SimilarityService.find_similar_by_text`
(`db/similarity.py`) now catches encoder failures and raises `EncoderUnavailableError`, mapped
to a 503 in `api/routers/events.py::semantic_search_events` — previously a flaky remote
encoder crashed semantic search with an unhandled 500, the exact failure mode the sibling
`_guard_encoder` fix (session 19) addressed only for the field wizard. Frontend `fmtDuration`
(`JobTray.tsx`) fixed to include seconds in its hour branch, matching `cli/progress.py`'s
`_fmt_duration` — they'd drifted, so web ETAs over 1h read differently from the CLI's.

Previous (session 19 — CLI ingestion promoted to a real feature. `tsig ingest
--case` previously accepted a case *name* and passed it straight through as the case ID with
no validation (`get_case` was never called), silently writing Sources against a
possibly-nonexistent case; it also never set `Source.created_by` and printed nothing during
multi-hour large-file runs. Now: new `tsig cases list` (unscoped, admin/CLI use — resolves
`owner_id`/`team_id` to usernames/team names via `list_users`/`list_teams`); `tsig ingest`
validates `--case` via `store.get_case()` before touching the file and rejects unknown IDs;
adds optional `--user` attribution (defaults to the sole active admin if unambiguous, else
errors) written to `Source.created_by` plus a `cli.ingest.source` audit-log row
(`record_audit`); and a new `src/tracesignal/cli/progress.py` ported near-verbatim from
ScalarForensic (`_ETATracker` Kalman throughput/ETA estimator, block-element progress bar,
duration formatter) driven by bytes off the existing `IngestionPipeline.progress_callback`
(same signal the web upload job already uses — no new plumbing in `pipeline.py`). New
`tests/test_cli.py` (11 tests: case listing, case/user validation, Kalman tracker math).
`tsig embed` also gained the same case-ID validation for consistency.)

Previous (session 18 — Milestone 2 batch, PR 7/7: M16b ColumnPicker
derived-key grouping (PR #54 finding #34). New `splitDerivedKey` in
`frontend/src/lib/enrichment.ts` (last-separator split, keeps the key contract mirrored
in one file). ColumnPicker's Dynamic fields group now collapses enrichment-derived keys
(`src_ip:geo_country`) under their parent attribute as a collapsed-by-default
"Derived (N)" disclosure, children labeled by output-field suffix; derived keys whose
parent isn't in the field list land in a trailing "Derived fields" group; an active
search auto-expands matching children (never hides a selectable field). Checkbox ids
stay the full raw key — selection persistence and the grid untouched. Frontend-only.
New vitest coverage: `columnPicker.test.tsx` (grouping, expansion, orphans,
search-expansion, raw-key selection) + `splitDerivedKey` unit tests.)

Previous (session 18 — Milestone 2 batch, PR 6/7: M16a staging-format
redesign. `EnrichmentResultStaging` regrained from row-per-(event, attr, output_field)
to row-per-(job, event) with a `fields` JSON map (`field_key -> value`, keys already
attr-prefixed) — ~3-6x fewer staging rows for multi-output enrichers, unique index now
`(job_id, event_id)`. `_process_batch` accumulates one map per event (empty maps skipped);
apply loop pages 4000 rows (was 10000 per-field rows) and expands maps back into triples
for `apply_enrichments` — no ClickHouse-side change. **Destructive migration**:
`init_schema` drops a legacy staging table (recognized by its `field_key` column) before
`create_all`; orphaned pre-upgrade staged rows are discarded, matching the pre-release
stance; the old `enricher_config_hash` ADD COLUMN block is gone. Dead helpers
`pop_staged_rows_for_job`/`delete_staged_rows` replaced by read-only
`list_staged_rows_for_job`. New tests: migration drop+recreate (idempotent), one-row-per-
event `_process_batch` grain.)

Previous (session 18 — Milestone 2 batch, PR 5/7: M15 per-source
field-stats cache. New `db/field_stats.py` + Postgres `source_field_stats` (versioned
JSON payload: top-level cols + attribute keys with distinct/coverage/3 samples; version
mismatch = cache miss, no migrations). Computed per source in 2 ClickHouse queries at:
ingest completion (isolated, never fails the ingest) and after every enrichment apply
(the only attributes mutation path; on refresh failure the stale row is dropped so reads
recompute). Read path is compute-on-read + store — pre-existing DBs self-heal. Converted
call sites: `list_fields` (ColumnPicker, timeline wizard, mapping validation),
`field_coverage` (timeline wizard — counts now exact instead of 20k-row samples;
`sampled_rows_per_source` removed from response + frontend type), `field_inventory`
(Visualize field picker, novelty recommender — `recommend_novelty_fields` accepts a
pre-merged inventory; canonical field-mapping coalesce aggregates stay live via new
`canonical_inventory`, since per-source counts can't dedupe multi-raw-key events). Merge
math: coverage sums exactly, distinct = max-across-sources (documented approximation).
Deliberately not converted: embedding wizard's `list_fields_by_artifact` (cost is the
cohesion value-sampling, not inventory). `delete_source` drops the cache row. New
`tests/test_field_stats.py`: live-ClickHouse parity vs the old scans, self-heal,
version-mismatch recompute, derived keys visible after `apply_enrichments`.)

Previous (session 18 — Milestone 2 batch, PR 4/7: CI container smoke test.
New `container-smoke` job: builds the reference image, boots it with `--network host`
against the same pg/clickhouse(glibc)/qdrant service containers the backend job uses,
asserts `/api/health` returns `status:"ok"` (would have caught C1's broken CMD import)
and that `/` serves the packaged frontend HTML; dumps container logs on failure.
Dockerfile gains `ARG INSTALL_EMBEDDINGS` (default 0) so the image skips the ~2 GB local
embedding stack once M5's `embeddings` extra lands — the smoke test then doubles as the
"boots without the extra" regression test.)

Previous (session 18 — Milestone 2 batch, PR 3/7: M17 job authz via case
RBAC. `Job` gains `case_id` (in `to_dict()` too), threaded through every
`job_store.create` site (ingest, embed, manual + automatic enrich, startup re-runs —
`run.case_id`). `GET /api/jobs/{id}`: creator/admin unchanged; otherwise READ access on
the job's case grants visibility (`resolve_case_access`), so case members can poll each
other's jobs and system jobs (`created_by=None`) become member-visible instead of
admin-only. Non-members still get 404 (no existence probing). Case-less jobs keep
owner-or-admin semantics. New `tests/test_jobs_api.py` covers the four quadrants.)

Previous (session 18 — Milestone 2 batch, PR 2/7: M5 dependency diet.
Removed never-imported `torchvision`/`onnxruntime`/`jinja2`/`alembic`; `torch` +
`sentence-transformers` moved to an optional `embeddings` extra
(`uv sync --extra embeddings`) — base install drops ~2 GB. Sole ML import
(`models/embeddings.py`) is now lazy inside `load()` with an actionable RuntimeError;
new `embeddings_available()` (importability OR `TS_EMBEDDING_API_BASE_URL` — remote mode
needs no torch) surfaces as `embeddings_available` on `/api/health` and gates embed-start
and semantic-search with a request-time 503 instead of a job that dies on ImportError.
Field-recommend already degraded gracefully. README quick-start/airgapped docs updated.)

Previous (session 18 — Milestone 2 batch, PR 1/7: ingest throughput.
`TS_INGEST_BATCH_SIZE` (default 20k) replaces the accidental reuse of
`embedding_batch_size` (64) as the ClickHouse insert batch in `IngestionPipeline` —
one HTTP insert per 20k rows instead of per 64, the dominant fix for the 100 GiB-over-LAN
ingest goal. CLI `--batch-size` falls through to the setting; enricher read paging
bumped to ≥1000; 413 upload rejection names `TS_MAX_UPLOAD_BYTES` and points at
`tsig ingest` for huge files; deferred native-protocol/async_insert options recorded
as ROADMAP M20. Remaining Milestone 2 PRs planned: M5 dependency diet, M17 job RBAC,
CI container smoke test, M15 field-stats precompute, M16 staging redesign +
ColumnPicker grouping.)

Previous (session 17 — final PR #54 cleanup batch, M16 bulk. Four commits on
`feat/enricher-subsystem`: **(1) micro-fixes** — GeoIP output-field names single-sourced
(order locked, config_hash-stable), `refresh_availability(key)` single-enricher form,
batched `count_events(source_ids=...)`, concurrent eligibility checks via `asyncio.gather`,
sidecar-first `check_availability` (no full `.mmdb` mmap when `.meta.json` carries
`database_type`), plus comments documenting: eligibility-regex role (#15), create_task-over-
BackgroundTasks rationale (#17/#21), deliberate reconcile divergence (#20 won't-fix), sorted
`list_fields` attributes (#33). **(2) shared abstractions** —
`ClickHouseStore.iter_source_events` batching generator (embedding pipeline + enricher jobs),
`api/uploads.py::receive_upload_to_tmp` (temp-file + hash + 413 handling, used by source and
asset uploads), `enrichers/base.py::effective_enricher_state` (single "explicit overrides
admin default" rule for `list_timeline_enrichers` and
`list_automatic_enrichers_for_source`). **(3) generic asset abstraction** — Enricher ABC
gains `asset_spec`/`asset_status()`/`install_asset()` + `AssetValidationError`; GeoIP
implements them (City-flavor validation moved out of admin.py; lazy db-path resolution);
GET/POST `/admin/enrichers/geoip/database` replaced by asset state folded into
`GET /admin/enrichers/config` + generic `POST /admin/enrichers/{key}/asset`; audit action now
`admin.enricher_asset_upload`; field-key contract extracted to
`base.FIELD_KEY_SEPARATOR`/`derived_field_key`. **(4) frontend** — new `lib/enrichment.ts`
(key contract mirror + `hasEnrichmentSiblings` + decorator registry), Explorer flag and
private/public badge now data-gated on enrichment siblings (user decision: badge means "was
enriched", so un-enriched private IPs show nothing), `AdminEnrichersPage` fully generic
(maps configs, asset section from `config.asset`), `privateIp.ts` IPv6 parsed to hextets
(zone suffixes, `::`, embedded IPv4; bitmask range checks; fixes uncompressed loopback and
`FEBF::` misclassification). Deferred to fresh branch: staging-format redesign + #34
(ColumnPicker cardinality) — roadmap M16 rewritten accordingly. 450 backend + 164 frontend
tests passing.)

Previous (session 16 — roadmap hardening batch M1–M4, M7, M8, shipped on
the enricher PR branch. **M1**: evidence-mutation failures now surface — `delete_source_events`
re-raises (only a missing `events` table stays a benign no-op), `delete_timeline_events`
aggregates per-source failures, DELETE source/case endpoints fail closed with 502 +
`source.delete_failed`/`case.delete_failed` audit rows and keep the Postgres row (the
authoritative evidence record) so the delete stays visible and retryable; ingest rollback is
still best-effort but logs each failed step and flags `cleanup incomplete` on the job error.
**M2**: one SQL escaping regime — `count_events` on `{name:String}` binds (numbered params
for the IN-list, empty list short-circuits), partition expressions built via a shared
validated `_partition_expr` (fail-closed charset guard mirroring `generate_id`'s contract,
Unicode `isalnum` + `-`/`_`). **M3**: in-memory exponential login backoff per
(username, client IP) — 429 + `Retry-After` after `TS_LOGIN_BACKOFF_THRESHOLD` (5) failures,
`base*2^(n-threshold)` capped at `TS_LOGIN_BACKOFF_MAX_SECONDS`; identical behavior for
unknown user vs. wrong password (no existence leak, tested); `auth.login_rate_limited`
audit action. **M4**: compose publishes Postgres/ClickHouse/Qdrant on `127.0.0.1` only
(loopback binds instead of the roadmap's internal-network+override idea — the native
`uv run tsig-web` dev workflow depends on localhost ports); README compose section un-staled
(app service is opt-in/commented). **M7**: JobStore caps retained terminal jobs at 200,
evicting oldest-finished first, never queued/running; mutations now behind a real lock.
**M8**: dead `secret_key` setting deleted everywhere. Roadmap also gained M17–M19 (PR #7
follow-ups rescued from the archive: job authz via case RBAC, `access_level` from the case
API, SSE invalidation misses histogram/anomaly panels). 438 tests passing.)

Previous (session 15, continued — enrichment persisted into
`events.attributes` (user decision: the ClickHouse events table is a normalized derivative
of the hashed, immutable source files, so dataset mutation is the better design): the
separate `event_enrichments` table, its read-time `_hydrate_enrichments` join, and the
`list_fields` "enrichments" response key are gone — **destructive**: `init_schema` now
`DROP TABLE IF EXISTS event_enrichments` (pre-release DBs deprecated; derived data,
re-running the enricher regenerates it). New write path: results stage in Postgres as
before, then one atomic per-source partition rewrite at job end
(`ClickHouseStore.apply_enrichments`: scratch triples table → `mapUpdate` LEFT JOIN copy of
the `(case_id, source_id)` partition → `REPLACE PARTITION`; idempotent, per-(case,source)
apply lock, scratch tables swept at startup; smoke-tested against live CH 24 — counts
stable, originals untouched, re-apply idempotent). Periodic flush +
`enrichment_flush_batch_count` removed (apply-once). Per-row `enricher_config_hash`
replaced by per-source Postgres provenance (`source_enrichments` upsert, audit
`enricher.applied`). Derived-field naming contract now `<attr_key>:<output_field>`
(`src_ip:geo_country`; GeoIP output fields renamed geo_country/geo_city/geo_country_code)
— sorts beside its source column and is filterable/exportable/visible in every read path
for free since it's a real attribute key. Frontend: `countryFlag.ts` reads the new sibling
keys, dead "Enrichments" ColumnPicker group removed, `FieldsResponse.enrichments` dropped,
EventDetailPanel long field labels now wrap (`break-all`) instead of overlapping values.
Immutability language reframed across `clickhouse.py`/`enrichers/*`/`field_mappings.py`/
`MODEL_REFINEMENT.md`: immutable = original evidence file + provenance hash columns, not
the derived attributes map.)

Previous (session 15 — enricher hardening, roadmap M9–M13 from the PR #54
review: per-run enricher instances via `Enricher.spawn()` (registry singleton now
metadata/availability-only; the shared-`_reader` close race is gone) with an in-memory
`(timeline_id, enricher_key)` run guard — manual "Run now" returns 409 with the conflicting
job id, auto-trigger skips with a log; GeoIP `enrich_value` validates input with stdlib
`ipaddress` and only swallows `AddressNotFoundError` — reader failures now fail the job
loudly (context note, no raw values) and a failed-but-alive job flushes+clears its own
marker; `enricher_config_hash` populated end-to-end (new `Enricher.config_hash()` mirroring
`ParserConfig`, GeoIP hashes db sha256+build_epoch from a `.meta.json` sidecar written at
upload — the upload's `copy_and_hash` digest is now captured — with a hash-and-persist
fallback for pre-sidecar installs; staging table gained the column via additive migration);
upload validation rejects non-City `.mmdb` flavors with an actionable 400 and
`check_availability` checks flavor too; startup reconciliation now *flushes* orphaned staged
rows to ClickHouse (audit `enricher.job_recovered`) and auto-schedules a re-run over the
timeline's current ready sources after availability refresh (argMax read-dedup makes the
overlap safe; ClickHouse-down leaves marker+rows for the next restart);
`EnrichersDialog.tsx` toggle/mode lost-update race fixed with the standard TanStack
optimistic-update pattern (`onMutate` cache patch, rollback on error, invalidate only when
last mutation settles))

Previous (session 14, continued — source ingest-status lifecycle:
`Source.status` (`ingesting`/`ready`, additive migration backfills `ready`); uploads create
the row as `ingesting` and the background job flips it to `ready`; `_resolve_timeline_scope`
(the single scope choke point) excludes non-ready sources so the explorer, histogram,
export, detectors, and wizards never see half-ingested data; timeline embedding refuses
409 while a member source is ingesting; field-mapping validation runs inventory checks only
against ready sources (structural rules always apply — `validate_field_mappings` now takes
`None` inventory to mean "unknown, skip inventory checks"); startup reconciliation removes
sources orphaned mid-ingest by a restart (partial events + row, audited as
`source.ingest_interrupted`) so re-upload isn't blocked by the file-hash duplicate check;
frontend shows an "Ingesting" badge in the source list and an Explorer banner with
poll-until-ready + auto-refetch when the source becomes visible)

Previous (session 14 — full repository audit; fixed all Critical/High
findings on `fix/audit-critical-high`: Dockerfile CMD now uses `--factory
tracesignal.api.main:create_app` (the shipped image previously pointed at a nonexistent
`app` attribute and could not start); CSV parser streams instead of `list(fh)`-ing the whole
file (incremental byte-offset/line tracking in `_RecordTrackingIterator`); `tsig-web` builds
the frontend only when `dist/` is missing (`TS_FRONTEND_REBUILD=1` forces) and enables the
uvicorn reloader only in development; embedding model load enforces `HF_HUB_OFFLINE` unless
`TS_ALLOW_ONLINE` and fails with an actionable message; all remaining blocking
`EventQueryService` calls in async handlers threadpool-wrapped; uploads single-pass
copy+hash off the event loop with a `TS_MAX_UPLOAD_BYTES` cap (413 mid-stream). Remaining
Medium/Low findings consolidated into a new phase-2 `docs/ROADMAP.md`; the fully-shipped
phase-1 roadmap archived to `docs/archive/ROADMAP_PHASE1.md`; CLAUDE.md frontend-build note
un-drifted)

Previous (session 13 — deployment: `docker-compose.yml` gained an `app` service
that builds/runs TraceSignal itself via a new `Dockerfile`, after the backing services;
`tsig-web` now always rebuilds the frontend on startup instead of skipping when `dist/` exists;
README documents the airgapped install path (build on an online machine, carry `.venv/` +
`frontend/dist/` over on a portable drive, backing services out of scope); archived
`docs/PLAN_ISSUES_5_10_11.md` to `docs/archive/` now that issues #5/#10/#11 are all shipped;
fixed a stale test asserting the old `text/x-python` converter content-type)

Previous (session 12 — issue #10: timeline creation wizard with query-time
field aggregation (`Timeline.field_mappings` metadata, coalesce resolution in
`db/field_mappings.py` threaded through filters/histogram/viz/export/detectors, field
discovery surfaces canonical names with provenance, `PATCH .../field-mappings` + audit,
`GET /cases/{id}/fields/coverage`, 4-step wizard with name+value-shape merge suggestions);
issue #5: full rename TraceVector → TraceSignal
(`tsig`/`tsig-web` CLI, `TS_` env prefix, hard cutover, `docs/MIGRATION_RENAME.md`); issue #11:
vendored self-contained 2timesketch converter scripts (`scripts/vendor_converters.py` →
`src/tracesignal/assets/converters/` + manifest), `GET /api/converters[/{name}]` download
endpoints, converter panel + static LLM-converter prompt in the upload dialog, and subtle
collapsible `GuidancePanel` guidance on the cases page and case overview)

Previous (session 11 — visualization v2: two-layer comparison with
server-enforced shared-grid invariants (`POST .../viz/compare`, kinds time/terms/numeric),
derived metrics as pure client-side transforms (Δ / rate / % of baseline / cumulative, nulls
for undefined bins), first-class time-histogram chart type, bar orientation + grouped compare
bars, numeric-histogram comparison overlay, per-chart options panel, unified on-screen/export
captions with truthfulness warnings, five task presets, saved charts (`SavedChart` Postgres
model + CRUD), URL-serialized `ChartConfig` (`c_*` params), and the Explorer histogram
tooltip anchor/clamping fix)

**Open follow-up:** none for PR #8 — every finding from its review (7 correctness bugs +
9 cleanup/design items) is resolved; see `docs/archive/PR8_REVIEW_FINDINGS.md`.

This document tracks implementation progress against the MVP defined in
[`CONCEPT.md`](./CONCEPT.md) and the tech-stack decisions in [`TECH_STACK.md`](./TECH_STACK.md).
See [`ROADMAP.md`](./ROADMAP.md) for the detailed scope breakdown and remaining work.

## Overall completion

**Estimated MVP completion: ~97 %**

Backend model, API, statistical anomaly detectors, the full frontend, and the full
auth/RBAC/teams/audit/live-collaboration layer are implemented and tested (341 backend tests,
118 frontend tests, both suites green; `ruff`/`tsc`/`oxlint` clean). What remains before MVP
closure is **offline-mode enforcement** — `allow_online` still isn't checked at most network
call sites (OIDC SSO is a deliberate, documented exception). GPU acceleration remains
aspirational (no code exists for it yet).

## MVP feature checklist

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | **Ingestion (CLI-first + web upload)** | ✅ Done | Streaming CSV/JSONL parsers; `tsig ingest --source` CLI; web drag-and-drop via `POST /api/cases/{id}/sources`. |
| 2 | **Source / Timeline / Artifact model** | ✅ Done | `Source` = one file; `Timeline` = grouping; `Artifact` = per-event Plaso class. Implemented across Postgres, ClickHouse, Qdrant, API, CLI, and tests. |
| 3 | **Storage & Vector Backend** | ✅ Done | ClickHouse `events` table with `tokenbf_v1` full-text index; Qdrant collections keyed by `(case_id, embedding_config_hash)` with vector-size config-match checks. |
| 4 | **Web UI (ELK-like investigation interface)** | ✅ Done | React 19 + Vite + TypeScript. Explorer (grid, filter rail, tag facets, histogram, export, saved views, bulk actions, column picker), light/dark theme + comfortable/compact density toggles, case/timeline/source management, job tray. |
| 5 | **Anomaly & Similarity Panel** | ✅ Done | Statistical engine (`value_novelty` + `frequency` z-score detectors, self-baseline and temporal modes) replaced the earlier embedding-distance-only approach; see `db/anomaly_stats.py`. Similarity search and semantic search remain Qdrant-backed. Detector runs persist to Postgres (`detector_runs`) instead of round-tripping live event IDs through the URL. |
| 6 | **Remote embedding support** | ✅ Done | OpenAI-compatible remote embedding endpoint as an alternative to local sentence-transformers. |
| 7 | **Authentication, RBAC, teams, audit trail, live collaboration** | ✅ Done | Session-cookie auth + optional OIDC, seeded one-time bootstrap admin with centrally-enforced forced rotation, case-RBAC dependency layer, teams with member/manager roles, append-only audit trail, SSE live-collaboration stream with per-tick access re-validation. Full security review completed, all findings resolved — see `docs/archive/PR7_REVIEW_FINDINGS.md`. |
| 8 | **Deployment & Operation** | 🟡 Partial | Reference `docker-compose.yml` (podman-compatible) builds and runs the app itself alongside the backing services; native `uv`/`tsig-web` workflow (always rebuilds the frontend, no stale-`dist` check); documented airgapped install path (README). Missing: offline-mode enforcement, GPU index selection. |

## Completed architectural decisions

- ✅ Language & packaging: Python 3.13 + `uv`
- ✅ Web backend: FastAPI + Uvicorn
- ✅ CLI ingestion: Typer
- ✅ Frontend: React 19 + Vite 8 + TypeScript, Zustand + TanStack Query/Table/Virtual
- ✅ Metadata store: PostgreSQL (async SQLAlchemy)
- ✅ Event store: ClickHouse
- ✅ Vector store: Qdrant (tested with v1.18.2)
- ✅ Embedding runtime: sentence-transformers (`all-MiniLM-L6-v2` baseline), plus an
  OpenAI-compatible remote endpoint option
- ✅ Data model: Case / Source / Timeline / Artifact (see `MODEL_REFINEMENT.md`)
- ✅ Auth backend: session-cookie auth for local users + optional OIDC SSO (see `TECH_STACK.md`
  §8)

## Known gaps / next logical steps

1. **Offline-mode enforcement** — `allow_online` is a config flag
   (`core/config.py`) that is read but never checked at most network call sites.
   Airgapped-by-default is a stated design goal (`CLAUDE.md`) that isn't fully enforced in
   code. OIDC SSO (`TS_OIDC_ENABLED`) is a deliberate, documented exception — it's
   operator-opted-in and independent of `allow_online` (see `TECH_STACK.md` §6).
2. **GPU acceleration** — no ROCm/CUDA-specific code paths exist anywhere in the codebase; this
   is still purely aspirational, unlike the other "TBD" items which have concrete partial work.
3. **Authentication, RBAC, teams, audit trail, live collaboration** — ✅ implemented
   (2026-07-02) and hardened through a full security review; all findings resolved — see
   `docs/archive/PR7_REVIEW_FINDINGS.md`. Remaining deliberately-descoped item from that
   review: `Job` has no `case_id`, so job-status polling is still authorized by creator
   identity rather than `resolve_case_access` (a teammate can't poll a shared case's embed
   job started by someone else) — flagged as a real follow-up, not done here.
4. **C13 tag push-down / C18 persisted detector runs** — ✅ both implemented (2026-07-02); see
   `db/queries.py` (`TagFilter`, `add_tag_filter`) and `db/postgres.py` (`DetectorRun`,
   `create_detector_run`/`get_detector_run`).
