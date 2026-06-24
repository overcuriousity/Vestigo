# TraceVector Implementation Progress

Last updated: 2026-06-24

This document tracks implementation progress against the MVP defined in
[`CONCEPT.md`](./CONCEPT.md) and the tech-stack decisions in
[`TECH_STACK.md`](./TECH_STACK.md).

## Overall completion

**Estimated MVP completion: 60–70 %**

## MVP feature checklist

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | **Ingestion (CLI-first)** | ✅ Done | Streaming CSV/JSONL parsers, `tv ingest` CLI, plus web-based drag-and-drop upload. |
| 2 | **Storage & Vector Backend** | ✅ Done | ClickHouse `events` table with token-bloom full-text index; Qdrant collections with embedding-config-hash isolation and vector-size config-match checks. |
| 3 | **Web UI (ELK-like investigation interface)** | 🟡 Core done | Case/timeline management, file upload, event table, full-text search, source/tag filters, and pagination. Missing: saved views, annotations/tags on events, CSV/JSONL export. |
| 4 | **Anomaly & Similarity Panel** | ❌ Not started | Outlier detection, rare-cluster highlighting, and semantic similarity search are not yet implemented. |
| 5 | **Deployment & Operation** | 🟡 Partial | Reference `docker-compose.yml`, `uv` workflow, environment-based config. Missing: authentication, GPU index selection, strict offline-mode guard for model downloads. |

## Completed architectural decisions

- ✅ Language & packaging: Python 3.13 + `uv`
- ✅ Web backend: FastAPI + Uvicorn
- ✅ CLI ingestion: Typer
- ✅ Frontend: Vue 3 + Vite + Vuetify
- ✅ Metadata store: PostgreSQL (async SQLAlchemy)
- ✅ Event store: ClickHouse
- ✅ Vector store: Qdrant (tested with v1.18.2)
- ✅ Embedding runtime: sentence-transformers (`all-MiniLM-L6-v2` baseline)

## Known gaps / next logical steps

1. **Event annotations** — add tags/comments to one or more selected events.
2. **Saved views** — persist filter sets per case/timeline.
3. **Export** — download filtered or full event sets as CSV/JSONL.
4. **Anomaly panel** — use Qdrant nearest-neighbor search to surface outliers and enable semantic similarity search.
5. **Authentication** — basic user auth for team access.
6. **Offline-mode enforcement** — prevent HuggingFace network calls when `allow_online=false`.
