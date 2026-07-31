# Vestigo — Model Refinement: Case / Source / Timeline / Artifact

> **Status:** Approved and implemented (2026-06-29). This defines the canonical
> Case / Source / Timeline / Event / Artifact vocabulary — the backend, tests, and
> frontend follow the definitions below. Read this before touching the data model.
>
> **Pending revision:** roadmap Milestone 7 (E1) proposes redefining **Artifact** as *a
> file* and renaming the per-event `artifact`/`artifact_long` columns to a type/kind
> concept, jointly with the stream-source model (S1). Until that design round lands and is
> approved, the definitions here stand as written.

---

## Why this change

The original concept (and the Timesketch vocabulary we borrowed) defined a **Timeline** as
*"a single imported data source."* That created two problems that have become blocking as the
product matures:

1. **The name contradicts the vision.** Vestigo's core proposition is *"correlating and
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
| **Embedding** | A dense vector representation of an event's textual content, produced by a local model. Configuration (model, field selection) lives on the **Timeline** (`timelines.embedding_model/embedding_config/embedding_config_hash`), set per embed run — not on the Source; see [Storage placement audit](#storage-placement-audit-2026-07-05) below. | `Embedding` |
| **Vector Collection** | A Qdrant collection holding event embeddings, keyed by `(case_id, embedding_config_hash)` — shared across all Sources in a case, not per-source. | `Vector Collection` (rescoped) |
| **View** | A saved set of filters (time range, full-text, artifact, source toggle, field values) applied to a Timeline. | `View` |
| **Annotation** | A `tag` or `comment` attached to one or more Events (`origin: user`), or an `anomaly` marker written by a detector run (`origin: system`). Analyst verdicts on findings are *not* annotations — they live in `finding_dispositions`, see `docs/ANOMALY_DETECTION.md`. Any annotated Event also carries the derived tag `annotated` (`ANNOTATED_TAG`), computed at read time from whether the Event has any annotation at all — it is never stored, so it cannot disagree with the annotations it describes. The frontend reads the tag's name from `/api/health` (`annotated_tag`) rather than mirroring it. Do not confuse the tag *value* `annotated` with the `annotated` filter *field* on `EventFilters`/`FilterSpec`, which selects by annotation **type** — `annotated=["tag"]` is "has a tag annotation", `tags_include=["annotated"]` is "carries the derived tag". | `Annotation` |

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
and issues a single `source_id IN (…)` predicate — implemented as the `source_ids` scope on
`EventQuery` (`db/queries.py`) and resolved per request by
`api/routers/events.py::_resolve_timeline_scope`.

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

**Vestigo Parquet interchange uploads** (converter-produced `.parquet`, see
`ingestion/parquet_format.py`) refine this split: the Source-level `file_hash` is the hash
of the uploaded parquet (retention/dedup as usual), while each Event's `file_hash`,
`byte_offset`, and `content_hash` refer to the **original raw evidence file** the converter
parsed — embedded per row by the converter, along with per-file sha256 provenance and the
converter name/version (which become the event's `parser_name`/`parser_version`) in the
parquet footer. For gzipped raw inputs, `byte_offset` addresses the *decompressed* content
stream; the sha256 covers the compressed file as it existed on disk. `line_number` is not
populated by this path (it is not part of event identity).

**Immutability lives on the original file, not the events table.** The ClickHouse `events`
table is a normalized *derivative* of the hashed source file. Enrichers (see
`enrichers/`) may amend an event's `attributes` map after ingest — derived keys follow the
`<attr_key>:<output_field>` contract (e.g. `src_ip:geo_country`) and are written via an
atomic per-source partition rewrite. The provenance columns (`content_hash`, `file_hash`,
`byte_offset`, `line_number`) are computed from raw bytes at ingest and never recomputed
or touched afterwards, so hash verification against the original evidence is unaffected;
which enricher config/data version produced a source's derived fields is recorded in
Postgres (`source_enrichments`).

**The Timeline is a derived, non-authoritative projection.** It is a view over
Source events — sorting, filtering, and coloring them — and does not itself constitute
evidence. Analysts should always trace findings back to the Source and its `file_hash`.

### Notable properties established by the refactor

- **Source files are retained**, content-addressed under `data/sources/{hash[:2]}/{hash}`;
  `GET /api/cases/{case_id}/sources/{source_id}/download` re-downloads the original.
- **Naive timestamps are assumed UTC** with a `UserWarning`
  (`models/event.py:_parse_timestamp`); per-source timezone config remains a future
  enhancement.
- **Exports carry forensic columns**: `source_id`, `artifact`, `artifact_long`,
  `content_hash`, `file_hash`.
- **Event identity requires a real file hash**: `derive_event_id` and the parsers refuse
  to fall back to a line hash; ingestion raises `ValueError` without one.
- **`created_by` is populated** on every Source since authentication landed.

---

## Reference: how Timesketch handles this

| Timesketch | Meaning | Vestigo equivalent (new) |
|---|---|---|
| Sketch | Investigation container | Case |
| Timeline | One import / one index | **Source** |
| Explore view | All timelines merged, color-coded, toggleable | **Timeline** (Explorer) |
| `data_type`, `source_short`/`source_long` | Per-event Plaso artifact class | **artifact** / **artifact_long** |

Timesketch avoids the overload by never calling the per-event field "source" in its UI — the
Plaso columns read as type/category. We adopt the same split: Source = the file you ingested;
Artifact = what kind of log record each event is.

---

## Storage placement audit (2026-07-05)

Reviewed every field across Postgres (metadata), ClickHouse (events), and Qdrant (vectors)
against the goal of minimal redundancy, each store doing what it's best at, and maximum
performance. Qdrant is the one optional store of the three (single-user/airgapped deployments
can skip it), so duplication *into* Qdrant is judged more leniently than duplication *between*
Postgres and ClickHouse.

### Verdict summary

| Data point | Stored in | Verdict | Why |
|---|---|---|---|
| Case/Source/Timeline/View/Annotation/User/Team/Audit rows | Postgres only | ✅ Correct | Relational, low-volume, needs transactions/joins/RBAC — exactly Postgres's job. |
| `content_hash`, `byte_offset`, `line_number` per event | ClickHouse only | ✅ Correct | Genuinely per-event; forensic pointer back into the raw file. |
| `file_hash`, `parser_name`, `parser_version` per event | ClickHouse (+ Source in Postgres) | ✅ Intentional, justified duplication | Constant per source, so this *is* denormalization — but ClickHouse dictionary-encodes low-cardinality columns near-free, and it avoids a per-query join against Postgres (which ClickHouse can't do natively without an external table engine) on the hot event-scan path. Keep. |
| `embedding_model`, `embedding_config_hash` per event (ClickHouse) + on Timeline (Postgres) + in Qdrant payload | 3 stores | ✅ Justified duplication | Lets a query resolve "which Qdrant collection does this event's vector live in" straight from the ClickHouse row, with zero Postgres round-trip. Same rationale as above. |
| `vector_id` column (ClickHouse) | ClickHouse | ❌ Pure redundancy — drop | `models/event.py` sets it unconditionally to `str(event_id)` in `__post_init__`; nothing anywhere ever assigns a different value. It's a second name for `event_id`, not an independent identity. Qdrant point IDs already come straight from `event.vector_id` (== `event_id`). Removing the column (using `event_id` as the Qdrant point ID directly) drops one column from every event row with zero behavior change. |
| `Source.embedding_model` / `Source.embedding_config` (Postgres) | Postgres | ❌ Dead field — drop | Declared on the `sources` table but never written anywhere in the codebase (`create_source` doesn't accept them; grep finds zero writers). The real, live embedding config lives on `Timeline` (`set_timeline_embedding`, driven by `POST /timelines/{id}/embed`). The Source-level fields are vestigial from before embedding moved to timeline-scope and should be removed, not carried forward as decoration. |
| Full row mirror in Qdrant payload (`message`, `display_name`, `source_file`, `byte_offset`, `line_number`, `content_hash`, `file_hash`, `parser_name`, `parser_version`, `timestamp_desc`, `artifact_long`, ...) | Qdrant + ClickHouse | ⚠️ Over-duplicated | Qdrant point ID == `event_id`, so any field not needed for **native Qdrant filtering** (payload-indexed `case_id`, `source_id`, `artifact`, `timestamp`, `tags`) can be dropped from the payload and fetched from ClickHouse in one batched `event_id IN (...)` lookup after the vector search returns candidate IDs — which callers already do downstream in practice. Trimming the payload to filter-relevant fields cuts Qdrant storage and index-rebuild cost substantially at 10M+ event scale, at the cost of one extra (cheap, PK-indexed) ClickHouse round trip per search. Worth doing since Qdrant is the store most sensitive to payload size on disk/RAM. |
| `tags` in Qdrant payload | Qdrant | ⚠️ Can go stale | Annotation tags are written only to Postgres (`annotations` table); nothing re-syncs a Qdrant point's `tags` payload after embed time. A tag added post-embedding is invisible to Qdrant-side tag filters until a re-embed. Not a correctness bug in the "evidence" sense (ClickHouse/Postgres stay authoritative), but a silent staleness trap for anyone filtering similarity search by tag. Either drop `tags` from the Qdrant payload (fold into the trim above, re-resolve tags from Postgres/ClickHouse post-search) or accept and document the staleness explicitly. |

### Net recommendation

- **ClickHouse stays the single source of truth for event-shaped data**, including the
  source-constant columns it denormalizes from Postgres — that duplication is deliberate and
  cheap, keep it.
- **Qdrant should shrink to an index, not a mirror**: keep only the payload fields needed for
  native vector-search filtering (`case_id`, `source_id`, `artifact`, `timestamp`, `tags` if kept),
  drop the rest, and resolve full event detail via a ClickHouse lookup on the returned IDs. This
  is safe to defer given Qdrant is optional, but becomes a real cost (RAM, reindex time) as
  case size grows past the millions-of-events range this project targets.
- **Two dead/redundant fields to actually delete**: `Event.vector_id` (use `event_id` as the
  Qdrant point ID directly) and `Source.embedding_model`/`Source.embedding_config` in Postgres
  (superseded by the Timeline-level fields; currently pure vestigial noise on every Source row).

**Status (2026-07-05, M21):** all three cleanups are implemented. `Event.vector_id` is gone
(the Qdrant point ID is `event_id` directly), the vestigial `Source.embedding_model`/
`Source.embedding_config` columns are removed, and the Qdrant payload is trimmed to
`case_id`/`source_id`/`artifact`/`timestamp`. `tags` was dropped from the payload entirely
(the second option above) — nothing filtered on it natively, and dropping it removes the
staleness trap; similarity results resolve tags from the authoritative ClickHouse row.
