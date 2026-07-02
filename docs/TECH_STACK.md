# TraceVector — Tech Stack Decision Record

## 1. Guiding Principles
- **Local-first / airgap-friendly**: No mandatory cloud services; models download once and run offline.
- **Container-first, low ops overhead**: Single-node Docker Compose deployment for small teams.
- **Python-native ML**: Reuse the ecosystem that already works for ScalarForensic (PyTorch, transformers, Qdrant).
- **Swappable embedding models**: Design the pipeline so a general model ships first and a log-specific model can be dropped in later.

## 2. Proposed Stack

| Layer | Choice | Version / Notes |
|-------|--------|-----------------|
| **Language & packaging** | Python | 3.13, managed with `uv` (3.14 support planned as deps mature) |
| **Web backend** | FastAPI + Uvicorn | Async API server; same stack as ScalarForensic web UI |
| **CLI ingestion** | Typer + Python stdlib | `tv ingest ...` command, streaming parser |
| **Frontend** | React 19 + Vite 8 + TypeScript | Zustand (client state) + TanStack Query/Table/Virtual (server state, grid); served as a static build from `frontend/dist`, API-first backend |
| **Metadata store** | PostgreSQL | Cases, sources, timelines, timeline-source membership, views, annotations, users |
| **Event store** | ClickHouse | Columnar log store for 80 GiB+ filtering and aggregation |
| **Vector store** | Qdrant | Embeddings + neighbor search; local disk mode supported |
| **Embedding runtime** | sentence-transformers + ONNX Runtime | Local inference, CPU-friendly, optional GPU |
| **Task queue (later)** | Celery + Redis | Only if background ingestion/analytics jobs become necessary |
| **Deployment** | Native `uv` workflow | Application runs via `uv`; databases are external services provided by the operator |

## 3. Rationale by Layer

### 3.1 Backend — Python + FastAPI
- **Python 3.13** is the active target; `requires-python` is pinned to `>=3.13,<3.14` until upstream wheels (especially PyTorch CPU) reliably support 3.14.
- Aligns with ScalarForensic and the broader ML/Python tooling ecosystem.
- FastAPI gives async request handling and auto-generated OpenAPI docs with minimal boilerplate.
- `uv` provides fast dependency resolution and lockfiles; supports PyTorch ROCm/CUDA index overrides.
- CPU-only PyTorch is the default in `pyproject.toml` (`tool.uv.index`/`tool.uv.sources`), so
  `uv sync` works out of the box on any machine with no GPU-specific setup — this is the
  right choice for evaluation and for deployments that don't run the embedding pipeline.
  GPU acceleration is opt-in and is the recommended path for **production use of the
  embedding features** (`tv embed`, semantic search, similarity) — embedding large
  timelines on CPU is significantly slower:
  - **AMD ROCm 6.4** is the primary GPU target (mirrors ScalarForensic).
  - **NVIDIA CUDA 12.8** is also supported.
  - To switch, uncomment the matching index block in `pyproject.toml` and comment out the
    CPU block, then `uv lock && uv sync`. See the comments in `pyproject.toml` for the
    caveats (`explicit = true` on every index; ROCm needs `pytorch-triton-rocm` added as a
    direct dependency since it's a transitive-only dep of `torch`).

### 3.2 Frontend — React 19 + Vite

Resolved. The backend exposes a complete REST API (`/api/docs`); the frontend (`frontend/`)
is a React 19 + Vite 8 + TypeScript SPA, using Zustand for client state, TanStack Query for
server state, and TanStack Table/Virtual for the event grid. It builds to `frontend/dist`,
which `tv-web` serves directly (auto-built on first run if missing).

### 3.3 Metadata Store — PostgreSQL
- External service, provided by the operator.
- Chosen over SQLite because the target user is a **team** (2–10 analysts).
- PostgreSQL handles concurrent writers, transactions for annotations/views, and user auth reliably.
- SQLite with WAL mode would work for a single-user desktop tool, but becomes a concurrency bottleneck here.

### 3.4 Event Store — ClickHouse
- Chosen for its strength with log-shaped data: columnar compression, fast time-range scans, and built-in full-text indexing (`tokenbf_v1`).
- An 80 GiB source compresses well and filters quickly on modest hardware.
- **Deployment note**: ClickHouse is an external service. TraceVector connects to it over HTTP/TCP; it is never inside the application package or container.

### 3.5 Vector Store — Qdrant
- Already proven in ScalarForensic for forensic vector search.
- Runs as an external service; also supports a local/embedded mode via the Python client for single-user deployments.
- Airgapped operation and efficient neighbor search.
- One collection per `(case_id, embedding_config_hash)` keeps isolation simple; source-level filtering is done via Qdrant payload filters on `source_id`. A case can have multiple collections if different embedding models or field selections are used.

### 3.6 Embedding Runtime — sentence-transformers + ONNX
- sentence-transformers provides a broad set of ready-to-use local models (e.g. `all-MiniLM-L6-v2`) that give a strong baseline for log-line similarity.
- ONNX Runtime reduces dependencies and improves CPU inference speed over raw PyTorch for embedding-only workloads.
- The pipeline is model-agnostic: any model that produces a fixed-size vector can be registered via config, enabling the "both, swappable" goal.

## 4. Deployment Model — Application vs. Services

TraceVector itself is **only the Python application**. The databases are external services that the operator provides, exactly like ScalarForensic expects an external Qdrant.

```
┌─────────────────────────────────────────┐
│         TraceVector application         │
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

TraceVector only needs connection strings.

### 4.2 Optional reference Docker Compose
For convenience, a `docker-compose.yml` is provided that launches all three backing services. The TraceVector app itself still runs via `uv run tv-web` against those services. This is a reference deployment, not a requirement.

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

- All model downloads happen once via an `--allow-online` flag during first setup.
- After download, the app blocks HuggingFace and other external network calls at runtime (mirroring ScalarForensic).
- Model weights can be pre-bundled for fully offline deployment.
- Docker images can also be pre-bundled, but Docker is not required for airgapped use.
- Telemetry and cloud APIs are disabled by default and not required.

## 7. Out of Scope for This Stack

- Kubernetes manifests (can be added later).
- Managed cloud database services.
- Real-time streaming ingestion infrastructure.

## 8. Open Implementation Decisions

1. ✅ Exact default embedding model and vector dimension — default is `all-MiniLM-L6-v2` (384-dim), swappable via config.
2. ✅ ClickHouse table schema — implemented; `events` table uses `tokenbf_v1` for full-text and is partitioned by `(case_id, source_id)`.
3. ✅ Frontend tech stack — React 19 + Vite, served as a static build directly from Uvicorn (no Nginx sidecar).
4. ⬜ Authentication backend: local users vs. OIDC.

## 9. Completed Steps

- ✅ Tech stack approved and recorded.
- ✅ Project skeleton (`uv`, FastAPI, Docker Compose reference) implemented.
- ✅ Ingestion CLI prototype implemented and refactored to the Source/Timeline model.
- ✅ Backend API complete for the current MVP scope.
- ✅ Frontend built (React 19 + Vite + TypeScript) and wired to the full API surface.
- ✅ Statistical anomaly engine (`value_novelty` + `frequency` detectors) added, replacing
  the original embedding-distance-only anomaly panel described in §5.

## 10. Next Steps

1. ⬜ Implement authentication (local or OIDC) — nothing built yet.
2. ⬜ Implement strict offline-mode enforcement — `allow_online` flag exists
   (`core/config.py`) but is not checked at any network call site yet.
3. ⬜ GPU acceleration (ROCm/CUDA) — still aspirational; no GPU-specific code exists.
