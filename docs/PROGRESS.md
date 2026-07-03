# TraceSignal Implementation Progress

Last updated: 2026-07-03 (session 14, continued — source ingest-status lifecycle:
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
