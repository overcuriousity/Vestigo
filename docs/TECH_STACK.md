# Vestigo — Tech Stack Decision Record

## 1. Guiding Principles
- **Local-first / airgap-friendly**: No mandatory cloud services; models download once and run offline.
- **Low ops overhead**: single-node deployment. The app itself is one native `uv` process;
  the three backing services are external, with a reference compose file provided for them
  (an optional app container image exists too). One app process per instance is a real
  constraint — see [Operational scale](./DEPLOYMENT.md#operational-scale).
- **Python-native ML**: Reuse the proven local-inference ecosystem (PyTorch, sentence-transformers, Qdrant).
- **Swappable embedding models**: Design the pipeline so a general model ships first and a log-specific model can be dropped in later.

## 2. Stack

| Layer | Choice | Version / Notes |
|-------|--------|-----------------|
| **Language & packaging** | Python | 3.13, managed with `uv` (3.14 support planned as deps mature) |
| **Web backend** | FastAPI + Uvicorn | Async API server |
| **CLI ingestion** | Typer + Python stdlib | `vestigo ingest ...` command, streaming parser |
| **Frontend** | React 19 + Vite 8 + TypeScript | Zustand (client state) + TanStack Query/Table/Virtual (server state, grid); served as a static build from `frontend/dist`, API-first backend |
| **Metadata store** | PostgreSQL | Cases, sources, timelines, timeline-source membership, views, annotations, users; schema managed by Alembic |
| **Event store** | ClickHouse | Columnar log store for 80 GiB+ filtering and aggregation |
| **Vector store** | Qdrant | Embeddings + neighbor search; local disk mode supported |
| **Embedding runtime** | sentence-transformers + ONNX Runtime | Local inference, CPU-friendly, optional GPU |
| **Background jobs** | In-process `JobStore` (`core/jobs.py`) | In-memory and ephemeral by design — a deliberate choice for the single-process deployment model, not a placeholder for Celery |
| **Deployment** | Native `uv` workflow | Application runs via `uv`; databases are external services provided by the operator |

## 3. Rationale by Layer

### 3.1 Backend — Python + FastAPI
- **Python 3.13** is the active target; `requires-python` is pinned to `>=3.13,<3.14` until upstream wheels (especially PyTorch CPU) reliably support 3.14.
- Aligns with the broader ML/Python tooling ecosystem.
- FastAPI gives async request handling and auto-generated OpenAPI docs with minimal boilerplate.
- `uv` provides fast dependency resolution and lockfiles; supports PyTorch ROCm/CUDA index overrides.
- CPU-only PyTorch is the default in `pyproject.toml` (`tool.uv.index`/`tool.uv.sources`), so
  `uv sync` works out of the box on any machine with no GPU-specific setup — this is the
  right choice for evaluation and for deployments that don't run the embedding pipeline.
  GPU acceleration is opt-in and is the recommended path for **production use of the
  embedding features** (`vestigo embed`, semantic search, similarity) — embedding large
  timelines on CPU is significantly slower:
  - **AMD ROCm 6.4** is the primary GPU target.
  - **NVIDIA CUDA 12.8** is also supported.
  - To switch, uncomment the matching index block in `pyproject.toml` and comment out the
    CPU block, then `uv lock && uv sync`. See the comments in `pyproject.toml` for the
    caveats (`explicit = true` on every index; ROCm needs `pytorch-triton-rocm` added as a
    direct dependency since it's a transitive-only dep of `torch`).

### 3.2 Frontend — React 19 + Vite

Resolved. The backend exposes a complete REST API (`/api/docs`); the frontend (`frontend/`)
is a React 19 + Vite 8 + TypeScript SPA, using Zustand for client state, TanStack Query for
server state, and TanStack Table/Virtual for the event grid. It builds to `frontend/dist`,
which `vestigo-web` serves directly (auto-built on first run if missing).

### 3.3 Metadata Store — PostgreSQL
- External service, provided by the operator.
- Chosen over SQLite because Vestigo is a **multi-user** tool, whatever the headcount.
- PostgreSQL handles concurrent writers, transactions for annotations/views, and user auth reliably.
- SQLite with WAL mode would work for a single-user desktop tool, but becomes a concurrency bottleneck as soon as two analysts share an instance.

### 3.4 Event Store — ClickHouse
- Chosen for its strength with log-shaped data: columnar compression, fast time-range scans, and built-in full-text indexing (`tokenbf_v1`).
- An 80 GiB source compresses well and filters quickly on modest hardware.
- **Deployment note**: ClickHouse is an external service. Vestigo connects to it over HTTP/TCP; it is never inside the application package or container.

### 3.4a Converter Interchange Format — Parquet + Arrow
- Client-side converter scripts (`assets/converters/*2vestigo.py`) parse raw evidence
  logs locally and emit **Parquet** files rather than the CSV/JSONL the vendored
  `*2timesketch` scripts produce. The server bulk-inserts those files into ClickHouse via
  **Arrow** record batches (`db/_arrow_schema.py`, `insert_events_arrow`) instead of
  row-by-row parsing — an order of magnitude faster on multi-GB logs, since ClickHouse's
  native Arrow ingestion skips the CSV/JSON tokenize-and-cast step entirely.
- Columnar + typed: Parquet's schema (`ingestion/parquet_format.py::PARQUET_EVENT_SCHEMA`)
  carries real types (`timestamp`, `uint64`, `map<string,string>`) end to end, avoiding the
  string-serialize-then-reparse round trip CSV/JSONL forces on every field. Zstd-compressed
  Parquet is also considerably smaller on disk than the equivalent CSV.
- Forensic provenance is a first-class part of the format, not bolted on: every row carries
  the sha256 of its original raw evidence file (`file_hash`), the byte offset of the
  record within it, and the sha256 of the record itself (`content_hash`) — enough for an
  examiner to re-derive the same `event_id` from the raw log alone
  (`models/event.py::derive_event_id`). Footer metadata pins converter name/version.
- Converters are standalone downloads (no dependency on the `vestigo` package) and use
  `pyarrow` as their only non-stdlib dependency, so they still work disconnected from any
  Vestigo deployment — consistent with the airgapped-by-default goal.
- Not a replacement for CSV/JSONL ingestion: those paths (and the vendored stdlib-only
  `*2timesketch` scripts) remain fully supported as a minimal-dependency alternative for
  environments that can't install pyarrow.

### 3.5 Vector Store — Qdrant
- Proven for forensic vector search workloads.
- Runs as an external service; also supports a local/embedded mode via the Python client for single-user deployments.
- Airgapped operation and efficient neighbor search.
- One collection per `(case_id, embedding_config_hash)` keeps isolation simple; source-level filtering is done via Qdrant payload filters on `source_id`. A case can have multiple collections if different embedding models or field selections are used.
- **Optional, and honestly so.** Embeddings are not required to use Vestigo, and running without them is supported: clear `VESTIGO_QDRANT_URL` and `VESTIGO_QDRANT_PATH` and the `embeddings` capability is false, which removes every entry point rather than disabling one. Availability is *probed*, not inferred from configuration — Qdrant must answer `get_collections()`, and a configured remote embedding endpoint must answer a one-token embed — because `qdrant_url` has a non-null default, so configuration alone reports a vector store on every instance whether or not one is listening. The embedded on-disk mode (`VESTIGO_QDRANT_PATH`) is the one arm that stays a directory check rather than a call: qdrant-client locks the storage folder exclusively, and the app itself holds that lock whenever the similarity service or an embedding job is alive, so a probe client would either be locked out or lock a running job out. See `src/vestigo/models/availability.py`.

### 3.6 Embedding Runtime — sentence-transformers + ONNX
- sentence-transformers provides a broad set of ready-to-use local models (e.g. `all-MiniLM-L6-v2`) that give a strong baseline for log-line similarity.
- ONNX Runtime reduces dependencies and improves CPU inference speed over raw PyTorch for embedding-only workloads.
- The pipeline is model-agnostic: any model that produces a fixed-size vector can be registered via config, enabling the "both, swappable" goal.

## 4. Deployment Model — Application vs. Services

Vestigo itself is **only the Python application**. The databases are external services that the operator provides.

```
┌─────────────────────────────────────────┐
│         Vestigo application         │
│     (FastAPI + CLI tools + frontend)    │
│             runs via `uv`               │
└─────────────────────────────────────────┘
         │              │              │
         ▼              ▼              ▼
   PostgreSQL     ClickHouse       Qdrant
   (metadata)     (events)       (vectors)
   external       external       external
```

### 4.1 Operator-provided services
The operator starts PostgreSQL, ClickHouse, and Qdrant however they prefer:
- Official Docker images
- Native OS packages
- Managed database services
- Existing infrastructure

Vestigo only needs connection strings.

### 4.2 Optional reference Docker Compose
For convenience, a `docker-compose.yml` is provided that launches all three backing services. The Vestigo app itself still runs via `uv run vestigo-web` against those services. This is a reference deployment, not a requirement.

### 4.3 Single-user / airgapped shortcut
For a lone analyst on one machine, Qdrant can run in **local mode** through the Python client (no separate Qdrant process). PostgreSQL and ClickHouse still need a server, but this removes one dependency for simple deployments.

## 5. Embedding Model Strategy

### Phase 1 — General sentence-transformer
- Default: `all-MiniLM-L6-v2` (384-dim) or `all-mpnet-base-v2` (768-dim).
- Runs locally via ONNX.
- Provides semantic similarity and outlier detection immediately.

### Phase 2 — Optional log-specific model
- Evaluate models trained on log data (e.g. LogBERT-style, or domain-finetuned sentence-transformers).
- Add a model registry/config layer so users can select the model per case or timeline.
- Enforce config-match checks: model name, pooling, normalization, and vector dimension must match the collection; refuse to query mismatched collections.

## 6. Offline / Airgapped Operation

- All model downloads happen once with `VESTIGO_ALLOW_ONLINE=true` during first setup.
- With `VESTIGO_ALLOW_ONLINE=false` (the default), the embedding-model loader forces
  `HF_HUB_OFFLINE`/`TRANSFORMERS_OFFLINE` (`models/embeddings.py`) so no HuggingFace or
  other model-download call can leave the machine at runtime.
- Model weights can be pre-bundled for fully offline deployment (see the airgapped
  install procedure in `docs/DEPLOYMENT.md`).
- Docker images can also be pre-bundled, but Docker is not required for airgapped use.
- Telemetry and cloud APIs are disabled by default and not required.
- The Sigma runner's `pysigma` dependency transitively installs `requests` (pySigma uses it
  only for optional rule-collection fetching we never call); no Vestigo Sigma code path
  touches the network — rules come from the local `VESTIGO_SIGMA_RULES_PATH` drop and
  Postgres uploads.
- Exception: optional OIDC SSO (`VESTIGO_OIDC_ENABLED`) is intentionally independent of
  `VESTIGO_ALLOW_ONLINE`. It is operator-opted-in (off by default) and talks to an
  operator-configured IdP the analyst chose to trust — commonly reachable on the same LAN as
  an otherwise airgapped deployment — rather than an unconditional external call. An operator
  relying on `VESTIGO_ALLOW_ONLINE=false` as the single offline switch should be aware OIDC egress
  is a separate toggle (`VESTIGO_OIDC_ENABLED`).

## 7. Out of Scope for This Stack

- Kubernetes manifests (can be added later).
- Managed cloud database services.
- Real-time streaming ingestion infrastructure (streaming *ingest* is a roadmap
  milestone, but built on plain HTTP batch pushes — no Kafka-class infrastructure).
