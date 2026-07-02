# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

TraceVector is a local-first, forensic-grade log investigation platform for small security
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
`docs/TECH_STACK.md` records *why* each backing service was chosen and what's still open
(auth backend, frontend stack was TBD but has since been decided — see `frontend/`).
there are multiple plans and roadmaps, which should be consulted, kept up to date and removed when done.

## Commands

### Backend (run from repo root)
```bash
uv sync                          # install deps
uv run tv-web                    # start API + serve built frontend on :8080
uv run tv ingest <path> -c <case> -s <source>   # CLI ingestion (no embeddings)
uv run tv embed -c <case> -s <source>           # CLI embedding job
uv run pytest                    # full test suite (coverage on by default, see pyproject.toml)
uv run pytest tests/test_pipeline.py            # single file
uv run pytest tests/test_pipeline.py::test_name # single test
uv run ruff check .              # lint
uv run ruff format .             # format
```
`podman compose up -d` starts the three backing services for local dev. Config is env-driven
via `TV_*` variables (see `.env.example` and `src/tracevector/core/config.py`), loaded through
pydantic-settings.

### Frontend (run from `frontend/`)
```bash
npm install
npm run dev                      # Vite dev server on :5173 (HMR, proxies to :8080 API)
npm run build                    # tsc -b && vite build -> dist/, served by tv-web
npm run typecheck                # tsc -b --noEmit
npm run lint                     # oxlint src
npm run test                     # vitest run
```
`tv-web` auto-builds `frontend/dist` on first run if it doesn't exist (see
`src/tracevector/web/app.py::_build_frontend`). For active frontend work, run `npm run dev`
alongside `uv run tv-web` instead of rebuilding.

## Architecture

### Backend layout (`src/tracevector/`)
- `api/main.py` — FastAPI app factory; mounts routers, CORS, serves `frontend/dist` as a
  catch-all SPA route when built.
- `api/routers/` — `cases.py`, `events.py`, `jobs.py` — thin HTTP layer over `db/` and `core/`.
- `core/config.py` — single `Settings` object (pydantic-settings, `TV_` env prefix), cached via
  `get_settings()`. Add new tunables here, not as scattered `os.environ` reads.
- `core/jobs.py` — in-memory, ephemeral `JobStore` for long-running background work (embedding,
  large ingests). Jobs do **not** survive a process restart — this is intentional for the
  current single-process deployment, not an oversight.
- `db/postgres.py` — SQLAlchemy async models + `PostgresStore` for metadata (Case, Source,
  Timeline, View, Annotation, ...).
- `db/clickhouse.py` — event storage/query (`ClickHouseStore`), the primary log data store.
- `db/qdrant.py` — vector storage (`QdrantStore`); one collection per
  `(case_id, embedding_config_hash)`.
- `db/queries.py` — cross-cutting query building for the Explorer (filters, histogram).
- `db/anomaly_stats.py` — statistical (non-embedding) anomaly detectors run directly against
  ClickHouse: `value_novelty` (rare/first-seen field values) and `frequency`
  (z-score spikes/silences over time buckets). Both support a self-baseline mode and a
  temporal mode (`baseline_end` splits baseline vs. detect window). Read the module docstring
  before changing bucket math — it deliberately does **not** reuse the events-view filters that
  `QueryService.histogram` applies.
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
- `cli/main.py` — Typer CLI (`tv`), mirrors what the API/UI does for scriptable/offline use.

### Frontend layout (`frontend/src/`)
- `api/` — one file per resource (`cases.ts`, `events.ts`, `anomalies.ts`, ...), thin fetch
  wrappers; `client.ts` holds the shared base client.
- `components/` grouped by feature area: `explorer/` (event grid, filters, histogram),
  `analysis/` (anomaly/frequency/value-novelty views, semantic search, similarity),
  `cases/`, `timelines/`, `sources/`, `triage/`, `layout/` (app shell, top bar, job tray),
  `ui/` (design-system primitives on top of Radix).
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
- Airgapped/offline-by-default is a design goal (`TV_ALLOW_ONLINE`, `docs/TECH_STACK.md` §6).
  Don't add code paths that reach the network unconditionally.
  
## References
This project is inspired heavily by the existing projects https://github.com/ait-aecid/logdata-anomaly-miner and google/timesketch. Our goal is to become the perfect combination of them, evolving to the best forensic log analysis system in existence. Consult these for how they solve problems and their features and get inspiration there.
