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

## Milestone 1 — correctness & forensic integrity (Medium severity)

- [ ] **M1 — No silent failures on evidence mutation.** `ClickHouseStore.delete_source_events`
  swallows all exceptions (`db/clickhouse.py`, bare `except: pass` around DROP PARTITION);
  `cases.py` ingest-failure cleanup likewise. A failed delete must log loudly and surface to
  the caller — orphan events reappearing after a "successful" source delete is a forensic
  integrity bug. Distinguish "partition doesn't exist" (fine, no-op) from real errors.
- [ ] **M2 — One SQL escaping regime.** `db/clickhouse.py::count_events` interpolates with
  `{value!r}`; `delete_source_events` f-strings IDs into the partition expression. Everything
  else in `db/` uses `{name:String}` binds. Parameterize both (or validate ID charset
  explicitly where DROP PARTITION can't bind). Low exploitability today (IDs are
  server-generated and RBAC-validated) but two regimes is how injection ships later.
- [ ] **M3 — Login backoff.** No rate limiting on `POST /api/auth/login`; argon2 slows one
  attempt, not a loop. In-memory per-username+IP failure counter with exponential delay fits
  the single-process design.
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
- [ ] **M7 — JobStore cap.** `core/jobs.py` never prunes; long-lived server leaks job dicts.
  Retain last N (e.g. 200) terminal jobs, evict oldest. Stays ephemeral/in-memory by design.
- [ ] **M8 — Remove dead `secret_key` setting.** `core/config.py` defines it, nothing reads it
  (sessions are DB-backed random tokens); `docker-compose.yml` dutifully sets it. Delete both
  or actually use it.
- [ ] **Container smoke test in CI.** Build the image, `docker compose up`, curl
  `/api/health`. Would have caught C1 before it shipped.

## Milestone 3 — polish

- [ ] Split `api/routers/events.py` (1500+ lines: query parsing, export streaming, anomaly
  orchestration, bulk annotation) opportunistically when next touched — not proactively.
- [ ] `ClickHouseStore._host/_port` string-splitting breaks on `https://` and creds-in-URL
  forms — use `urllib.parse`.
- [ ] Startup config sanity report: log resolved offline mode, cookie security
  (warn when `environment=production` and `auth_cookie_secure=false`), datastore targets.
- [ ] Finish repo directory rename TraceVector → TraceSignal (coordinate with GitHub repo
  rename; see `docs/MIGRATION_RENAME.md`).
- [ ] Large-file ingest regression test: bound peak memory (or assert lazy yielding) over a
  generated ~100 MB CSV, protecting the H1 fix.

## Explicitly out of scope (decided during the audit)

- Persistent job store — in-memory is a documented deliberate choice for the single-process
  deployment model.
- CSRF tokens — SameSite=Lax cookies plus the LAN threat model are adequate for now.
- Alembic adoption — hand-rolled additive migration works at the current schema churn;
  revisit at ~5+ migrated columns.
- Proactive router/query-builder splits — churn risk outweighs payoff at current velocity.
