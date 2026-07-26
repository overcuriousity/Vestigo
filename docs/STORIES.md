# Stories (W7)

Reference for the Stories subsystem: the per-case block document where the
investigation's narrative and its evidence live together. Design round:
`docs/superpowers/specs/2026-07-26-w7-stories-design.md`.

A story is a **living report**. Embeds are live queries while the analyst
writes, so the document tracks the data as ingestion and detection progress;
**export** freezes a server-resolved, hashed, immutable point-in-time
snapshot. Editing is collaborative at block granularity.

## Data model (Postgres)

Three tables, added by migration `0016_stories`.

### `stories`

Title and metadata only — content lives in blocks.

| column | notes |
| --- | --- |
| `id`, `case_id` | `case_id` indexed; multiple stories per case |
| `title`, `description` | |
| `created_by`, `updated_by` | usernames |
| `created_at`, `updated_at` | |

Deleting a story deletes its blocks and exports (hard delete, audited) — the
same convention as `views`/`saved_charts`.

### `story_blocks`

| column | notes |
| --- | --- |
| `id`, `story_id` | `story_id` indexed |
| `position` | integer **gap strategy**, stride `STORY_POSITION_GAP = 1024` |
| `kind` | `markdown \| view_ref \| chart_ref \| event_ref` |
| `content` | JSON, validated per kind at the API boundary |
| `origin` | `user \| agent` |
| `version` | optimistic-concurrency counter |
| `created_by`/`updated_by`, timestamps | |

**Ordering.** New blocks append at `last + 1024`; an insert between two blocks
takes the midpoint. When a gap closes to nothing the store renumbers the whole
story back onto the stride inside the same transaction and recomputes — order
and uniqueness are preserved, and no client ever sees a fractional position.

**Concurrency.** Every update/move carries the `version` the client last saw.
A mismatch raises `StaleBlockError`, which the router turns into `409` with the
current block in the body. Block granularity does the heavy lifting: two
analysts editing different blocks never conflict, and embed blocks are
effectively conflict-free (a ref plus display options).

### `story_exports`

Immutable. No update path; deletion is admin-only and audited.

| column | notes |
| --- | --- |
| `id`, `story_id`, `case_id` | |
| `snapshot` | the frozen `"v": 1` bundle (format below) |
| `snapshot_hash` | SHA-256 over canonical JSON (`sort_keys`, no whitespace) |
| `html`, `html_hash` | client-rendered artifact, sealed exactly once |
| `created_by`, `created_at` | |

### Referential integrity

No FK cascade from views/charts/events into blocks. Deleting a referenced
object leaves the block in place; the editor renders an explicit "was deleted"
placeholder and an export freezes a `resolution.error`. Investigation objects
stay freely deletable and the story degrades **visibly** — never silently.

## Block content contracts

Validated by `vestigo.stories.schemas.validate_block_content` (the single gate
for both the HTTP router and the agent's `propose_story_block`).

| kind | content |
| --- | --- |
| `markdown` | `{text}` |
| `view_ref` | `{view_id, timeline_id, display: {limit=200, columns?}}` |
| `chart_ref` | `{chart_id, timeline_id}` |
| `event_ref` | `{event_id, source_id, caption?}` |

`display` lives on the block, not on the View, so one View can be embedded
twice with different presentation. `limit` is validated `1..VIEW_BLOCK_ROW_CAP`
(1000). A `view_ref` carries `timeline_id` because a View is case-scoped while
resolving its rows needs a source scope.

## API

All routes are case-scoped, so `require_case_read` (view) and
`require_case_contribute` (edit/export) apply unchanged.

```
GET    /api/cases/{case}/stories
POST   /api/cases/{case}/stories                       {title, description?}
GET    /api/cases/{case}/stories/{story}               → story + ordered blocks
PATCH  /api/cases/{case}/stories/{story}
DELETE /api/cases/{case}/stories/{story}

POST   .../stories/{story}/blocks                      {kind, content, after_block_id?}
PATCH  .../stories/{story}/blocks/{block}              {content, version}   409 on stale
POST   .../stories/{story}/blocks/{block}/move         {after_block_id, version}
DELETE .../stories/{story}/blocks/{block}

POST   .../stories/{story}/exports                     → resolves + hashes (server)
POST   .../stories/{story}/exports/{export}/artifact   {html}   409 once sealed
GET    .../stories/{story}/exports
GET    .../stories/{story}/exports/{export}/snapshot
GET    .../stories/{story}/exports/{export}/artifact
DELETE .../stories/{story}/exports/{export}            admin only
```

`after_block_id: null` appends on create and moves to the top on move.

**The editor needs no story-specific data endpoints.** Embed blocks hold refs;
the frontend resolves them through the existing events/viz APIs, so an embedded
view is the same query the Explorer runs and cannot drift from it.

**Audit actions:** `story.create`, `story.delete`, `story.export`,
`story.export_delete`, plus the agent's `agent.story_block_confirm` /
`agent.story_block_reject`.

## Export semantics

Two phases, deliberately asymmetric:

1. **The server resolves.** `POST .../exports` executes every block itself —
   view queries (row-capped, through the same `_build_query` path as the
   Explorer so field mappings, tag filters and clock-skew offsets apply),
   chart execution via `execute_chart_spec`, event fetches, markdown frozen as
   text — then stores the bundle with its SHA-256. This is the authoritative
   record.
2. **The browser renders.** The client draws the snapshot with
   `SnapshotRenderer` (which performs no network access at all — asserted by a
   test that fails if a render fetches), inlines the app's compiled CSS, and
   uploads the standalone HTML once. The artifact is presentation; an export
   is complete and usable if the upload never happens.

Per-block resolution is individually wrapped: a dangling ref or a failed query
freezes as `resolution.error` with `data: null`. One bad block never fails an
export, and the gap is legible in the report.

### Snapshot format (`"v": 1`)

```json
{
  "v": 1,
  "story": {"id": "…", "title": "…", "case_id": "…",
            "exported_at": "…", "exported_by": "…"},
  "blocks": [
    {"id": "…", "kind": "view_ref", "origin": "user",
     "ref": {"view_id": "…", "timeline_id": "…", "name": "…",
             "query": "…", "filter": {}},
     "data": {"rows": [], "row_count_total": 14203,
              "rows_included": 200, "truncated": true, "columns": null},
     "resolution": {"executed_at": "…", "timeline_id": "…", "error": null}}
  ]
}
```

Per kind, `data` is:

- `markdown` — `{text}`
- `view_ref` — rows plus the truncation facts above
- `chart_ref` — `{name, config, resolved, warnings, chart}`, where `chart` is
  the raw aggregation and `resolved` records the data kind and compare mode the
  renderer needs to redraw it
- `event_ref` — `{event, caption}`

**Truncation is always stated**, never implied: a report showing 200 of 14203
rows says so, in the editor and in the exported artifact.

## Agent parity

The agent can do what an analyst can do: read directly, write through
propose→confirm. See `docs/AGENT.md` for the tool registry and deny layers.

- `list_stories` / `read_story` — read tools; also exposed on the external
  `/mcp` endpoint.
- `propose_story_block(story_id, block_kind, content, after_block_id?,
  rationale)` — conversation-bound, records an `AgentProposal`
  (`kind="story_block"`, target in `payload`) and writes nothing. Confirming
  creates the block with `origin: agent`. A `chart_ref` may carry
  `{chart_spec, name}` instead of a `chart_id` — the spec is validated by
  executing it at propose time, and confirming saves the chart and embeds it in
  one step. Absent from the external `/mcp` listing, like `propose_annotation`.

**Deliberate parity boundary:** block edit/move/delete and export stay
analyst-only. Parity covers analytical contribution, not document arrangement
or the attestation act — an export is a human sign-off by design. Revisit
trigger: a user asks the agent to restructure a story.

## Frontend map (`frontend/src/components/stories/`)

- `StoriesPage` / `StoriesPanel` — list and case-overview entry point.
- `StoryEditor` — block list, 10s polling, conflict resolution, dnd-kit
  reorder. `MarkdownBlock` edits raw markdown (no WYSIWYG); `EmbedCards`
  renders view/chart/event blocks read-only; `BlockPicker` inserts embeds.
- `AddToStoryButton` — the push path, mounted on the Explorer filter rail,
  the saved-charts rail, event detail and agent finding cards. Pushes carrying
  live filter state save a View first, because embeds must reference persisted
  objects.
- `SnapshotRenderer` + `exportHtml` + `ExportsTab` — the export half.

Charts are drawn by `components/viz/ChartCanvas` (live) and its `ChartMarks`
(shared with the snapshot renderer), so a chart looks identical in the
Visualize page, an agent card, a story block and an exported report.
