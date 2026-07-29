# Vestigo — Application Concept

The concept below is implemented. The open backlog lives in
[`docs/ROADMAP.md`](./ROADMAP.md); the chronological change log is
[`docs/PROGRESS.md`](./PROGRESS.md); stack decisions are recorded in
[`docs/TECH_STACK.md`](./TECH_STACK.md).

## 1. Vision (one-liner)
A local-first, forensic-grade log investigation platform for small security teams: ingest Timesketch-compatible timelines at scale, explore them through an ELK-like web interface, and detect anomalies with statistical detectors running directly over ClickHouse plus embedding-based semantic search — with an optional AI investigation agent as analysis companion.

## 2. Problem Statement
Incident responders and forensic analysts work with massive timeline-shaped datasets (Plaso output, Windows Event Logs, endpoint telemetry, cloud audit trails). Existing options force a choice between:
- **Full SIEMs** that are expensive, noisy, and not timeline-investigation native.
- **Notebook scripts** that are flexible but not reproducible or team-friendly.
- **Timesketch** which is powerful but operationally heavy and broad.

Vestigo is a focused, self-hosted alternative: ingest huge logs, explore them like an ELK stack, and let local embeddings surface the needles in the haystack.

## 3. Target User
**Small security team (2–10 analysts), self-hosted, often airgapped.**
- Runs on the team's own hardware or a private cloud.
- Needs forensic rigor: reproducible processing, immutable source data, audit-friendly outputs.
- Wants minimal operational complexity and no mandatory external services.

## 4. Core Value Proposition
- **Large-scale ingestion**: Process tens of gigabytes of Timesketch-compatible timeline data (CSV, JSONL, Plaso) without exhausting memory.
- **ELK-like exploration**: Search, filter, time-range zoom, and annotate events through a responsive web UI.
- **Explainable anomaly detection**: A suite of SQL-explainable statistical detectors over ClickHouse (value novelty, frequency, charset/entropy/range, periodicity, sequence n-grams, distribution drift, proportion shift — see `docs/ANOMALY_DETECTION.md`) plus Sigma rule runs, complemented by locally-embedded vectors in Qdrant for outlier and semantic similarity search.
- **Forensic rigor**: Immutable ingestion, config-stability checks for embedding models, and offline-by-default operation.

## 5. Key Concepts / Data Model

The current vocabulary is defined and implemented in
[`docs/MODEL_REFINEMENT.md`](./MODEL_REFINEMENT.md).

| Concept | Description |
|--------|-------------|
| **Case** | An investigation container (e.g. "Compromised endpoint ACME-123"). |
| **Source** | One ingested file — the unit of forensic provenance and immutability. Hashed with SHA-256; retained for re-download. |
| **Timeline** | A named grouping of 1..N Sources — the merged, correlated chronological view. Every case has a default "All sources" timeline. |
| **Event** | One record from a Source; scoped by `source_id` and stamped with its **Artifact** type. |
| **Artifact** | The per-event Plaso class and long description (`LOG` / `Syslog line`, `WEBHIST` / `Firefox history`, …). Renamed from `source`/`source_long`. |
| **Embedding** | A dense vector representation of an event's textual content, produced by a local model. |
| **Vector Collection** | A Qdrant collection holding event embeddings for a case, keyed by embedding-config hash. |
| **View** | A saved set of filters (time range, full-text, artifact, source toggle, field values) applied to a Timeline. |
| **Annotation** | A `tag` or `comment` on Events (`origin: user`), or an `anomaly` marker from a detector run (`origin: system`). |

## 6. Core Feature Set (shipped)

### 6.1 Ingestion
- Ingest directories or single files in Timesketch-compatible formats:
  - Plaso CSV / JSONL
  - Generic CSV with configurable column mapping
  - Generic JSONL (one event per line)
  - Vestigo Parquet interchange files produced by the downloadable converter scripts
    (raw-log provenance embedded per row; see `docs/MODEL_REFINEMENT.md`)
- Streaming parser: handle 80 GiB+ inputs without loading everything into RAM.
- Per-event SHA-256 hash and provenance metadata (source file, byte offset, parser config).
- Optional deduplication by hash or by (file path + offset).
- Parallel embedding of event text into a local sentence-transformer / log-specific model.
- Upsert events and vectors into the primary store and Qdrant in batches.

### 6.2 Storage & Vector Backend
- Primary event store: **ClickHouse** (decision recorded in [`docs/TECH_STACK.md`](./TECH_STACK.md)).
- Vector store: **Qdrant** (consistent with ScalarForensic, supports local disk and airgapped deployment).
- One Qdrant collection per `(case, embedding-config hash)`, with deterministic naming.
- Store embedding model configuration (model name, pooling, normalization) alongside vectors and enforce config-match on query.

### 6.3 Web UI (ELK-like investigation interface)
- Case / source / timeline list and management.
- Event table with configurable columns.
- Full-text search, artifact/source-specific filters, and time-range picker.
- Pagination and infinite scroll for large result sets.
- Saved views per case.
- Multi-select events and add tags/comments.
- Export filtered results or full annotated timeline as CSV/JSONL.
- Time histogram and per-source color stripes in the Explorer.
- Chart-based visualization of any aggregation (`docs/ROADMAP.md` tracks the remaining
  chart families).
- Stories: an investigation write-up built from live view/chart/event blocks, exportable as
  a frozen snapshot (`docs/STORIES.md`).
- An optional AI investigation agent working through read-only, case-scoped tools
  (`docs/AGENT.md`).
- Live collaboration: annotations and tags from other analysts appear without a refresh.

### 6.4 Analysis panel
- Statistical detectors over ClickHouse — no embeddings needed, results the instant
  ingestion finishes. Every detector is SQL-explainable and scored against an
  analyst-declared baseline window; see `docs/ANOMALY_DETECTION.md`.
- Sigma rule runner for signature matching, and log-template clustering for surfacing rare
  line *shapes* without naming a field.
- Semantic similarity search over embeddings: paste or select an event and find the most
  similar log lines, with nearest neighbors and distances shown as the explanation.
- Findings carry a `normal` / `dismissed` / `confirmed` disposition that survives re-scans,
  and every persisted run keeps its parameters under a `run_id`.

Embedding-space *outlier* scoring (density over Qdrant neighbors, rare-cluster
highlighting) is deliberately not built: the statistical detectors cover the same ground
while staying explainable.

### 6.5 Deployment & Operation
- Vestigo is a native Python application managed with `uv`.
- Backing services (PostgreSQL, ClickHouse, Qdrant) are external; the operator provides them via Docker, native packages, managed services, etc.
- Optional reference `docker-compose.yml` for one-command setup.
- Airgapped mode by default: no outbound network calls for model downloads or telemetry.
- Optional `VESTIGO_ALLOW_ONLINE` setting for first-time model download.
- Multi-user auth for team access: session cookies with optional OIDC SSO, case-level RBAC
  with teams, and an append-only audit trail over every mutating action.
- Optional GPU acceleration: AMD ROCm 6.4 primary, NVIDIA CUDA 12.8 secondary; CPU is the default.

## 7. Explicitly Out of Scope
- SaaS multi-tenancy, billing, or managed hosting.
- Pluggable analyzer marketplace.
- Graph/link analysis visualizations.
- Bespoke endpoint collection agents — Vestigo stays agentless; streaming ingest (a
  roadmap milestone) accepts pushes from existing collectors instead.
- Server-side raw-log parsing — parsing is converter territory (client-side Parquet
  interchange converters), permanently out of core scope.

Formerly listed here but since taken on: the story/report builder shipped (see
`docs/STORIES.md`), while streaming ingest (Milestone 6) and correlation rules (D10) are
open roadmap items — see `docs/ROADMAP.md`.

## 8. Differentiation
- **Scale + simplicity**: Designed from the start for 80 GiB+ timelines while staying a single native app that's easy to operate.
- **Embedding-native investigation**: Vectors are not an afterthought; they power anomaly detection and semantic search inside the same UI used for filtering.
- **Forensic rigor by default**: Immutable sources, provenance metadata, model-config stability checks, offline-first operation.
