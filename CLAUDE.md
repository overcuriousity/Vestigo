# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Vestigo is a local-first, forensic-grade log investigation platform for security teams
of any size. It ingests Timesketch-compatible timelines (Plaso CSV/JSONL, generic CSV/JSONL) at
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
uv run pytest                    # full test suite (no coverage; CI adds --cov, see pyproject.toml)
uv run pytest tests/test_pipeline.py            # single file
uv run pytest tests/test_pipeline.py::test_name # single test
uv run ruff check .              # lint
uv run ruff format .             # format
uv run ruff format --check .     # what CI enforces — `ruff check` passing does NOT imply this
uv run alembic revision --autogenerate -m "describe change"  # new migration after a model change
uv run alembic upgrade head      # apply migrations (the app also does this on startup)
```
`podman compose up -d` starts the three backing services for local dev. Config is env-driven
via `VESTIGO_*` variables (see `.env.example` and `src/vestigo/core/config.py`), loaded through
pydantic-settings.

**The test suite requires reachable ClickHouse and PostgreSQL, and refuses to start without
them.** `tests/conftest.py::pytest_configure` probes both once (~0s) and, if either fails, exits
before collecting anything with the `podman compose up -d` fix in the message. There is no
opt-out flag, and no test skips itself over reachability any more: most of the suite reaches
them *indirectly through the app*, so a stopped container used to mean an eight-minute run
ending in driver tracebacks that never named the actual problem. Qdrant is faked, so it is not
probed. A `ClickHouseStore()` that now raises in a fixture is a real failure — don't
reintroduce a `try/except Exception: pytest.skip(...)` around it, which is exactly how a broken
`init_schema` would read as a green run.

**Tests run against real PostgreSQL, never SQLite.** The `store` fixture takes `pg_database`: a
private database cloned from a session-scoped template that Alembic migrated once (`~55ms` per
test, and *faster* than replaying the migrations into a fresh SQLite file). Variants:
`blank_pg_database` for tests that drive Alembic themselves, `module_pg_database` for a corpus
built once per module. Don't add a `sqlite+aiosqlite` URL back — the dialects disagree about
exactly what this model leans on (`JSONB` equality, `json` having no equality operator, server
defaults, boolean literals), and the `confirmed` disposition path once shipped broken on
PostgreSQL with every SQLite test passing.

asyncpg binds a connection to the event loop that opened it, and `TestClient` drives the app
from its own portal loop, so any test taking `client` gets an unpooled store (plus anything
marked `@pytest.mark.multiloop` — the CLI runs each command in its own `asyncio.run`). That is
scoped on purpose: unpooling everything costs a connect per operation and made the suite ~40%
slower. The `store` fixture also does *not* dispose its engine — those connections may belong
to the client's loop, and disposing across that boundary fails a passing test in teardown;
`pg_database` drops the database `WITH (FORCE)` immediately after, which closes them.

Postgres schema is managed by **Alembic** (`src/vestigo/db/migrations`; `env.py` resolves
the DSN from `get_settings()`). `PostgresStore.init_schema` upgrades to head on startup and
auto-adopts a pre-Alembic database (stamps it at revision `0001`), so deploys need no manual
step. Add schema changes as autogenerated revisions — never as inspector `ALTER TABLE`s in
`init_schema` (the old pattern, now retired). Migrations are exercised against real PostgreSQL
in tests (`blank_pg_database`), so a dialect-specific revision now fails where it would have
passed under the old SQLite fixtures.

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
  reuse the events-view filters that `QueryService.histogram` applies. Which fields a
  self-selecting detector scans is the analyst's call, not the recommenders': every auto path
  runs its recommendation through `apply_field_overrides` against the timeline's
  `field_overrides` (per method, shared, audited, never a lock — an explicit `fields=[…]`
  bypasses it and a held-back field is disclosed in `warnings`).
- `db/analysis_plan.py` — the analysis gate: pure predicates deciding, per method and
  without scanning an event, whether it *can* produce a finding on this data. A method is
  gated off only when it structurally cannot, never when it looks unpromising, and a gated
  method is still runnable on request — the plan is advice plus an audit record, never a
  lock. See `docs/ANOMALY_DETECTION.md` §"The analysis gate".
- `db/analysis_cache.py` — fingerprint-keyed memoization for `/analysis/findings`. The key
  covers every input that can change an answer, and sources are immutable, so a hit is proof
  the answer still holds. Deliberately no TTL. Distinct from `DetectorRun`, which stays the
  forensic diary of what an analyst ran.
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
- `columns/` — recommended event-grid columns per timeline (issue #213): a pure scorer over
  the `db/field_stats.py` cache, an optional one-shot typed LLM call that only reorders the
  scorer's candidates (`docs/AGENT.md` §"Outside the agent loop"), and the job that persists
  the result to `Timeline.recommended_columns`. Display metadata — a per-user column choice
  in the browser outranks it for every *automatic* recompute, but an explicit re-suggest
  clears that override (the analyst asked for the new answer). The job's `use_llm` defaults
  to False and only the "Suggest with AI" endpoint sets it, so every automatic trigger
  scores locally; the analyst answers once per timeline — yes or no, both recorded — after a
  disclosure naming what is sent.
- `converters/` — generated converters (1.13): the prompt rendered from the Parquet
  contract (`prompt.py`), the head/middle/tail sample (`sample.py`), the stdlib-only guarded
  runner (`runner.py`: AST deny-list + `python -I` + rlimits), the output validator
  (`validate.py`), the one-shot typed generator on the agent plumbing (`generator.py`), and
  the convert-and-ingest job (`job.py`) that hands the Parquet to
  `api/routers/cases.py::register_source_for_ingest`. Off by default
  (`converter_generation_enabled`); the produced Parquet *is* the source. See
  `docs/INPUT_FORMATS.md` §"Generated converters" and `docs/AGENT.md` §"Outside the agent
  loop".
- `sigma/` — Sigma rule loader/compiler/router (`docs/ANOMALY_DETECTION.md` §13).
- `stories/` — the Stories subsystem (blocks, snapshots, export). See `docs/STORIES.md`.
- `transfer/` — case export/import (`.vestigo` archive).
- `enrichers/` — post-ingest attribute enrichment (GeoIP and ASN via local MaxMind DBs;
  each enricher is a self-contained module, no shared code by design).
- `demo/` — the fabricated demo case every user is seeded on first login: a deterministic
  generator (`scenario.py`, `sources/`), the analyst artifacts (`metadata.py`), and the
  build that ingests it through the real pipeline (`build.py`). Generated per user rather
  than shipped as data. `tests/test_demo_detector_coverage_clickhouse.py` asserts every
  analysis tool still finds something in it — keep that green when retuning a detector.
- `cli/main.py` — Typer CLI (`vestigo`), mirrors what the API/UI does for scriptable/offline use.

### Frontend layout (`frontend/src/`)
- `api/` — one file per resource (`cases.ts`, `events.ts`, `anomalies.ts`, ...), thin fetch
  wrappers; `client.ts` holds the shared base client.
- `components/` grouped by feature area: `explorer/` (event grid, filters, histogram),
  `analysis/` (the Investigate surface: `InvestigateRail` holds findings grouped by evidence
  weight and is the only fixed-width surface the *analysis* flow spends — the agent panel is
  the other panel an analyst may open beside it, deliberately, since reading a finding while
  asking about it is the intended workflow; `DetectorMuteStrip` sits above the feed and takes
  a method out of the sweep entirely — shared, audited state on `Timeline.muted_methods`,
  never a lock (the plan ignores it and a muted method still runs when asked for by name),
  and the count it holds back is always disclosed; `InvestigateSheet` is one absolutely-positioned
  overlay in three modes — finding, method, tools — so detail can be wide without ever
  widening the row, and it sizes to its content rather than the viewport so a short finding
  does not strand its verdict bar a screen below the claim; `ToolsSheet` is four tabs (Scope,
  Methods, Signatures, Explore) rather than one scroll, so a thousand-row template list cannot
  bury the baseline picker; `method-registry.ts` is the
  single description of all twelve methods, including the prose that used to live in a
  Method tab and each method's optional `railFloor`, a presentation-only bar on the ranked
  feed whose held-back count is always disclosed; the sheet's method
  mode runs a method with the analyst's own knob values, which is what keeps the analysis
  gate advice rather than a lock in the UI as well as the API), `viz/`
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
- The analysis plan endpoint may never withhold a method. It reports what is worth running
  first and explains what it did not run; every method it marks `not_applicable` still runs
  through `/analysis/findings` and returns what an unconditional sweep would have. A wrong
  precondition fails silently, so `tests/test_demo_detector_coverage_clickhouse.py` asserts
  the gate offers every method that file proves finds something.
- A `require_case_read` endpoint does not write. Two deliberate exceptions exist, neither of
  them a precedent — each carries its own argument, and a third would need its own too:
  - `api/routers/cases.py::_settle_dead_recommendations` relabels a `running` column
    recommendation whose job is provably gone, because a read-only member has no other way to
    repair a timeline that reports "suggesting columns…" forever. It is bounded to display
    metadata, recomputes nothing, touches no evidence and records no audit row.
  - `api/routers/analysis.py::get_analysis_findings` writes the answer it just computed into
    `analysis_cache`. The row is derived data keyed by a fingerprint of its own inputs, so it
    asserts nothing the request did not already establish; it touches no evidence, records no
    audit row, and eviction is bounded per case. Refusing the write for read-only members
    would make the surface they use slowest the one that never warms — and every member's
    first open pays the full sweep either way.
- Airgapped/offline-by-default is a design goal (`VESTIGO_ALLOW_ONLINE`, `docs/TECH_STACK.md` §6).
  Don't add code paths that reach the network unconditionally. Exception: optional OIDC SSO
  (`VESTIGO_OIDC_ENABLED`) is deliberately independent of `VESTIGO_ALLOW_ONLINE` — see `TECH_STACK.md` §6.
  
## References

Vestigo builds on two projects, and **the two relationships are not the same — keep them
distinct in anything user-facing.**

- [timesketch](https://github.com/google/timesketch) — same category. The investigative
  model (cases, timelines, collaborative annotation) is descended from it. This is the
  comparison we invite and where we claim to be ahead; `docs/CONCEPT.md` §8 lists the axes
  (detection as the workflow, event-granular provenance, low operational floor, embeddings
  optional not required, reporting built in) and what we are behind on (production
  hardening, analyzer ecosystem, community).
- [logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner) — **method
  source, not a competitor.** Different problem (online detection over live streams).
  Never claim to beat it or replace it. We took the explainability principle and a
  catalogue of methods re-derived as batch SQL, and we are deliberately narrower: detectors
  must be field-agnostic and SQL-explainable, `TSAArimaDetector`/`PCADetector` are skipped.
  About two thirds of its catalogue has an analog here; the remaining gaps and the places
  our analog is narrower than the original are tracked in `ROADMAP.md` Milestone 4 — keep
  that list honest rather than claiming parity. Someone who needs live-stream detection
  should run AMiner.

Consult both for how they solve problems, and borrow freely.

**Tone rule for anything user-facing** (README, docs, UI copy): confident about what we
actually ship, never dismissive of prior art, and never a claim we cannot point at code
for. Credit the inspiration explicitly. Do not lump the two references together as
"projects we improve on" — that reads as a claim against AMiner that we do not make.
"We think we are better at X, here is why" is
fine; "project Y is bad" is not, and neither is a comparative claim about another
project's current feature set that nobody verified.
