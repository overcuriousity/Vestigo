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

## Storage placement

Which store owns what, and where duplication is deliberate. Qdrant is the one optional
store of the three (single-user/airgapped deployments skip it), so duplication *into*
Qdrant is judged more leniently than duplication between Postgres and ClickHouse.

| Data | Stored in | Why |
|---|---|---|
| Case / Source / Timeline / View / Annotation / User / Team / Audit rows | Postgres only | Relational, low-volume, needs transactions, joins and RBAC. |
| `content_hash`, `byte_offset`, `line_number` per event | ClickHouse only | Genuinely per-event — the forensic pointer back into the raw file. |
| `file_hash`, `parser_name`, `parser_version` per event | ClickHouse, and on `Source` in Postgres | Deliberate denormalization: constant per source, but ClickHouse dictionary-encodes low-cardinality columns near-free, and it avoids a per-query join against Postgres on the hot event-scan path. |
| `embedding_model`, `embedding_config_hash` | ClickHouse + `Timeline` (Postgres) + Qdrant payload | Lets a query resolve which Qdrant collection an event's vector lives in straight from the ClickHouse row, with no Postgres round-trip. |
| Vector payload | Qdrant | Trimmed to what native vector-search filtering needs: `case_id`, `source_id`, `artifact`, `timestamp`. |

**Qdrant is an index, not a mirror.** The point ID *is* the `event_id`, so full event
detail resolves through one batched `event_id IN (...)` ClickHouse lookup after the vector
search returns candidates. `tags` are deliberately not in the payload: annotation tags are
written only to Postgres and nothing re-syncs a point after embed time, so a payload copy
would go stale silently and similarity results filtered by it would be wrong. Tags resolve
from the authoritative row instead.

Three cleanups from the 2026-07-05 placement review shipped in M21 and are recorded here
as settled rather than pending: `Event.vector_id` is gone (it was only ever
`str(event_id)`), the vestigial `Source.embedding_model`/`Source.embedding_config` columns
are removed (the live config lives on `Timeline`), and the Qdrant payload is trimmed as
above.
