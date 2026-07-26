# W7 Stories — design

Date: 2026-07-26. Status: approved (brainstorm session, user-confirmed).
Phase 3 Step 3 (`2026-07-19-phase3-investigation-depth-design.md`); canonical roadmap
entry: Phase 3 Step 3 in `ROADMAP.md`.

**Phase-spec override (user decision, 2026-07-26):** the phase spec deferred agent
authorship of stories; that deferral is rescinded. The agent-parity principle — the
agent can do (via propose→confirm) whatever an analyst can do — applies to Stories from
day one. W7 ships as a complete package, agent surface included, no follow-up round.

## Decision summary

A Story is a per-case block document — the investigation report that assembles itself
while the analysis happens. Ordered blocks of kind `markdown | view_ref | chart_ref |
event_ref`. Embeds are **live** in the editor (they track the data as ingestion and
detection progress); **export** freezes a server-authoritative point-in-time snapshot
(hashed, audited, immutable) plus a client-rendered standalone HTML artifact.

Decisions made in the brainstorm, with the alternatives they beat:

- **Block editor (Notion-style)** over plain-markdown-with-directives or a hybrid:
  matches the agreed Postgres block model 1:1, gives the drag-chart-into-report feel,
  and is the cleanest surface for agent authorship (the `propose_story_block` tool fits
  the existing sandbox+apply invariant — see "Agent parity" below).
- **Live editor + stored export snapshots** over freeze-at-export-only (report not
  reproducible from within Vestigo) and pin-at-embed (kills the live-report feel).
  Export = attested point-in-time record, like Source hashing.
- **Block-level optimistic concurrency + polling** over CRDT/WebSockets (deployment
  character change, overkill) and story-level locks (blocks collaborative triage).
  Block granularity makes most concurrent edits conflict-free by construction.
- **Push-first embed insertion** ("Add to story" on analysis surfaces) with editor
  pickers as the secondary path. Embed refs always point at persisted objects: pushing
  an unsaved Explorer filter state auto-creates a View first.
- **Standalone HTML export artifact** (client-rendered from the snapshot) over
  server-side PDF (heavy dependency, hostile to airgapped installs, would need a
  server-side chart-rendering stack). PDF = print the HTML. The snapshot JSON is
  server-produced and authoritative; the HTML is presentation.
- **Story lifecycle:** multiple stories per case; no finalize/lock state — stories stay
  editable forever, immutable exports are the record. Hard-delete with audit entry
  (matches View/SavedChart convention).
- **event_ref = single event** (compact evidence card). Sets of events are a view_ref.

## Data model (Postgres, Alembic migration)

Three new tables.

### `stories`

- `id` String(64) PK, `case_id` String(64) indexed
- `title` String(255), `description` Text nullable
- `created_by`, `updated_by` (user id), `created_at`, `updated_at`

### `story_blocks`

- `id` PK, `story_id` indexed
- `position` Integer — gap strategy (1000, 2000, …; insert-between takes the midpoint;
  server renumbers the story when a gap is exhausted)
- `kind`: `markdown | view_ref | chart_ref | event_ref`
- `content` JSON, validated per kind at the API boundary (pydantic discriminated union):
  - `markdown`: `{text}`
  - `view_ref`: `{view_id, display: {limit, columns?}}` — display options live on the
    block, not the View, so one View can be embedded twice with different presentation
  - `chart_ref`: `{chart_id}`
  - `event_ref`: `{event_id, source_id, caption?}`
- `origin`: `user | agent` — agent-origin blocks are created by confirming a
  `propose_story_block` proposal (see "Agent parity")
- `version` Integer — optimistic concurrency counter
- `created_by`, `updated_by`, timestamps

### `story_exports`

- `id` PK, `story_id`, `case_id`, `created_by`, `created_at`
- `snapshot` JSON — the full frozen bundle (format below)
- `snapshot_hash` — SHA-256 over canonical JSON (same canonicalization convention as
  `_windows_config_hash` / `models/event.py` config hashes)
- `html` Text nullable + `html_hash` nullable — client-rendered artifact, uploaded once
- Immutable: no update path; delete admin-only with audit entry

### Referential integrity

No FK cascades from investigation objects into blocks. Deleting a View/SavedChart that
a block references leaves the block in place; the editor renders an explicit
"referenced view deleted" placeholder. Existing exports are unaffected — their data is
frozen. Investigation objects stay freely deletable; the story degrades visibly, never
silently.

## API (`api/routers/stories.py`)

Story CRUD:

- `GET /api/cases/{case_id}/stories` — list
- `POST /api/cases/{case_id}/stories` — create
- `GET /api/stories/{story_id}` — story + ordered blocks
- `PATCH /api/stories/{story_id}` — title/description
- `DELETE /api/stories/{story_id}` — audit-logged

Blocks:

- `POST /api/stories/{story_id}/blocks` — kind, content, optional `after_block_id`
  (server computes the gap position). Push-from-Explorer uses this same endpoint.
- `PATCH /api/blocks/{block_id}` — body carries `version`; mismatch returns 409 with
  the current block in the body so the UI can show the conflict
- `POST /api/blocks/{block_id}/move` — `after_block_id`, version-checked
- `DELETE /api/blocks/{block_id}`

Editor live data needs **zero new endpoints**: embed blocks hold refs and the frontend
resolves them through the existing events/viz/event-detail APIs — same data path as the
rest of the app, TanStack Query caching included.

Export, two-phase, snapshot server-authoritative:

1. `POST /api/stories/{story_id}/exports` — the **server** resolves every block:
   executes view queries (row-capped), chart queries (SavedChart config mapped onto the
   existing viz query functions — reuse, no new query code), fetches the event, freezes
   markdown. Stores snapshot + hash, writes the audit entry, returns export + snapshot.
2. `POST /api/exports/{export_id}/artifact` — client renders the snapshot to standalone
   HTML and uploads once; server hashes and seals. The export is already complete and
   usable (snapshot JSON) if the artifact upload never happens.

- `GET /api/exports/{export_id}/snapshot`, `GET /api/exports/{export_id}/artifact` —
  downloads; `GET /api/stories/{story_id}/exports` — list.

RBAC: existing case roles — case read = view stories and exports, case write = edit and
export. Audit entries: story create/delete, export create (with hashes).

## Agent parity

The agent-parity principle (see `docs/AGENT.md`): read parity directly, write parity
through propose→confirm — the agent itself never writes. Stories get both at launch.

### Read tools

- `list_stories(case)` — id, title, description, block count, updated_at.
- `read_story(story_id)` — story with blocks; markdown as text, embed blocks as their
  ref + name (the agent resolves referenced views/charts/events through its existing
  read tools rather than a duplicate data path). Output bounded by the existing
  `_truncate`/cap conventions.

Both enter `TOOL_REGISTRY` as core read tools; the three deny layers (admin hard-deny,
per-user defaults, per-chat opt-in) and the tool-selector popover apply as everywhere.
Exposed on the external `/mcp` endpoint like the other read tools.

### Authorship: `propose_story_block`

`propose_story_block(story_id, kind, content, after_block_id?)` — conversation-bound,
records an `AgentProposal` (does not touch `story_blocks`), analyst confirms or rejects
via the existing decide endpoints; confirm re-resolves and creates the block with
`origin: agent`, audit-logged. All four kinds:

- `markdown` — the agent drafts narrative.
- `view_ref` / `event_ref` — must reference persisted objects, same rule as human push.
- `chart_ref` — may carry an inline `ChartConfig` (the `propose_chart` spec shape,
  validated by executing the query) instead of a `chart_id`; confirm then creates the
  SavedChart and the block in one step, so the agent can author a chart block without
  a pre-existing saved chart.

Like `propose_annotation`, `propose_story_block` is absent from the external `/mcp`
tool list — external MCP clients get read-only story access.

The chat panel renders a story-block proposal card (markdown preview or embed card,
reusing the editor's block renderers) with confirm/reject — same pattern as
`ChartProposalCard`.

### Deliberate parity boundary

Block **edit/move/delete and export** stay analyst-only. Parity covers analytical
contribution (authoring content), not document arrangement or the attestation act:
an export is a human sign-off by design, and diff-style proposals for editing other
authors' blocks add proposal-UX weight with no analytical payoff. Not a follow-up —
a decided boundary; revisit trigger: a user asks the agent to restructure a story.

## Frontend (`components/stories/`)

- `StoriesPage` — story list per case (create, rename, delete).
- `StoryEditor` — vertical block list, drag reorder (dnd-kit):
  - Markdown block: rendered markdown (react-markdown + GFM); click to edit in a plain
    textarea; save on blur/Ctrl-S. No WYSIWYG.
  - Embed blocks: read-only cards reusing existing components — view card = existing
    event-table component honoring the block's limit/columns; chart card = existing
    chart renderer fed by the SavedChart config; event card = compact evidence row.
    Card header: name, "open in Explorer/Visualize" link, remove, display options.
  - "+ block" inserter between blocks: markdown | pick view | pick chart | pick event
    (searchable lists over existing list APIs).
- `AddToStoryButton` on Explorer toolbar, Visualize saved-chart cards, event detail
  drawer, finding detail. Popover: pick story (or create), append block, toast with an
  "open story" link. Unsaved Explorer filter state triggers a pre-filled "Save view
  as…" dialog first.
- Collaboration: story + blocks polled via TanStack Query (`refetchInterval` ~10s).
  A block being edited is local state and never clobbered by polling; a 409 on save
  shows an inline "changed by {user} — load theirs / overwrite" choice.
- Export flow: phase-1 call, then a dedicated `SnapshotRenderer` renders the snapshot
  to standalone HTML — same chart components in a static, non-interactive mode, data
  from the snapshot only, no network — then serialize, upload, offer download. An
  Exports tab lists past exports with hashes and downloads.
- Routing: `/cases/{id}/stories`, `/cases/{id}/stories/{storyId}`; case-sidebar entry.

## Snapshot format

Versioned (`"v": 1`):

```json
{
  "v": 1,
  "story": {"id": "…", "title": "…", "case_id": "…",
            "exported_at": "…", "exported_by": "…"},
  "blocks": [
    {"id": "…", "kind": "view_ref", "origin": "user",
     "ref": {"view_id": "…", "name": "…", "query": "…", "filter": {}},
     "data": {"rows": [], "row_count_total": 14203,
              "rows_included": 200, "truncated": true},
     "resolution": {"executed_at": "…", "timeline_id": "…", "error": null}}
  ]
}
```

- Per-block `resolution.error`: a dangling ref or query failure freezes as an explicit
  error block ("view deleted before export"). The export succeeds; the gap is honest
  and visible, never silently dropped.
- Truncation is always flagged — the report states "200 of 14203 rows shown".

## Error handling and limits

- Block `content` validated per kind at the API boundary.
- Export resolution wrapped per block — one failing block never fails the export.
- Row caps per view block: default 200, block-configurable up to a hard cap (~1000).
  JSON size cap on the artifact upload.
- Concurrent export + edit: the snapshot reads committed state at resolve time, no
  locking — exports are point-in-time by definition.

## Testing

- Backend: model/store unit tests (SQLite, as elsewhere); router tests for block CRUD,
  gap reordering + renumber-on-exhaustion, the 409 conflict path, the RBAC matrix,
  export resolution with dangling refs, snapshot hash stability (canonical JSON).
- Agent: tool tests for `list_stories`/`read_story` (truncation caps included) and the
  `propose_story_block` lifecycle — proposal row, confirm creates block with
  `origin: agent` (incl. the inline-ChartConfig chart_ref path creating the
  SavedChart), reject path, absence from the external `/mcp` tool list.
- Frontend (vitest): block ordering logic, conflict UI state, `SnapshotRenderer`
  rendering a fixture snapshot with zero network calls (assert no fetch).
- End-to-end via the `/verify` skill against the live stack: create story, push blocks
  from Explorer, export, verify hashes.

## Documentation plan

New `docs/STORIES.md` reference (model, snapshot format contract, export semantics);
`docs/AGENT.md` updated in the same commit as the agent tools (tool table, proposal
lifecycle, `/mcp` exposure note); remove the Phase 3 Step 3 entry from `ROADMAP.md` on
landing; `PROGRESS.md` entry as usual.

## Alternatives considered

Recorded inline under "Decision summary" — each decision lists what it beat and why.
