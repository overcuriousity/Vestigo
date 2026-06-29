# TraceVector — Model Refinement: Case / Source / Timeline / Artifact

> **Status:** Approved design. Implementation not yet started (2026-06-29).
> This supersedes the vocabulary in §5 of `CONCEPT.md` and the "timeline = one file"
> framing throughout the current codebase. All subsequent implementation must follow
> the definitions here.

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

### Known gaps to resolve during implementation

| Gap | Location | Resolution |
|---|---|---|
| Original source file deleted after hashing | `api/routers/cases.py:220` | Implement content-addressed retention; add `GET /sources/{id}/download`. |
| Naive timestamps silently assumed UTC | `models/event.py:278,289,296` | Add per-source timezone config; warn when naive timestamps are coerced. |
| CSV export omits forensic columns | `api/routers/events.py:228` | Add `source_id`, `content_hash`, `file_hash`, `artifact` to `_CSV_COLUMNS`. |
| `created_by` never populated | `api/routers/cases.py:346` | Wire to auth identity once authentication is implemented. |
| CLI `file_hash` fallback to line hash | `ingestion/parser.py:118` | Fix: require a real file hash; reject or warn on fallback path. |

---

## Backend implementation plan

Key files to change: `db/postgres.py`, `db/clickhouse.py`, `db/queries.py`, `db/qdrant.py`,
`db/similarity.py`, `models/event.py`, `ingestion/{parser,pipeline,files}.py`,
`api/routers/{cases,events,jobs}.py`, `cli/main.py`.

1. **Promote Source (Postgres).** Migrate `TimelineUpload` → first-class `Source` table:
   add `name`, `size_bytes`, `created_by`; keep `file_hash`, `filename`, `parser`,
   `event_count`. Unique constraint on `(case_id, file_hash)`. Embedding field selection
   (`embedding_model`, `embedding_config`) moves from Timeline to Source.

2. **Timeline grouping (Postgres).** Redefine `Timeline` to hold only grouping metadata
   (`id`, `case_id`, `name`, `description`). Add `timeline_sources` join table
   (`timeline_id`, `source_id`). A "create case" operation auto-creates a default Timeline
   ("All sources") and populates it lazily as Sources are added.

3. **Rescope events (ClickHouse).** Rename column `timeline_id` → `source_id`;
   `ORDER BY (case_id, source_id, timestamp, event_id)`;
   `PARTITION BY (case_id, source_id)` (preserves instant `DROP PARTITION` on source delete).
   Rename `source` → `artifact`, `source_long` → `artifact_long`.

4. **Event identity (`models/event.py`).** `_derive_id` (`event.py:151`) swaps
   `timeline_id` → `source_id`; rename `source`/`source_long` fields →
   `artifact`/`artifact_long`. (IDs change vs. old data — acceptable; this is a model reset
   with no production data to migrate.)

5. **Query layer (`db/queries.py`).** `EventQuery.timeline_id` (`queries.py:18`) becomes
   `source_ids: list[str] | None`; a Timeline query resolves its sources first. Rename
   `source` filter → `artifact`; add `source_id` filter (per-source toggle). Update
   histogram, similarity, and anomaly queries to follow suit; update Qdrant payload key
   `timeline_id` → `source_id` in `db/qdrant.py`.

6. **Ingestion.** `parser.py` maps Plaso `source`/`source_long` → `artifact`/`artifact_long`.
   Upload endpoint (`cases.py:137`) writes a `Source` row instead of `TimelineUpload`;
   associates the source with its timeline(s). Fix `file_hash` fallback (`parser.py:118`).

7. **Routes.** Split the namespace:
   - `POST /api/cases/{case_id}/sources` — upload & ingest a file (returns Source).
   - `GET/DELETE /api/cases/{case_id}/sources/{source_id}` — provenance, re-download.
   - `GET/POST /api/cases/{case_id}/timelines` — list/create named groupings.
   - `POST /api/cases/{case_id}/timelines/{timeline_id}/sources/{source_id}` — add a source.
   - Query endpoints (`/events`, `/fields`, `/histogram`, `/export`, `/anomalies`,
     `/similar`) remain under `/timelines/{timeline_id}` but now resolve to source IDs.

8. **CLI.** `--timeline` → `--source`; `tv ingest` uploads to a Source (auto-adds to the
   case default Timeline).

---

## Frontend implementation plan

Key files: `api/types.ts` + `api/*.ts`, `router.tsx`, `pages/`,
`components/timelines/*` → split into `sources/` and `timelines/`,
`components/explorer/{FilterRail,EventGrid,EventDetailPanel}`,
`components/layout/TopBar.tsx`.

1. **Contract (`types.ts`).** Add `Source` interface; redefine `Timeline` as grouping with
   `source_ids`; rename `Event.source`/`source_long` → `artifact`/`artifact_long`;
   `EventFilters.source` → `artifact`, add `source_id` facet.

2. **Case Overview page.** Two sections:
   - **Sources** — upload (creates a Source), list with hash/size/parser/who, delete.
   - **Timelines** — create a named grouping, pick member sources from a checkbox list, open.

3. **Explorer = the merged timeline view.**
   - `EventGrid.tsx:378`: rename "Source" column → "Artifact".
   - Add a per-source **color stripe** on each row's left border and a source **legend**
     above the grid with on/off toggles (Timesketch-style). Reuse the existing left-border
     color mechanism (`EventGrid.tsx:573`).
   - `FilterRail.tsx:139`: rename "Source" filter → "Artifact"; add a **Source** facet
     listing member sources with counts and visibility toggle.
   - `EventDetailPanel.tsx:316`: rename "Source" section → "Artifact"; add a provenance
     row linking to the originating Source (original filename + SHA-256).

4. **Routing.** `/cases/:caseId/timelines/:timelineId` stays for the Explorer. Add source
   management routes. Fix breadcrumbs to show names, not raw UUIDs (`TopBar.tsx:46`).

---

## Verification plan

- **Unit/integration tests.** Ingest two different files as two Sources in one case; create a
  Timeline spanning both; assert a single `/events` query returns merged time-sorted events
  with the correct `source_id` and `artifact`. Assert a Source reused in two Timelines stores
  events exactly once. Run `uv run pytest`.
- **Rename integrity.** `rg -n "timeline_id|source_long|\bsource\b"` across `src/` and
  `frontend/src/` to catch stale references; `uv run ruff check`; `npm run typecheck`.
- **End-to-end (manual).** `docker compose up -d && uv run tv-web`. Upload two log files as
  Sources, build a Timeline from both, confirm the Explorer shows a merged timeline with
  per-source color stripes/toggles and an "Artifact" column. Verify Source provenance panel
  (hash, size, filename). If retention is implemented, re-download and SHA-256-verify against
  the original.

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
