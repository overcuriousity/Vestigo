# TraceVector Implementation Progress

Last updated: 2026-07-03 (session 10 — closed out the last PR #8 review items #9/#11/#14/
#15/#16: fused the field_terms double scan into one window-aggregate query, unified the
column-token SQL resolution helper, extracted shared chart boilerplate primitives, moved the
chartType clamp out of an effect, and gave the Visualization field picker its own unfiltered
`GET .../viz/fields` inventory endpoint)

**Open follow-up:** none for PR #8 — every finding from its review (7 correctness bugs +
9 cleanup/design items) is resolved; see `docs/archive/PR8_REVIEW_FINDINGS.md`.

This document tracks implementation progress against the MVP defined in
[`CONCEPT.md`](./CONCEPT.md) and the tech-stack decisions in [`TECH_STACK.md`](./TECH_STACK.md).
See [`ROADMAP.md`](./ROADMAP.md) for the detailed scope breakdown and remaining work.

## Overall completion

**Estimated MVP completion: ~97 %**

Backend model, API, statistical anomaly detectors, the full frontend, and the full
auth/RBAC/teams/audit/live-collaboration layer are implemented and tested (261 backend tests,
23 frontend tests, both suites green; `ruff`/`tsc`/`oxlint` clean). What remains before MVP
closure is **offline-mode enforcement** — `allow_online` still isn't checked at most network
call sites (OIDC SSO is a deliberate, documented exception). GPU acceleration remains
aspirational (no code exists for it yet).

## MVP feature checklist

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | **Ingestion (CLI-first + web upload)** | ✅ Done | Streaming CSV/JSONL parsers; `tv ingest --source` CLI; web drag-and-drop via `POST /api/cases/{id}/sources`. |
| 2 | **Source / Timeline / Artifact model** | ✅ Done | `Source` = one file; `Timeline` = grouping; `Artifact` = per-event Plaso class. Implemented across Postgres, ClickHouse, Qdrant, API, CLI, and tests. |
| 3 | **Storage & Vector Backend** | ✅ Done | ClickHouse `events` table with `tokenbf_v1` full-text index; Qdrant collections keyed by `(case_id, embedding_config_hash)` with vector-size config-match checks. |
| 4 | **Web UI (ELK-like investigation interface)** | ✅ Done | React 19 + Vite + TypeScript. Explorer (grid, filter rail, tag facets, histogram, export, saved views, bulk actions, column picker), light/dark theme + comfortable/compact density toggles, case/timeline/source management, job tray. |
| 5 | **Anomaly & Similarity Panel** | ✅ Done | Statistical engine (`value_novelty` + `frequency` z-score detectors, self-baseline and temporal modes) replaced the earlier embedding-distance-only approach; see `db/anomaly_stats.py`. Similarity search and semantic search remain Qdrant-backed. Detector runs persist to Postgres (`detector_runs`) instead of round-tripping live event IDs through the URL. |
| 6 | **Remote embedding support** | ✅ Done | OpenAI-compatible remote embedding endpoint as an alternative to local sentence-transformers. |
| 7 | **Authentication, RBAC, teams, audit trail, live collaboration** | ✅ Done | Session-cookie auth + optional OIDC, seeded one-time bootstrap admin with centrally-enforced forced rotation, case-RBAC dependency layer, teams with member/manager roles, append-only audit trail, SSE live-collaboration stream with per-tick access re-validation. Full security review completed, all findings resolved — see `docs/archive/PR7_REVIEW_FINDINGS.md`. |
| 8 | **Deployment & Operation** | 🟡 Partial | Reference `docker-compose.yml` (podman-compatible), `uv` workflow, environment-based config. Missing: offline-mode enforcement, GPU index selection. |

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
   code. OIDC SSO (`TV_OIDC_ENABLED`) is a deliberate, documented exception — it's
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
