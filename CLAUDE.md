# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Vestigo is a local-first, forensic-grade log investigation platform for small security
teams. It ingests Timesketch-compatible timelines (Plaso CSV/JSONL, generic CSV/JSONL) at
scale, lets analysts explore events through an ELK-like web UI, and detects anomalies by
embedding log lines into a vector database and by running statistical detectors directly
over ClickHouse.

Backend: Python 3.13+, FastAPI/Uvicorn, native `uv` app talking to three **external** services
— PostgreSQL (metadata), ClickHouse (events), Qdrant (vectors). None of these run inside the
app; `docker-compose.yml` is only a reference/dev deployment for them.
Frontend: React 19 + Vite 8 + TypeScript, in `frontend/`.

Read `docs/CONCEPT.md` and `docs/MODEL_REFINEMENT.md` before touching the data model
(Case/Source/Timeline/Event/Artifact) — the vocabulary is deliberate and recently refactored.
`docs/TECH_STACK.md` records *why* each backing service was chosen; every choice in it is
resolved. Auth is session-cookie based with optional OIDC, case-RBAC, teams and an audit
trail (`api/routers/auth.py`, `admin.py`, `deps.py`).

### `docs/` map

- `CONCEPT.md` / `MODEL_REFINEMENT.md` — product vision and the Case/Source/Timeline/Event/
  Artifact data model. Read before touching the model; rarely changes.
- `TECH_STACK.md` — backing-service decision record (*why*, not *what's shipped*).
- `ANOMALY_DETECTION.md` — reference for all fourteen analysis tools actually running
  (statistical detectors, Sigma runner, log templates, semantic similarity), plus the
  baseline/disposition model. Update alongside any detector change in the same commit.
- `AGENT.md` — the optional AI investigation agent (design invariants, MCP tools, provider
  config incl. Kimi coding plan). Update alongside any `src/vestigo/agent/` change.
- `STORIES.md` — the Stories subsystem (block model, collaboration and export semantics,
  snapshot format, agent parity). Update alongside any `src/vestigo/stories/` change.
- `DEPLOYMENT.md` — operator guide: reference compose stack, containerized app, airgapped
  install, TLS reverse proxy (`nginx-tls.conf`), stability/upgrade guarantees.
- `INPUT_FORMATS.md` — CSV/JSONL/Parquet field-level normalization spec for ingestion.
- `ROADMAP.md` — **the only open backlog.** Condensed, checkbox-per-item, current phase only.
  Keep it up to date as items land; delete items once fixed rather than marking them done and
  leaving them.
- `PROGRESS.md` — append-only chronological session log ("what changed and why"), newest entry
  on top. Not a plan — don't add TODOs here, that's `ROADMAP.md`'s job. Old sessions are
  periodically split out to `archive/PROGRESS_SESSIONS_*.md` to keep the live file readable.
- `superpowers/specs/` + `superpowers/plans/` — dated per-feature design rounds and the
  execution plans that followed them (`YYYY-MM-DD-<slug>.md`). Point-in-time records of *why a
  shape was chosen*, referenced from `ROADMAP.md`/`PROGRESS.md`; the shipped behavior lives in
  the reference docs above, so update those rather than editing an old spec.
- `archive/` — completed roadmap phases (`ROADMAP_PHASE{N}.md`) and point-in-time PR review
  findings (`PR{N}_REVIEW_FINDINGS.md`, one file per reviewed PR, full unrestricted finding
  set). Write a new `PR{N}_REVIEW_FINDINGS.md` when a review surfaces more than can fit as a
  couple of `ROADMAP.md` lines; `ROADMAP.md` should then hold only condensed items that point
  back into it, not the full text.

Don't let ad hoc plans/reviews/audit dumps accumulate as their own top-level `docs/*.md` files
or as bloated sections inside `ROADMAP.md` — triage into the structure above instead.

## Commands

### Backend (run from repo root)
```bash
uv sync                          # install deps
uv run vestigo-web                    # start API + serve built frontend on :8080
uv run vestigo ingest <path> -c <case> -s <source>   # CLI ingestion (no embeddings)
uv run vestigo embed -c <case> -s <source>           # CLI embedding job
uv run pytest                    # full test suite (coverage on by default, see pyproject.toml)
uv run pytest tests/test_pipeline.py            # single file
uv run pytest tests/test_pipeline.py::test_name # single test
uv run ruff check .              # lint
uv run ruff format .             # format
uv run alembic revision --autogenerate -m "describe change"  # new migration after a model change
uv run alembic upgrade head      # apply migrations (the app also does this on startup)
```
`podman compose up -d` starts the three backing services for local dev. Config is env-driven
via `VESTIGO_*` variables (see `.env.example` and `src/vestigo/core/config.py`), loaded through
pydantic-settings.

Postgres schema is managed by **Alembic** (`src/vestigo/db/migrations`; `env.py` resolves
the DSN from `get_settings()`). `PostgresStore.init_schema` upgrades to head on startup and
auto-adopts a pre-Alembic database (stamps it at revision `0001`), so deploys need no manual
step. Add schema changes as autogenerated revisions — never as inspector `ALTER TABLE`s in
`init_schema` (the old pattern, now retired). Migrations run against SQLite in tests too, so
keep them dialect-portable (`sa.func.now()`, not `sa.text('now()')`).

### Frontend (run from `frontend/`)
```bash
npm install
npm run dev                      # Vite dev server on :5173 (HMR, proxies to :8080 API)
npm run build                    # tsc -b && vite build -> dist/, served by vestigo-web
npm run typecheck                # tsc -b --noEmit
npm run lint                     # oxlint src
npm run test                     # vitest run
```
`vestigo-web` auto-builds `frontend/dist` only when it doesn't exist (see
`src/vestigo/web/app.py::_build_frontend`); set `VESTIGO_FRONTEND_REBUILD=1` to force a rebuild
after frontend changes. For active frontend work, run `npm run dev` alongside `uv run vestigo-web`
instead of rebuilding.

## Architecture

### Backend layout (`src/vestigo/`)
- `api/main.py` — FastAPI app factory; mounts routers, CORS, serves `frontend/dist` as a
  catch-all SPA route when built.
- `api/routers/` — one module per surface (`cases`, `events`, `jobs`, `auth`, `admin`,
  `agent`, `agent_tokens`, `baselines`, `converters`, `dispositions`, `enrichers`, `sigma`,
  `stories`, `stream` (SSE collaboration), `transfer`, `viz`) — thin HTTP layer over `db/`
  and `core/`. `api/deps.py` holds auth/case-access dependencies.
- `core/config.py` — single `Settings` object (pydantic-settings, `VESTIGO_` env prefix), read via
  `get_settings()`. Add new tunables here, not as scattered `os.environ` reads. Settings resolve
  per field: **environment wins**, then the DB-backed `app_settings` overrides an admin edits in
  the web console, then the built-in default. Every new field also needs a `SettingSpec` in
  `core/settings_registry.py` (group, help text, `env_only`/`secret`/`restart_required`) — a
  coverage test fails otherwise, which is what keeps "every setting is editable in the UI" true.
  `core/runtime_settings.py` owns loading/validating/persisting that layer.
- `core/capabilities.py` — one predicate per optional subsystem (embeddings, agent, MCP, OIDC,
  enrichers, Sigma, case transfer), served as `capabilities` on `/api/health`. An unconfigured
  subsystem renders **no** UI entry point and its agent tools are not advertised to the model;
  its endpoints refuse independently, so hiding is never the only enforcement.
- `core/jobs.py` — in-memory, ephemeral `JobStore` for long-running background work (embedding,
  large ingests). Jobs do **not** survive a process restart — this is intentional for the
  current single-process deployment, not an oversight.
- `db/postgres.py` — SQLAlchemy async models + `PostgresStore` for metadata (Case, Source,
  Timeline, View, Annotation, ...).
- `db/clickhouse.py` — event storage/query (`ClickHouseStore`), the primary log data store.
- `db/qdrant.py` — vector storage (`QdrantStore`); one collection per
  `(case_id, embedding_config_hash)`.
- `db/queries.py` — cross-cutting query building for the Explorer (filters, histogram).
- `db/anomaly_stats.py` — every statistical (non-embedding) detector, run directly against
  ClickHouse, plus log-template clustering. Temporal detectors score analyst-declared
  **baseline definitions** (one baseline window + 1..N labeled suspect windows, in Postgres)
  rather than a single split point; findings carry `normal`/`dismissed`/`confirmed`
  dispositions. `docs/ANOMALY_DETECTION.md` is the contract for all of them — update it in the
  same commit as any detector change. Read the module docstring before changing bucket math or
  scan-cost machinery (`HEAVY_SCAN_SETTINGS`, `HEAVY_SCAN_GATE`); it deliberately does **not**
  reuse the events-view filters that `QueryService.histogram` applies.
- `db/similarity.py` / `db/field_recommend.py` — embedding-based nearest-neighbor search and
  field-selection heuristics for the embedding wizard.
- `ingestion/parser.py` — format detection + streaming parsers (Plaso CSV/JSONL, generic
  CSV/JSONL).
- `ingestion/pipeline.py` — two distinct pipelines, deliberately separate:
  - `IngestionPipeline`: parses + writes events to ClickHouse only (fast, immediate browsing).
  - `EmbeddingPipeline`: separate, user-triggered job that embeds already-ingested events into
    Qdrant. Do not conflate these when adding ingestion features.
- `models/event.py` — `Event`, `ParserConfig`, `EmbeddingConfig` — all hashed
  (`config_hash()`) for forensic reproducibility. Changing a parser/embedding config's fields
  changes its hash and therefore its identity (new Qdrant collection, etc.) — treat these
  dataclasses as append-only where possible.
- `agent/` — the optional AI investigation agent (pydantic-ai runtime, `tools.py` tool
  registry, MCP exposure). See `docs/AGENT.md`.
- `sigma/` — Sigma rule loader/compiler/router (`docs/ANOMALY_DETECTION.md` §13).
- `stories/` — the Stories subsystem (blocks, snapshots, export). See `docs/STORIES.md`.
- `transfer/` — case export/import (`.vestigo` archive).
- `enrichers/` — post-ingest attribute enrichment (GeoIP via a local MaxMind DB).
- `cli/main.py` — Typer CLI (`vestigo`), mirrors what the API/UI does for scriptable/offline use.

### Frontend layout (`frontend/src/`)
- `api/` — one file per resource (`cases.ts`, `events.ts`, `anomalies.ts`, ...), thin fetch
  wrappers; `client.ts` holds the shared base client.
- `components/` grouped by feature area: `explorer/` (event grid, filters, histogram),
  `analysis/` (detector views, Sigma, templates, semantic search, similarity), `viz/`
  (charts), `agent/`, `stories/`, `cases/`, `timelines/`, `sources/`, `auth/`, `jobs/`,
  `tour/`, `layout/` (app shell, top bar, job tray), `ui/` (design-system primitives on top
  of Radix).
- State: Zustand for client state, TanStack Query for server state, TanStack Table/Virtual for
  the event grid.

### Key cross-cutting concepts (see `docs/MODEL_REFINEMENT.md` for full detail)
Case → Source (immutable ingested file, SHA-256 hashed) → Timeline (named grouping of sources)
→ Event (one record, scoped to a Source, stamped with an Artifact type) → optional Embedding
(Qdrant vector). Views are saved filter sets on a Timeline; Annotations attach to Events with
`origin: user | system`.

## Working conventions

- Ruff is configured with `select = ["E", "F", "I", "UP", "B", "C4", "SIM"]`, `line-length =
  100`, `E501` ignored (long lines are fine, don't wrap for length alone). Google-style
  docstrings.
- Background jobs (`core/jobs.py::JobStore`) are intentionally ephemeral/in-memory — don't add
  persistence there without a deliberate design discussion; it changes the deployment model.
- Forensic reproducibility/explainability is a hard requirement for basically any subsystem. 
- Airgapped/offline-by-default is a design goal (`VESTIGO_ALLOW_ONLINE`, `docs/TECH_STACK.md` §6).
  Don't add code paths that reach the network unconditionally. Exception: optional OIDC SSO
  (`VESTIGO_OIDC_ENABLED`) is deliberately independent of `VESTIGO_ALLOW_ONLINE` — see `TECH_STACK.md` §6.
  
## References
This project is inspired heavily by the existing projects https://github.com/ait-aecid/logdata-anomaly-miner and google/timesketch. Our goal is to become the perfect combination of them, evolving to the best forensic log analysis system in existence. Consult these for how they solve problems and their features and get inspiration there.
