<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.svg">
    <img src="docs/assets/logo.svg" alt="Vestigo" width="320">
  </picture>
</p>

<p align="center"><em>vestigo</em> (Latin) — <em>I follow the tracks; I investigate.</em></p>

<p align="center">
  <a href="https://github.com/overcuriousity/Vestigo/actions/workflows/ci.yml"><img src="https://github.com/overcuriousity/Vestigo/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/overcuriousity/Vestigo/actions/workflows/codeql.yml"><img src="https://github.com/overcuriousity/Vestigo/actions/workflows/codeql.yml/badge.svg" alt="CodeQL"></a>
  <a href="https://github.com/overcuriousity/Vestigo/releases/latest"><img src="https://img.shields.io/github/v/release/overcuriousity/Vestigo?logo=github" alt="Latest release"></a>
  <a href="https://github.com/overcuriousity/Vestigo/pkgs/container/vestigo"><img src="https://img.shields.io/badge/container-ghcr.io-blue?logo=docker&logoColor=white" alt="Container image"></a>
  <a href="LICENSE"><img src="https://img.shields.io/github/license/overcuriousity/Vestigo" alt="License: GPL-3.0"></a>
  <img src="https://img.shields.io/badge/python-3.13-3776AB?logo=python&logoColor=white" alt="Python 3.13">
  <img src="https://img.shields.io/badge/react-19-61DAFB?logo=react&logoColor=black" alt="React 19">
</p>

A forensic-grade post-mortem log investigation platform.

Vestigo ingests Timesketch-compatible timelines at scale, lets analysts explore events
through an ELK-like web interface, and surfaces anomalies both statistically and by embedding
every log line into a vector database. It's built to run airgapped if needed — with reproducible, auditable processing at every step.

## Why

Incident responders and forensic analysts work with massive timeline-shaped datasets (Plaso
output, Windows Event Logs, endpoint telemetry, cloud audit trails). The usual options force a
tradeoff: a full SIEM is expensive, noisy, and not timeline-native; notebook scripts are
flexible but not reproducible or team-friendly; [Timesketch](https://github.com/google/timesketch)
is powerful but operationally heavy. Vestigo aims to be the focused middle ground — ingest
huge logs, explore them like an ELK stack, and let both classic statistical detectors and local
embeddings surface the needles in the haystack, without needing a cluster to run it.

<img width="2866" height="1589" alt="image" src="https://github.com/user-attachments/assets/d505af86-9ba2-4fe1-b448-10b18ae2d409" />

## Capabilities

### Ingestion
- Streaming parsers for Plaso CSV/JSONL and generic CSV/JSONL handle tens of gigabytes without
  loading everything into memory.
- Every ingested file (**Source**) is SHA-256 hashed and retained content-addressed for
  re-download — forensic provenance and immutability by construction.
- CLI-available (`vestigo ingest`), so ingestion is scriptable and reproducible outside the web UI.
  For very large files (tens of GiB and up), prefer it over the web upload: it streams straight
  from disk — no HTTP upload, no temp copy, no `VESTIGO_MAX_UPLOAD_BYTES` cap — with live
  progress/ETA and per-user Source attribution for chain-of-custody. See `vestigo ingest --help`.
- A separate, user-triggered embedding job (`vestigo embed` / the embed wizard) computes vectors
  after ingestion — ingestion itself stays fast and embedding-free until you ask for it.
- Downloadable converter scripts (nginx, filterlog, suricata, cloudtrail, pcap, and more) parse
  vendor-specific log formats client-side into typed, columnar **Parquet**, which the server
  bulk-inserts via Arrow record batches — an order of magnitude faster than row-by-row CSV on
  multi-GB logs, with the same per-row forensic provenance. See
  [`docs/TECH_STACK.md`](docs/TECH_STACK.md) §3.4a.

### Explorer
- A virtualized, ELK-like event grid: resizable/pickable columns, comfortable/compact density,
  eye-friendly light/dark themes.
- Full-text and structured filtering (artifact type, source, tags, arbitrary field
  equality/exclusion), pushed down into ClickHouse rather than resolved client-side.
- Time histogram with anomaly overlay markers, bidirectional keyset pagination with jump-to-time.
- Tag/comment annotations with bulk apply, clickable include/exclude tag facets, and saved views.
- Streaming CSV/JSONL export honoring the active filters, with forensic columns
  (`source_id`, `artifact`, `content_hash`, `file_hash`) included.

### Anomaly detection
- **Nine statistical detectors**, inspired by
  [`ait-aecid/logdata-anomaly-miner`](https://github.com/ait-aecid/logdata-anomaly-miner) and
  run directly against ClickHouse — no embeddings required: value novelty, frequency
  spikes/silences, timestamp order, numeric range, charset novelty, entropy outliers,
  proportion shift, interval cadence, and never-seen event sequences.
- **Semantic similarity search** over locally-computed embeddings (Qdrant): find events that
  *mean* the same thing as a known-bad line, across differing literal content.
- Explicit **baseline vs. suspect windows**: every detector can compare a known-good period
  against the window under investigation, instead of only self-baselining.
- Findings carry a unified disposition workflow (confirm/dismiss), and manually-confirmed
  findings survive re-scans.
- See [Anomaly Detection](docs/ANOMALY_DETECTION.md) for a plain-language explanation of every
  detector — what it catches, how it scores, and its knobs.

### Authentication, access control, and audit
- Session-cookie authentication for local accounts, with a seeded one-time bootstrap admin and
  optional OIDC SSO for teams that already run an identity provider.
- Case-level RBAC: personal cases (owner-only) or team cases (member/manager roles), enforced
  centrally on every case-scoped endpoint.
- An append-only audit trail covering every mutating action and authentication event, with
  self-service and admin-wide query/export.
- Live collaboration via Server-Sent Events: analysts viewing the same case see each other's
  annotations and tags appear without a manual refresh.

### Forensic rigor
- Immutable, hashed Sources; parser and embedding configs are hashed too, so changing either
  changes its identity (a new Qdrant collection, a new reproducibility boundary) rather than
  silently mutating results in place.
- Airgapped/offline-by-default operation is a hard design goal — no code path reaches the
  network unconditionally (OIDC SSO is the one deliberate, documented, operator-opted-in
  exception).

## Architecture

- **Backend**: Python 3.13+, FastAPI/Uvicorn, managed with `uv`. Talks to three external
  services — PostgreSQL (metadata), ClickHouse (events, the primary log data store), and
  Qdrant (vectors). None of them run inside the app itself.
- **Frontend**: React 19 + Vite + TypeScript, served as a static build directly from Uvicorn
  (no separate web server required).
- **CLI**: a Typer-based `vestigo` command mirrors the API/UI for scriptable, offline-friendly use.

## Quick start

### 1. Provide backing services

Run PostgreSQL, ClickHouse, and Qdrant natively, or use the reference Docker/Podman Compose:

```bash
docker compose up -d
```

The compose file publishes all three services on `127.0.0.1` only — they run with default or
no credentials, so they are deliberately unreachable from the LAN. The app's defaults
(`.env.example`) connect via these localhost ports.

### 2. Install and run the application

```bash
uv sync
uv run vestigo-web
```

The base install ships without the local embedding stack (~2 GB of torch +
sentence-transformers). To use local embeddings (`vestigo embed`, semantic search), install the
extra: `uv sync --extra embeddings`. Alternatively point `VESTIGO_EMBEDDING_API_BASE_URL` at a
remote OpenAI-compatible endpoint — no extra needed. Without either, embedding endpoints
return a clear 503 and `/api/health` reports `embeddings_available: false`; everything else
works normally.

The API is available at `http://localhost:8080` (OpenAPI docs at `/api/docs`), serving the
built frontend from `frontend/dist` (auto-built on first run).

For active frontend development, run `npm install && npm run dev` in `frontend/` alongside
`uv run vestigo-web` — see `frontend/README.md`. Configuration is env-driven (`VESTIGO_*` variables); see
`.env.example` for the full list.

### Docker/Podman Compose (optional containerized app)

Released application images are published to GitHub Container Registry:

```bash
docker pull ghcr.io/overcuriousity/vestigo:latest
```

`docker-compose.yml` ships with a **commented-out** `app` service that builds the image from
the local checkout (`Dockerfile`) and reaches the backing services over the compose-internal
network. Uncomment it, then `docker compose up -d` (or `podman compose up -d`) brings up the
full stack in one command.

**This compose file is a reference/evaluation deployment, not a production hardening guide.**
It ships with fixed, well-known defaults so it works out of the box: `postgres`/`vestigo`
DB credentials, no ClickHouse/Qdrant auth, and a one-time `VESTIGO_ADMIN_PASSWORD` bootstrap secret
(forced to rotate on first login). For any deployment reachable by more than you — and
generally for real production use — prefer the native `uv run vestigo-web` install against
properly credentialed, network-restricted backing services, and set your own
`VESTIGO_ADMIN_PASSWORD`/`VESTIGO_*_PASSWORD`/`VESTIGO_QDRANT_API_KEY` values rather than the compose
defaults.

### Airgapped installation

Vestigo's application layer (backend + frontend) can be installed fully offline. **The
three backing services — PostgreSQL, ClickHouse, Qdrant — are out of scope for this
procedure**: provision them on the airgapped network however you normally handle offline
service deployment (e.g. `podman load` of pre-pulled images, or native packages).

On a machine **with internet access**:

1. Clone or copy the repository.
2. Install and build everything, so all dependencies are resolved and cached locally:
   ```bash
   uv sync --extra embeddings
   cd frontend && npm install && npm run build && cd ..
   ```
   This populates `.venv/` (all Python dependencies, including the CPU PyTorch wheels for
   local embeddings — drop `--extra embeddings` if the deployment won't embed locally) and
   `frontend/dist/` (the built static frontend).
3. Copy the whole repository — including `.venv/`, `uv.lock`, and `frontend/dist/` — to a
   portable drive.

On the **airgapped machine**:

1. Copy the repository from the portable drive.
2. Point `VESTIGO_POSTGRES_URL`, `VESTIGO_CLICKHOUSE_URL`, and `VESTIGO_QDRANT_URL` (in `.env`, copied from
   `.env.example`) at the already-running backing services on the isolated network.
3. Run the app directly from the carried-over virtualenv — no `uv sync` or `npm install`
   needed, since both were already resolved on the online machine:
   ```bash
   .venv/bin/vestigo-web
   ```
   Because `frontend/dist/` was carried over and the app is started via the `.venv` entry
   point directly (not `uv run`, which would try to re-resolve the environment), no network
   access is required at any point on the airgapped machine. `VESTIGO_ALLOW_ONLINE=false` (the
   default) additionally keeps the embedding pipeline from reaching any remote endpoint.
4. Same binary compatibility requirements apply as any offline Python deployment: build and
   run on matching OS/architecture (e.g. build on the same Linux distribution/glibc version
   you'll run on), since the `.venv/` carries compiled wheels (PyTorch, onnxruntime, etc.).

## Stability & upgrades

What the 1.0 line guarantees, and what it doesn't:

- **PostgreSQL metadata schema** is Alembic-managed; the app migrates to the current head
  automatically on startup. Upgrading a 1.0.x deployment is: stop, update code/image, start.
- **Parquet interchange format v1** (converter output) is stable: files produced by any 1.0
  converter script remain ingestible by any 1.0 server. Files written by pre-rename
  (`*2tracesignal.py`) converters are still accepted.
- **Forensic identity is append-only**: parser/embedding config hashes (`config_hash()`)
  identify processing configurations; existing hashes never change meaning within 1.0.x.
- **ClickHouse and Qdrant schemas** have no in-place migration story yet: within 1.0.x they
  won't change; a future change would come with an explicit re-ingest/re-embed procedure in
  the release notes, never a silent one.
- The REST API is versioned by the app itself (`/api/health` reports the version); breaking
  API changes are reserved for 2.0.

**Upgrading from a pre-1.0 (TraceSignal) deployment:** the project was renamed for 1.0 —
CLI `tsig` → `vestigo`, env vars `TS_*` → `VESTIGO_*`, and default backing-store names are
now `vestigo`. Existing data stays where it is: rename your env vars and pin the old names
via `VESTIGO_POSTGRES_URL`, `VESTIGO_CLICKHOUSE_DATABASE`, and
`VESTIGO_QDRANT_COLLECTION_PREFIX`. See [CHANGELOG.md](CHANGELOG.md).

## Inspiration

Vestigo's design draws on two projects:

- **[Timesketch](https://github.com/google/timesketch)** — for the timeline-centric
  investigation model (Plaso-compatible ingestion, ELK-like exploration, saved views,
  collaborative annotation) that Vestigo aims to make lighter-weight and easier to
  self-host.
- **[ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner)** —
  for the statistical, non-ML approach to log anomaly detection (rare-value and frequency
  detectors) that Vestigo's `db/anomaly_stats.py` engine adapts to run directly over
  ClickHouse alongside the vector-backed similarity search.

The goal is to land somewhere between the two: Timesketch's investigative UX combined with
aminer's lightweight, explainable anomaly detection, evolving toward the best forensic log
analysis system for a small, self-hosted team.

## Documentation

- [Concept](docs/CONCEPT.md)
- [Input Formats](docs/INPUT_FORMATS.md) — CSV/JSONL/Parquet field-level normalization spec
- [Anomaly Detection](docs/ANOMALY_DETECTION.md) — every statistical detector explained, plain language
- [Tech Stack](docs/TECH_STACK.md)
- [Model Refinement](docs/MODEL_REFINEMENT.md) — approved Case / Source / Timeline / Artifact redesign
- [Roadmap](docs/ROADMAP.md)
- [Progress](docs/PROGRESS.md)
- [Changelog](CHANGELOG.md)

## License

GPL-3.0 — see [LICENSE](LICENSE).
