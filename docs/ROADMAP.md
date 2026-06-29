# TraceVector Roadmap

This document tracks the agreed scope for the TraceVector API and UI.

> **Next major phase:** Model refactor (Case / Source / Timeline / Artifact vocabulary).
> Full design in [`docs/MODEL_REFINEMENT.md`](./MODEL_REFINEMENT.md).
> The items below are scoped to the **current** (pre-refactor) model.

## Out of scope

The following Timesketch features are **not** planned for this phase:

- Stories
- Scenarios / DFIQ
- Graph view
- Analyzers
- Sigma rules
- LLM integration
- Threat intel

## In scope

### 1. Richer Explore view ✅ (mostly done)

The main investigation screen should feel like a real forensic timeline explorer:

- ✅ **Event details inline panel** — single-row expand showing the full event: message, timestamp, timestamp_desc, source, source_long, display_name, tags, and all attributes.
- ✅ **Tag / comment annotations**
  - Multi-select events.
  - "Tag" and "Comment" actions apply to all selected events.
  - Backend: `Annotation` model in PostgreSQL; endpoints for per-event GET/POST/DELETE plus a bulk GET for table chips.
- ✅ **Saved views that actually persist**
  - Backend: `View` model in PostgreSQL; GET/POST/DELETE `/api/cases/{id}/views`.
- ✅ **Export CSV / JSONL**
  - Backend: `POST /api/cases/{case_id}/timelines/{timeline_id}/export`
    accepting `format` and filter params; streams all matching events in batches.

### 2. Real column filtering ✅

- The backend `/events` endpoint supports `q`, `source`, `tag`, `start`, `end`,
  plus arbitrary field equality/exclusion filters via `filters` and `exclusions`
  JSON query params.

### 3. Time visualization

- Backend: add `GET /api/cases/{case_id}/timelines/{timeline_id}/histogram`
    returning bucket counts by time.

### 4. Anomaly / similarity panel ✅

- ✅ `GET /api/cases/{case_id}/timelines/{timeline_id}/events/{event_id}/similar`
- ✅ `GET /api/cases/{case_id}/timelines/{timeline_id}/anomalies`
- ✅ `POST /api/cases/{case_id}/timelines/{timeline_id}/anomalies/tag`
- Algorithm: distance-to-centroid (O(1) ANN query); honest "triage, not threat detection" framing.

### 5. Case / timeline management

- ✅ **Delete timeline** — `DELETE /api/cases/{case_id}/timelines/{timeline_id}`.
- ✅ **Delete case** — `DELETE /api/cases/{case_id}` (cascade delete timelines).

## Implementation order

1. ✅ Real column filtering (include/exclude on fields and attributes).
2. ✅ Fix create-timeline refresh bug (quick win).
3. ✅ Add case/timeline delete endpoints.
4. ✅ Persisted saved views + backend endpoints.
5. ✅ Tag/comment annotations + backend endpoints.
6. ✅ Export CSV/JSONL + backend endpoint.
7. Time visualization histogram endpoint.
8. ✅ Anomaly/similarity endpoints wired to Qdrant.

## Notes

- All new backend endpoints should follow the existing FastAPI router pattern
  in `src/tracevector/api/routers/`.
- Frontend tech stack is TBD — the redesign is driven by the API endpoints above.
