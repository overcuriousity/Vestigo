# TraceVector — Model Refinement: Case / Source / Timeline / Artifact

> **Status:** Approved design, **implementation complete** (2026-06-29).
> This supersedes the vocabulary in §5 of `CONCEPT.md` and the old "timeline = one file"
> framing. The backend, tests, and frontend types now follow the definitions below.

---

## Why this change

The original concept (and the Timesketch vocabulary we borrowed) defined a **Timeline** as
*"a single imported data source."* That created two problems that have become blocking as the
product matures:

1. **The name contradicts the vision.** TraceVector's core proposition is *"correlating and
   analyzing logs from different sources against each other in **one singular timeline**."*
   Naming the import unit "Timeline" means there is no word left for the unified correlated
   view — and indeed the merged view has never been built.

2. **"Source" is critically overloaded.** It refers simultaneously to (a) the uploaded
   dataset (a Timeline in current code) and (b) the per-event Plaso field `source`/`source_long`
   (values like `LOG`, `WEBHIST`, `FILE`) surfaced as a column and filter in the Explorer.
   This confusion runs through every layer: the UI, the API, the ClickHouse schema, and the
   embedding wizard.

The current code already partially contradicts its own naming: the `timeline_uploads` table
is a 1-to-many relationship (multiple distinct files can be uploaded into one timeline), yet
every docstring and UI label says "a timeline holds a single file." The model below aligns
vocabulary with reality and with the product vision.

---

## Refined model

### Concept table (replaces CONCEPT.md §5)

| Concept | Definition | Replaces |
|---|---|---|
| **Case** | An investigation container (e.g. "Compromised endpoint ACME-123"). Unchanged. | `Case` |
| **Source** | One ingested file in a case. The atomic unit of ingestion, hashing, provenance, and forensic immutability. Ingested once; may belong to many Timelines. | Promoted from `TimelineUpload` / old `Timeline` |
| **Timeline** | A **named grouping of 1..N Sources** — the correlated chronological view across those sources, merged on one time axis, color-coded per source, with per-source visibility toggles. The implicit "all sources" view is the default Timeline for a case. | Repurposed from old `Timeline` |
| **Event** | One record, scoped by `source_id` (which file it came from), stamped with its **Artifact** type. The atomic unit of filtering, annotation, embedding, and anomaly detection. | `Event` (rescoped from timeline_id to source_id) |
| **Artifact** | The per-event Plaso artifact class and its long description (e.g. `LOG` / `Syslog line`, `WEBHIST` / `Firefox history`, `FILE` / `File stat`). The forensic type of the event. | Renamed from `source` / `source_long` |
| **Embedding** | A dense vector representation of an event's textual content, produced by a local model. Configuration (model, field selection) lives on the Source. | `Embedding` |
| **Vector Collection** | A Qdrant collection holding event embeddings, keyed by `(case_id, source_id, embedding_config_hash)`. | `Vector Collection` (rescoped) |
| **View** | A saved set of filters (time range, full-text, artifact, source toggle, field values) applied to a Timeline. | `View` |
| **Annotation** | A comment, tag, or highlight attached to one or more Events. Origin is either `user` or `system` (machine-generated outlier). | `Annotation` |

### Relationship summary

```
Case (1)
  └── Source (N)          ← one ingested file, hashed once, immutable
        └── Event (M)     ← scoped by source_id, has artifact/artifact_long

Case (1)
  └── Timeline (N)        ← named grouping; one per case by default ("all sources")
        └── Source (M)    ← many-to-many via timeline_sources join table
              (a Source may belong to multiple Timelines)

Timeline → merged Explorer view
  ├── Source A events  ──  color stripe A, toggle A
  ├── Source B events  ──  color stripe B, toggle B
  └── ...
```

### Why source-scoped events

Because a Source can belong to multiple Timelines, events **must be stored once per Source**
(keyed by `source_id`) to avoid duplication. A Timeline query resolves its member source IDs
and issues a single `source_id IN (…)` predicate — already supported by the optional scope
in `db/queries.py:18`, just not yet exposed by any endpoint.

---

## Forensic integrity framing

**Provenance lives on the Source, not the Timeline.** A Source is the evidence unit and carries:

- `file_hash` — SHA-256 of the original file (computed by `ingestion/files.py:hash_file`).
- `filename` — original filename as uploaded (not the temp path).
- `size_bytes` — original file size.
- `parser` + `parser_version` — exact processing configuration, itself fingerprinted.
- `ingest_time` — UTC timestamp of ingestion.
- `created_by` — analyst who uploaded (to be populated once auth is in place).
- `event_count` — number of events ingested from this file.

Each Event additionally carries `content_hash` (SHA-256 of the raw record), `byte_offset`,
and `line_number` so it can be located in the original file.

**The Timeline is a derived, non-authoritative projection.** It is a view over immutable
Source events — sorting, filtering, and coloring them — and does not itself constitute
evidence. Analysts should always trace findings back to the Source and its `file_hash`.

### Known gaps resolved by the refactor

| Gap | Location | Status |
|---|---|---|
| Original source file deleted after hashing | `api/routers/cases.py` | ✅ Implemented. Files are retained content-addressed under `data/sources/{hash[:2]}/{hash}`; `GET /api/cases/{case_id}/sources/{source_id}/download` re-downloads the original. |
| Naive timestamps silently assumed UTC | `models/event.py:_parse_timestamp` | ✅ Implemented. Naive/unqualified timestamps are assumed UTC with a `UserWarning`; per-source timezone config remains a future enhancement. |
| CSV export omits forensic columns | `api/routers/events.py:_CSV_COLUMNS` | ✅ Implemented. Exports now include `source_id`, `artifact`, `artifact_long`, `content_hash`, and `file_hash`. |
| CLI `file_hash` fallback to line hash | `ingestion/parser.py` | ✅ Implemented. `Event._derive_id` and parsers now require a real file hash; ingestion raises `ValueError` if one is not supplied. |

### Remaining gaps (out of refactor scope)

| Gap | Location | Notes |
|---|---|---|
| `created_by` never populated | `api/routers/cases.py:upload_source` | Blocked on authentication implementation. |

---

## Backend implementation status

Key files changed: `db/postgres.py`, `db/clickhouse.py`, `db/queries.py`, `db/qdrant.py`,
`db/similarity.py`, `models/event.py`, `ingestion/{parser,pipeline,files}.py`,
`api/routers/{cases,events,jobs}.py`, `cli/main.py`.

| Step | Status | Notes |
|---|---|---|
| 1. Promote `Source` in Postgres | ✅ Done | `Source` table with `(case_id, file_hash)` unique constraint; `embedding_model`/`embedding_config` live on the Source. |
| 2. Timeline grouping | ✅ Done | `Timeline` is a metadata row; `timeline_sources` M:N join; default "All sources" timeline created per case and lazily populated. |
| 3. Rescope events in ClickHouse | ✅ Done | Columns renamed to `source_id`, `artifact`, `artifact_long`; ordering/partitioning keys updated. |
| 4. Event identity | ✅ Done | `_derive_id` uses `source_id`; field names renamed. Old vector/event IDs are discarded (model reset). |
| 5. Query layer | ✅ Done | `EventQuery.source_ids`, `artifact` filter, `source_id` filter; Qdrant payloads/filters use `source_id`. |
| 6. Ingestion | ✅ Done | Parsers emit `artifact`/`artifact_long`; upload creates a `Source` and auto-adds it to the default timeline. |
| 7. Routes | ✅ Done | `/sources` namespace split from `/timelines`; query endpoints still hang off timelines but resolve to source IDs. |
| 8. CLI | ✅ Done | `tv ingest --source` creates a Source; `tv embed --source` generates vectors. |

---

## Frontend implementation status

Key files changed: `frontend/src/api/types.ts` + API clients, Explorer components,
FilterRail, EventGrid, EventDetailPanel, and routing.

| Step | Status | Notes |
|---|---|---|
| 1. Contract (`types.ts`) | ✅ Done | `Source` and `Timeline` interfaces updated; `Event` uses `source_id`, `artifact`, `artifact_long`; filters include `artifact` and `source_id`. |
| 2. Case Overview page | ✅ Done | Source list/upload and timeline creation are full React pages (`components/cases/`, `components/timelines/`), built out during the React 19 + Vite frontend rebuild. |
| 3. Explorer — merged timeline view | ✅ Done | Event grid renamed the old "Source" column to "Artifact"; source legend/toggle and color-stripe hooks are wired through the updated types and API. |
| 4. Routing | ✅ Done | Explorer route remains `/cases/:caseId/timelines/:timelineId`; source management routes and breadcrumbs use names where available. |

> **Note:** This table predates the frontend rebuild. The frontend stack was resolved
> (React 19 + Vite + TypeScript, see `TECH_STACK.md`) and is now fully built out — see
> `docs/PROGRESS.md` for current frontend completeness.

---

## Verification results

- **Unit/integration tests.** ✅ `uv run pytest tests -q` — 80 passed.
- **Lint.** ✅ `uv run ruff check src tests` — all checks passed.
- **Frontend typecheck.** ✅ `cd frontend && npm run typecheck` — passed.
- **End-to-end (manual).** ✅ Superseded by ongoing real usage — the Explorer, embed wizard,
  and anomaly panels have since been exercised repeatedly against multi-source cases and a
  live 3.4M-event sample dataset during later feature and review passes (see
  `docs/PROGRESS.md`), covering the merged view, per-source toggles, and Artifact column this
  item asked to verify.

---

## Reference: how Timesketch handles this

| Timesketch | Meaning | TraceVector equivalent (new) |
|---|---|---|
| Sketch | Investigation container | Case |
| Timeline | One import / one index | **Source** |
| Explore view | All timelines merged, color-coded, toggleable | **Timeline** (Explorer) |
| `data_type`, `source_short`/`source_long` | Per-event Plaso artifact class | **artifact** / **artifact_long** |

Timesketch avoids the overload by never calling the per-event field "source" in its UI — the
Plaso columns read as type/category. We adopt the same split: Source = the file you ingested;
Artifact = what kind of log record each event is.
