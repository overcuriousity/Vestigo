# TraceVector — Application Concept

## 1. Vision (one-liner)
A local-first, forensic-grade log investigation platform for small security teams: ingest Timesketch-compatible timelines at scale, explore them through an ELK-like web interface, and detect anomalies by embedding every log line into a vector database.

## 2. Problem Statement
Incident responders and forensic analysts work with massive timeline-shaped datasets (Plaso output, Windows Event Logs, endpoint telemetry, cloud audit trails). Existing options force a choice between:
- **Full SIEMs** that are expensive, noisy, and not timeline-investigation native.
- **Notebook scripts** that are flexible but not reproducible or team-friendly.
- **Timesketch** which is powerful but operationally heavy and broad.

TraceVector is a focused, self-hosted alternative: ingest huge logs, explore them like an ELK stack, and let local embeddings surface the needles in the haystack.

## 3. Target User
**Small security team (2–10 analysts), self-hosted, often airgapped.**
- Runs on the team's own hardware or a private cloud.
- Needs forensic rigor: reproducible processing, immutable source data, audit-friendly outputs.
- Wants minimal operational complexity and no mandatory external services.

## 4. Core Value Proposition
- **Large-scale ingestion**: Process tens of gigabytes of Timesketch-compatible timeline data (CSV, JSONL, Plaso) without exhausting memory.
- **ELK-like exploration**: Search, filter, time-range zoom, and annotate events through a responsive web UI.
- **Vector-backed anomaly detection**: Embed each log line locally, store vectors in Qdrant, and expose outliers + semantic similarity search to the analyst.
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
| **Annotation** | A comment, tag, or highlight attached to one or more Events. Origin is `user` or `system`. |

## 6. Proposed MVP Feature Set

### 6.1 Ingestion (CLI-first, like ScalarForensic)
- Ingest directories or single files in Timesketch-compatible formats:
  - Plaso CSV / JSONL
  - Generic CSV with configurable column mapping
  - Generic JSONL (one event per line)
- Streaming parser: handle 80 GiB+ inputs without loading everything into RAM.
- Per-event SHA-256 hash and provenance metadata (source file, byte offset, parser config).
- Optional deduplication by hash or by (file path + offset).
- Parallel embedding of event text into a local sentence-transformer / log-specific model.
- Upsert events and vectors into the primary store and Qdrant in batches.

### 6.2 Storage & Vector Backend
- Primary event store: a fast column-oriented or relational store (tech stack TBD in next step).
- Vector store: **Qdrant** (consistent with ScalarForensic, supports local disk and airgapped deployment).
- One Qdrant collection per case, or per timeline, with deterministic naming.
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

### 6.4 Anomaly & Similarity Panel
- Run unsupervised scoring over event embeddings:
  - Outlier detection (distance/density based on Qdrant neighbors).
  - Rare-cluster highlighting.
- Semantic similarity search: paste or select an event and find the most similar log lines.
- Explain scores by showing nearest neighbors and distance metrics.

### 6.5 Deployment & Operation
- TraceVector is a native Python application managed with `uv`.
- Backing services (PostgreSQL, ClickHouse, Qdrant) are external; the operator provides them via Docker, native packages, managed services, etc.
- Optional reference `docker-compose.yml` for one-command setup.
- Airgapped mode by default: no outbound network calls for model downloads or telemetry.
- Optional `--allow-online` flag for first-time model download, mirroring ScalarForensic.
- Simple multi-user auth (basic or OIDC) for team access.
- Optional GPU acceleration: AMD ROCm 6.4 primary, NVIDIA CUDA 12.8 secondary; CPU is the default.

## 7. Explicitly Out of Scope for MVP
- Real-time streaming ingestion / continuous monitoring.
- SIEM-style alerting / correlation rules engine.
- Graph/link analysis visualizations.
- Rich-text story/report builder.
- Pluggable analyzer marketplace.
- SaaS multi-tenancy, billing, or managed hosting.

## 8. Differentiation
- **Scale + simplicity**: Designed from the start for 80 GiB+ timelines while staying container-first and easy to operate.
- **Embedding-native investigation**: Vectors are not an afterthought; they power anomaly detection and semantic search inside the same UI used for filtering.
- **Forensic rigor by default**: Immutable sources, provenance metadata, model-config stability checks, offline-first operation.

## 9. Success Criteria for the MVP
- Install with `uv sync` on Python >=3.13 in < 15 minutes (assuming backing services are available).
- Ingest an 80 GiB timeline file on commodity hardware without OOM.
- Open a case, run a full-text filter, and add an annotation in < 30 seconds.
- Compute embeddings and surface the top-N anomalous events without leaving the app.
- Run fully offline after initial model download.

## 10. Tech-Stack Selection (resolved)

Decisions are recorded in [`docs/TECH_STACK.md`](./TECH_STACK.md):

1. ✅ Primary event store: **ClickHouse**.
2. ✅ Backend language/framework: **Python 3.13 + FastAPI/Uvicorn**.
3. ✅ Frontend stack: **React 19 + Vite 8 + TypeScript** (Zustand + TanStack Query/Table/Virtual).
4. ✅ Embedding model: general sentence-transformer baseline (`all-MiniLM-L6-v2`), with a swappable registry and an OpenAI-compatible remote endpoint option.
5. ✅ Deployment target: single-node Docker Compose reference deployment; application runs via `uv`.

## 11. Next Steps
1. ✅ Finalize and approve this concept.
2. ✅ Produce an architecture/tech-stack decision record.
3. ✅ Implement the Case / Source / Timeline / Artifact model refactor.
4. ✅ Rebuild the frontend UI (React 19 + Vite).
5. ⬜ Implement authentication.
6. ⬜ Implement strict offline-mode enforcement (`allow_online` flag exists but is not yet checked anywhere).
