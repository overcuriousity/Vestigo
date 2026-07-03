# TraceSignal

A local-first, forensic-grade log investigation platform for small security teams.

TraceSignal ingests Timesketch-compatible timelines at scale, lets analysts explore events
through an ELK-like web interface, and surfaces anomalies both statistically and by embedding
every log line into a vector database. It's built to run entirely on a team's own hardware —
airgapped if needed — with reproducible, auditable processing at every step.

## Why

Incident responders and forensic analysts work with massive timeline-shaped datasets (Plaso
output, Windows Event Logs, endpoint telemetry, cloud audit trails). The usual options force a
tradeoff: a full SIEM is expensive, noisy, and not timeline-native; notebook scripts are
flexible but not reproducible or team-friendly; [Timesketch](https://github.com/google/timesketch)
is powerful but operationally heavy. TraceSignal aims to be the focused middle ground — ingest
huge logs, explore them like an ELK stack, and let both classic statistical detectors and local
embeddings surface the needles in the haystack, without needing a cluster to run it.

## Capabilities

### Ingestion
- Streaming parsers for Plaso CSV/JSONL and generic CSV/JSONL handle tens of gigabytes without
  loading everything into memory.
- Every ingested file (**Source**) is SHA-256 hashed and retained content-addressed for
  re-download — forensic provenance and immutability by construction.
- CLI-first (`tsig ingest`), so ingestion is scriptable and reproducible outside the web UI.
- A separate, user-triggered embedding job (`tsig embed` / the embed wizard) computes vectors
  after ingestion — ingestion itself stays fast and embedding-free until you ask for it.

### Explorer
- A virtualized, ELK-like event grid: resizable/pickable columns, comfortable/compact density,
  light/dark theme.
- Full-text and structured filtering (artifact type, source, tags, arbitrary field
  equality/exclusion), pushed down into ClickHouse rather than resolved client-side.
- Time histogram with anomaly overlay markers, bidirectional keyset pagination with jump-to-time.
- Tag/comment annotations with bulk apply, clickable include/exclude tag facets, and saved views.
- Streaming CSV/JSONL export honoring the active filters, with forensic columns
  (`source_id`, `artifact`, `content_hash`, `file_hash`) included.

### Anomaly detection
- **Statistical engine**, inspired by
  [`ait-aecid/logdata-anomaly-miner`](https://github.com/ait-aecid/logdata-anomaly-miner)'s
  value/frequency detector approach, run directly against ClickHouse — no embeddings required:
  - `value_novelty` — rare or first-seen field values.
  - `frequency` — z-score spikes and silences over time buckets.
  - Both support a self-baseline mode and a temporal mode (split a case into a baseline window
    and a detection window).
  - Detector runs persist to Postgres, so results survive rather than being re-derived from a
    URL-encoded event-ID list.
- **Vector-backed similarity and semantic search**, via a local sentence-transformer or an
  OpenAI-compatible remote embedding endpoint, backed by Qdrant. Nearest-neighbor search,
  distance-to-centroid outlier scoring, and free-text semantic search across a case.
- Cross-detector suppression and pinned, forensic-grade annotations of confirmed findings —
  framed throughout the UI as *triage*, not automated threat detection.

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
- **CLI**: a Typer-based `tsig` command mirrors the API/UI for scriptable, offline-friendly use.

## Quick start

### 1. Provide backing services

Run PostgreSQL, ClickHouse, and Qdrant natively, or use the reference Docker/Podman Compose:

```bash
docker compose up -d
```

### 2. Install and run the application

```bash
uv sync
uv run tsig-web
```

The API is available at `http://localhost:8080` (OpenAPI docs at `/api/docs`), serving the
built frontend from `frontend/dist` (auto-built on first run).

For active frontend development, run `npm install && npm run dev` in `frontend/` alongside
`uv run tsig-web` — see `frontend/README.md`. Configuration is env-driven (`TS_*` variables); see
`.env.example` for the full list.

## Inspiration

TraceSignal's design draws on two projects:

- **[Timesketch](https://github.com/google/timesketch)** — for the timeline-centric
  investigation model (Plaso-compatible ingestion, ELK-like exploration, saved views,
  collaborative annotation) that TraceSignal aims to make lighter-weight and easier to
  self-host.
- **[ait-aecid/logdata-anomaly-miner](https://github.com/ait-aecid/logdata-anomaly-miner)** —
  for the statistical, non-ML approach to log anomaly detection (rare-value and frequency
  detectors) that TraceSignal's `db/anomaly_stats.py` engine adapts to run directly over
  ClickHouse alongside the vector-backed similarity search.

The goal is to land somewhere between the two: Timesketch's investigative UX combined with
aminer's lightweight, explainable anomaly detection, evolving toward the best forensic log
analysis system for a small, self-hosted team.

## Documentation

- [Concept](docs/CONCEPT.md)
- [Tech Stack](docs/TECH_STACK.md)
- [Model Refinement](docs/MODEL_REFINEMENT.md) — approved Case / Source / Timeline / Artifact redesign
- [Roadmap](docs/ROADMAP.md)
- [Progress](docs/PROGRESS.md)

## License

MIT — see [LICENSE](LICENSE).
