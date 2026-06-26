# TraceVector Implementation Progress

Last updated: 2026-06-26 (session 4)

This document tracks implementation progress against the MVP defined in
[`CONCEPT.md`](./CONCEPT.md) and the tech-stack decisions in
[`TECH_STACK.md`](./TECH_STACK.md).

## Overall completion

**Estimated MVP completion: 80 %**

## MVP feature checklist

| # | Feature | Status | Notes |
|---|---------|--------|-------|
| 1 | **Ingestion (CLI-first)** | ✅ Done | Streaming CSV/JSONL parsers, `tv ingest` CLI, plus web-based drag-and-drop upload. |
| 2 | **Storage & Vector Backend** | ✅ Done | ClickHouse `events` table with token-bloom full-text index; Qdrant collections with embedding-config-hash isolation and vector-size config-match checks. |
| 3 | **Web UI (ELK-like investigation interface)** | 🟡 Core done | Timesketch-v3-inspired shell with search chips, source/tag filters, time-range, field-level include/exclude filters, selectable event table (✅ column picker), single-row click-to-expand detail (✅), persisted saved views (✅), case/timeline delete (✅), tag/comment annotations (✅). Export endpoint still pending. |
| 4 | **Anomaly & Similarity Panel** | 🟡 UI stubbed | Frontend anomaly panel and similarity search UI added; backend endpoints (`/anomalies`, `/similar`) still need Qdrant nearest-neighbor implementation. |
| 5 | **Deployment & Operation** | 🟡 Partial | Reference `docker-compose.yml` with fully-qualified image names (podman-compatible), `uv` workflow, environment-based config. Missing: authentication, GPU index selection, strict offline-mode guard for model downloads. |

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

1. ✅ **Event annotations** — `Annotation` model in PostgreSQL; `GET`/`POST`/`DELETE` per-event endpoints + bulk `GET /annotations` for table chips; tag chips (secondary colour + account-tag icon) and comment indicators in the event table; full annotation CRUD in the event detail panel; multi-select Tag/Comment toolbar actions wired end-to-end.
2. ✅ **Saved views** — `View` model in PostgreSQL; GET/POST/DELETE `/api/cases/{id}/views` endpoints; delete affordance in SavedViews panel.
3. ✅ **Event table UX** — `item-value="event_id"` fixes single-row expand; click anywhere on a row to expand/collapse its detail (skips chips/buttons); persistent chevron icon; column visibility picker (`mdi-view-column-outline`) for Time, Source, Message, Tags, Description, Display name.
4. ✅ **Podman compatibility** — `docker-compose.yml` updated to use fully-qualified `docker.io/…` image names; tested with podman-compose.
5. **Export** — download filtered or full event sets as CSV/JSONL.
6. **Anomaly panel** — use Qdrant nearest-neighbor search to surface outliers and enable semantic similarity search.
7. **Authentication** — basic user auth for team access.
8. **Offline-mode enforcement** — prevent HuggingFace network calls when `allow_online=false`.
9. ✅ **Case/timeline deletion** — `DELETE` endpoints with cascade across ClickHouse + Qdrant + PostgreSQL; confirmation UI on CaseDetailView and CaseList.
10. **Time visualization** — histogram endpoint and chart above the event table.
