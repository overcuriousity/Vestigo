# TraceVector Frontend Roadmap

This document captures the agreed scope for evolving the TraceVector web UI.
It replaces the broader "adapt all of Timesketch" idea with a focused,
backend-achievable set of features.

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

The main investigation screen (`TimelineDetailView`) should feel like a real
forensic timeline explorer:

- ✅ **Event details inline panel** — clicking anywhere on a row expands it
  in-place showing the full event: message, timestamp, timestamp_desc, source,
  source_long, display_name, tags, and all attributes. Persistent chevron icon;
  single-row expand (others collapse). Column visibility picker for optional
  columns.
- ✅ **Tag / comment annotations**
  - Multi-select events in the table.
  - "Tag" and "Comment" toolbar actions apply to all selected events.
  - Backend: `Annotation` model in PostgreSQL; endpoints for per-event
    GET/POST/DELETE plus a bulk GET for table chips.
  - Frontend: user-tag chips (secondary colour + account-tag icon) and comment
    indicator in the table row; full CRUD in the expanded detail panel.
- ✅ **Saved views that actually persist**
  - Backend: `View` model in PostgreSQL; GET/POST/DELETE `/api/cases/{id}/views`.
  - Frontend: saved views left panel, apply on click, save current filter set,
    delete affordance.
- **Export CSV / JSONL**
  - Backend: `POST /api/cases/{case_id}/timelines/{timeline_id}/export`
    accepting `format` and the current `FilterState`.
  - Frontend: "Export" button in the event table toolbar (stub wired, endpoint pending).

### 2. Real column filtering ✅

Add Timesketch/ELK-style filter chips/buttons per column in the event table:

- Clicking a value in the **Source**, **Tag**, or attribute columns adds a
  filter chip for that value.
- A filter bar shows active chips and allows removing them individually or
  clearing all.
- The backend `/events` endpoint supports `q`, `source`, `tag`, `start`, `end`,
  plus arbitrary field equality/exclusion filters via `filters` and `exclusions`
  JSON query params (e.g. `display_name`, `timestamp_desc`, attribute keys).
- Event detail panel shows filter/include, exclude, and copy icons next to each
  field and attribute.

### 3. Time visualization

Add a histogram/timeline chart above the event table:

- Bucket events by time (auto-resolution based on range: hour, day, week).
- Clicking a bucket zooms the table to that time range.
- Use `vue3-apexcharts` or Vuetify chart components.
- Backend: add `GET /api/cases/{case_id}/timelines/{timeline_id}/histogram`
    returning bucket counts.

### 4. Anomaly / similarity panel

Turn the current stub panel into a working feature:

- Backend: expose Qdrant nearest-neighbor search:
  - `GET /api/cases/{case_id}/timelines/{timeline_id}/events/{event_id}/similar`
  - `GET /api/cases/{case_id}/timelines/{timeline_id}/anomalies`
- Frontend panel:
  - "Find similar events" for the selected event.
  - "Find outliers" across the timeline.
  - Render results in a compact list with similarity scores.

### 5. Case / timeline management

- **Delete timeline** from a case.
  - Backend: `DELETE /api/cases/{case_id}/timelines/{timeline_id}`.
  - Frontend: action in the timeline list on `CaseDetailView`.
- **Delete case**.
  - Backend: `DELETE /api/cases/{case_id}` (cascade delete timelines).
  - Frontend: action on `CaseListView` / `CaseDetailView` with confirmation.
- **Fix create-timeline refresh bug**
  - Currently the timeline list only updates after a second reload.
  - Ensure `createTimeline` awaits the backend response and then refetches
    the timeline list before the router navigation or view refresh.

## Implementation order

1. ✅ Real column filtering (include/exclude on fields and attributes).
2. ✅ Fix create-timeline refresh bug (quick win).
3. ✅ Add case/timeline delete endpoints and UI.
4. ✅ Persisted saved views + backend endpoints.
5. ✅ Tag/comment annotations + backend endpoints.
6. ✅ Event table UX — single-row expand on click, persistent chevron, column picker.
7. Export CSV/JSONL + backend endpoint.
8. Time visualization histogram.
9. Anomaly/similarity panel wired to Qdrant.

## Notes

- All new backend endpoints should follow the existing FastAPI router pattern
  in `src/tracevector/api/routers/`.
- All new frontend components should keep the Vue 3 + Vuetify 3 + TypeScript +
  Pinia stack already in place.
