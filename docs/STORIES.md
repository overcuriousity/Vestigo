# Stories

Reference for the Stories subsystem: the per-case block document where the
investigation's narrative and its evidence live together. Design round:
`docs/superpowers/specs/2026-07-26-w7-stories-design.md`.

A story is a **living report**. Embeds are live queries while the analyst
writes, so the document tracks the data as ingestion and detection progress;
**export** freezes a server-resolved, hashed, immutable point-in-time
snapshot. Editing is collaborative at block granularity.

## Data model (Postgres)

Three tables, added by migration `0016_stories`; migration
`0018_story_block_position_unique` adds the unique `(story_id, position)`
index the ordering invariant rests on.

### `stories`

Title and metadata only — content lives in blocks.

| column | notes |
| --- | --- |
| `id`, `case_id` | `case_id` indexed; multiple stories per case |
| `title`, `description` | |
| `created_by`, `updated_by` | usernames |
| `created_at`, `updated_at` | |

Deleting a story deletes its blocks and exports (hard delete, audited) — the
same convention as `views`/`saved_charts`. Because that cascade takes
*exports*, and deleting a single export is admin-only, a story that carries any
export is **admin-only to delete**; otherwise the cascade would be a way around
that gate. The deleted exports' `id`/`snapshot_hash`/`html_hash` go into the
`story.delete` audit record, so the attestation trail outlives the rows.

`delete_case` deletes stories, blocks and exports explicitly — none of them
carries an FK to `cases.id`, and a leftover export snapshot holds frozen event
data from a case the operator believes is gone.

### `story_blocks`

| column | notes |
| --- | --- |
| `id`, `story_id` | `story_id` indexed |
| `position` | integer **gap strategy**, stride `STORY_POSITION_GAP = 1024`; unique per story |
| `kind` | `markdown \| view_ref \| chart_ref \| event_ref` |
| `content` | JSON, validated per kind at the API boundary |
| `origin` | `user \| agent` |
| `version` | optimistic-concurrency counter |
| `created_by`/`updated_by`, timestamps | |

**Ordering.** New blocks append at `last + 1024` (or below the first block with
`at_top`); an insert between two blocks
takes the midpoint. When a gap closes to nothing the store renumbers the whole
story back onto the stride inside the same transaction and recomputes — order
and uniqueness are preserved, and no client ever sees a fractional position.

Positions are computed from a read of the sibling set, so inserts and moves run
under a `SELECT … FOR UPDATE` lock on the parent `stories` row, and the unique
`(story_id, position)` index is the actual invariant behind them: a lock orders
transactions but doesn't make a value read before it was taken current, and
SQLite (the test dialect) ignores `FOR UPDATE` entirely. A lost race for a slot
raises `IntegrityError` and is **retried** (`STORY_POSITION_ATTEMPTS`) rather
than surfaced — the caller asked for "after block X", not for a specific
integer. Reads order by `(position, id)` so ties in a pre-index database are at
least stable across polls; ambiguous document order in a forensic report is a
correctness problem, not a cosmetic one.

Renumbering parks every row at a distinct negative rank before writing the
finals, since assigning them in place would transiently collide with a row that
still holds the target value. It leaves `updated_at` and `version` alone: a
bumped timestamp makes an untouched block look edited to a polling client, and a
bumped version would manufacture 409s for collaborators holding a valid one.

**Concurrency.** Every update/move carries the `version` the client last saw.
The guard is a **compare-and-swap in the UPDATE's WHERE clause**, not a
read-then-write: under `READ COMMITTED` two collaborators could otherwise both
read `version=1`, both pass a Python-side check, and both write `version=2`,
silently losing one edit. `rowcount == 0` raises `StaleBlockError`, which the
router turns into `409` with the current block in the body. Block granularity
does the heavy lifting: two analysts editing different blocks never conflict,
and embed blocks are effectively conflict-free (a ref plus display options).

The editor's side of that contract matters as much: a markdown block captures
the version **at edit start** and saves against it. Reading the live polled prop
would send whatever version the last 10s poll fetched — including a
collaborator's — so the server's check would pass and their edit would be
destroyed with no 409 at all. Since a paragraph takes longer to write than the
poll interval, that was the common path, not the rare one.

### `story_exports`

Immutable. No update path; deletion is admin-only and audited.

| column | notes |
| --- | --- |
| `id`, `story_id`, `case_id` | |
| `snapshot` | the frozen `"v": 1` bundle (format below) |
| `snapshot_hash` | SHA-256 over canonical JSON (`sort_keys`, no whitespace) |
| `html`, `html_hash` | client-rendered artifact, sealed exactly once |
| | the seal guard is `WHERE html IS NULL` in the UPDATE, not a preceding read |
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
| `markdown` | `{text}`, capped at `VESTIGO_STORY_MAX_MARKDOWN_BYTES` (256 KiB) |
| `view_ref` | `{view_id, timeline_id, display: {limit=200, columns?}}` |
| `chart_ref` | `{chart_id, timeline_id}` — the chart carries its own filters, see below |
| `event_ref` | `{event_id, source_id, caption?}` (caption ≤ 1000 chars) |

A block's `timeline_id`, `view_id`, `chart_id` and `source_id` are additionally
checked against the case by `vestigo.stories.refs.validate_block_scope`. Shape
validation alone let a foreign or mistyped id through, to surface much later as
an undrawable card or a frozen `resolution.error`; an error at the point of the
mistake is the better failure. (A ref that goes dangling *afterwards* is a
different thing, and still degrades visibly.)

### A `chart_ref` is a chart *and* the slice it was built over

A `SavedChart.config` holds the frontend's `ChartConfig` (chart type, field,
scale, metric, options, comparison layer) **plus a `filters` key**: the
Explorer filter set the chart was drawn under, serialized in exactly the same
payload shape as `View.view_filter` (`filtersToViewPayload`). On the Visualize
page those filters come from the URL and are shown by `InheritedFiltersBar`, so
storage is the one place the chart and its scope travel together.

Every consumer reapplies them — the live block (`ChartBlockCard` →
`ChartCanvas`), the export resolver (`_stored_chart_to_spec` → `ChartSpec.filters`
→ `execute_chart_spec`), the block's "Open in Visualize" link, and loading the
chart back into the Visualize rail. Without this a chart saved from a filtered
view redrew over the whole timeline everywhere, showing precisely the data the
analyst had excluded.

The last two reach the filters by **naming the chart** rather than describing
it: both navigate to `?c_chart=<id>` (`CHART_ID_PARAM`), and `VisualizePage`
resolves the id against the saved-chart list and reads both halves out of
storage. Describing the chart in `c_*` params instead would have to describe its
filters too, and three of those have no URL form at all (below) — so a
reconstructed link could restore an agent-scoped chart's shape while silently
widening it to the whole timeline. The reference lives inside the `c_*`
namespace that `chartConfigToParams` clears before writing, so editing the chart
drops it by construction: `c_chart` can only ever claim "this is saved chart X"
while that is still true. A reference to a deleted or unreadable chart says so
and falls back to the params.

The key is additive within `v: 1`: a chart saved before it existed has no
`filters` key, and that absence means "whole timeline" — the behavior those
charts have always had. There is no way to recover their lost filters; re-save
them from the filtered view.

The payload is a **superset** of a View's: on top of `filtersToViewPayload` it
carries `eventIds`, `runId` and `collapseRoutine`, three narrowings the Explorer
cannot produce and a View deliberately never freezes. An agent's `ChartSpec`
can, so both sides write and read them (`chartFiltersToStored` /
`parseStoredChartFilters` on the frontend, `_spec_filters_to_payload` /
`_filter_payload_to_spec` on the backend) — dropping them anywhere would widen
a chart the agent had scoped to, say, one detector run's events. A chart saved
from the Visualize page never carries them: `collapseRoutine` is not
URL-serialized (it derives from live dispositions) and the rail is passed the
raw URL filters, so the analyst path is unchanged.

Unlike a View, the chart payload also stores only what narrows — no
`null`/`false`/empty defaults — so "unfiltered" and "saved before the key
existed" are the same bytes.

Three things still do **not** survive:

- **`collapseRoutine`, once Visualize has the chart.** The page derives it from
  the live disposition set rather than restoring the stored value, deliberately:
  a mute is a filter, and a shared URL should show a teammate the charts their
  *current* dispositions produce. So a chart whose stored `collapseRoutine`
  disagrees with today's mutes renders per the live set on Visualize, and per
  the frozen set in the story canvas and the export. `ids` and `anomalyRunId`
  have no such live source and are restored as stored.
- **An unsaved chart's link.** `ChartProposalCard` shows a chart that has not
  been saved yet, so there is no id to name and its link falls back to `c_*`
  params — lossy for the three members above. Saving the chart is what makes it
  addressable; the link on the resulting saved chart is exact.
- **Semantic-search mode.** `qMode: "semantic"` survives the payload but the
  server-side `FilterSpec` has no semantic mode, so an export treats the query
  as a keyword (`ROADMAP.md`, Milestone 3).

Every write path runs both gates: the HTTP router (422), the agent's
`propose_story_block` (a tool error the model can correct) **and** its confirm
handler — the last because a referent can be deleted between propose and
confirm, in which case the proposal still decides but reports
`applied: false` with the reason. An `event_ref`'s `source_id` is checked;
the `event_id` itself is only resolvable against ClickHouse, so a wrong one
surfaces at export as a `resolution.error`.

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

POST   .../stories/{story}/blocks              {kind, content, after_block_id?, at_top?}
PATCH  .../stories/{story}/blocks/{block}              {content, version}   409 on stale
POST   .../stories/{story}/blocks/{block}/move         {after_block_id, version}
DELETE .../stories/{story}/blocks/{block}?version=N                  409 on stale

POST   .../stories/{story}/exports                     → resolves + hashes (server)
POST   .../stories/{story}/exports/{export}/artifact   {html}   409 once sealed
GET    .../stories/{story}/exports
GET    .../stories/{story}/exports/{export}/snapshot
GET    .../stories/{story}/exports/{export}/artifact
DELETE .../stories/{story}/exports/{export}            admin only
```

`after_block_id: null` appends on create and moves to the top on move. The two
endpoints genuinely differ, and create keeps its meaning because every append
caller relies on it — the "Add to story" pushes and the agent's
`propose_story_block` default. Create therefore cannot name the top with an
anchor, so it takes **`at_top`** for that placement (mutually exclusive with
`after_block_id`, 422 if both). Without it the story editor's "Add at top"
button sent `null` and its block landed at the bottom. Both "top of document"
paths share one implementation (`PostgresStore._story_top_position`): halve
below the first block, renumbering from index 2 when there is no room left.

Every block mutation carries the optimistic `version`, **delete included** — it
rides as a query parameter there because DELETE bodies are not reliably carried
end to end. Deleting a block a collaborator has meanwhile rewritten is the one
loss the version guard cannot undo afterwards, so it is not the mutation that
skips the check; the editor refetches on the 409 and lets the analyst decide
again.

`PATCH` touches only the fields present in the body, so `{"description": null}`
clears the description while `{"title": "x"}` leaves it alone; a supplied title
must be non-blank, the same rule `POST` applies. Mutating routes additionally
depend on `require_password_current`, like the `cases`/`events`/`viz` writes.

**Limits** (all `VESTIGO_`-prefixed settings, `core/config.py`):
`STORY_EXPORT_MAX_BLOCKS` (500) bounds how much querying one export request can
trigger — resolution is synchronous; `STORY_EXPORT_MAX_SNAPSHOT_BYTES` (64 MiB)
is enforced *during* resolution, block by block, so a story that would blow past
it stops costing memory and ClickHouse queries at the block that crosses the
line (the 413 names that block, which is what tells the analyst which embed to
shrink) — measuring only the finished bundle would bound what gets stored while
still materializing an arbitrarily large one first; `STORY_MAX_ARTIFACT_BYTES`
(20 MiB) is applied to the *arriving stream* (the seal route reads the raw
request rather than a parsed body model, so an oversized upload is abandoned
instead of buffered and then rejected). `0` disables the two byte ceilings,
matching the other `VESTIGO_MAX_*_BYTES` settings.

**The artifact download is hardened against its own origin.** The HTML is
authored entirely by the client — the seal route only checks that it embeds the
export's `snapshot_hash` — and is served back from the app's origin, where the
session cookie lives. `Content-Disposition: attachment` is what stops a browser
rendering analyst-supplied markup there; `X-Content-Type-Options: nosniff` and
`Content-Security-Policy: sandbox` ride along so that defense is not one header
deep. Every UI path treats the response as a download, so nothing is lost.

**The editor needs no story-specific data endpoints.** Embed blocks hold refs;
the frontend resolves them through the existing events/viz APIs, so an embedded
view is the same query the Explorer runs and cannot drift from it.

**Audit actions:** `story.create`, `story.delete` (with the cascaded exports'
hashes in `detail`), `story.export`, `story.export_delete` (with the deleted
export's hashes), plus the agent's `agent.story_block_confirm` /
`agent.story_block_reject`. `case.delete` also carries the hashes of every story
export the case cascade destroyed.

**Deleting sealed exports is admin-only at every level.** A single export is
admin-only because it is an immutable attestation; a story carrying any is
admin-only for the same reason, since the story cascade takes them too; and so
is a *case* carrying any (`DELETE /api/cases/{case}`, otherwise
`require_case_manage`). Each level is a way around the one below it, so the gate
has to sit on all three. The hashes go into the audit record in every case —
that log is the only place an attestation survives its row.

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
   is complete and usable if the upload never happens. Such an export stays
   unsealed (`html IS NULL`), and the Exports tab offers **Render HTML** on it
   — a retry that re-renders from the *stored* snapshot, never a fresh
   resolution, so the artifact still attests to the same frozen record and the
   same hash.

Charts are executed at export time under **`ANALYST_CHART_LIMITS`**, not the
agent's. `execute_chart_spec` is shared with `propose_chart`, whose caps exist
to protect a context window (terms top-30, pivot 8×8, scatter 300 points); the
`/api/viz/*` endpoints the analyst's card uses are far wider (top-50/500,
10×10/50×50, 5000/20000). Running an export under the agent's bounds meant a
top-50 bar chart froze as top-30 and the report picked up a clamp warning
reading "agent context budget" — an attested document must not silently show
less than the analyst signed off on.

View blocks run their stored filter through the same regex validation every
interactive path applies before building a query, so the export path can't drift
into accepting a pattern the Explorer rejects.

**Verifying an export.** `GET .../snapshot` serves the *canonical* bytes —
exactly the serialization `snapshot_hash` was computed over — plus an
`X-Vestigo-Snapshot-Hash` header, so a third party can hash the response body
directly with no knowledge of our canonicalization rules. Re-encoding the dict
with a different key order would not reproduce the hash. `canonical_json` uses
`allow_nan=False` deliberately: Python emits bare `NaN`/`Infinity`, which no
conforming JSON parser accepts, so hashing them would leave an unverifiable
attestation. The resolver coerces non-finite floats to `null` upstream
(`_json_safe`), and sorts sets, whose iteration order varies between processes.

The uploaded artifact must **embed the export's `snapshot_hash`** (a `<meta>`
tag and the visible footer); the seal route rejects one that doesn't. Nothing
else binds the HTML's content to the record it claims to render, and `html_hash`
is presented with the same authority as `snapshot_hash`. Verification is still
always against the snapshot — this only stops an artifact being sealed onto the
wrong export.

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
  renderer needs to redraw it. `chart` is the aggregation **as the service
  returned it**, which is not always the shape the mark reads: an uncompared
  time chart freezes a raw histogram (`{start, count}`) while the mark wants
  `{primary, comparison}`. `snapshotToChartResult` (in
  `components/viz/chartFetch.ts`, beside `fetchChartData` so the two can't
  drift) is the single place that reshaping happens, and it returns the real
  discriminated union rather than a cast — a divergence between the live and
  frozen paths is a build failure, not a chart of blank bars.
- `event_ref` — `{event, caption}`

`SnapshotBlock` and `StoryBlock` are **discriminated unions** on `kind` in
`frontend/src/api/types.ts`, mirroring the backend's pydantic models. The
earlier `Record<string, unknown>` typing forced every renderer to re-assert the
shape locally, which is how the histogram bug above stayed invisible.

Agent-authored blocks are marked in the exported HTML, not only in the editor:
the export is the artifact that leaves the tool, and a reader of the report is
exactly who needs to know which paragraphs the AI wrote.

**Truncation is always stated**, never implied: a report showing 200 of 14203
rows says so, in the editor and in the exported artifact.

## Agent parity

The agent can do what an analyst can do: read directly, write through
propose→confirm. See `docs/AGENT.md` for the tool registry and deny layers.

- `list_stories` / `read_story` — read tools; also exposed on the external
  `/mcp` endpoint. `read_story` returns markdown under a per-block and
  per-response budget (`STORY_TEXT_TRUNCATE` / `STORY_TEXT_BUDGET`), and marks
  every cut with `truncated`/`text_length` — the agent has no way to fetch a
  cut block's tail, so a cut it cannot see is a cut it would summarize as the
  whole block.
- `propose_story_block(story_id, block_kind, content, after_block_id?,
  rationale)` — conversation-bound, records an `AgentProposal`
  (`kind="story_block"`, target in `payload`) and writes nothing. Confirming
  creates the block with `origin: agent`. A `chart_ref` may carry
  `{chart_spec, name}` instead of a `chart_id` — the spec is validated by
  executing it at propose time, and confirming saves the chart and embeds it in
  one step. Absent from the external `/mcp` listing, like `propose_annotation`.

  The saved chart's `config` is a **stored `ChartConfig`** (the frontend's
  camelCase, `v: 1` shape), derived at propose time by
  `stories.export.spec_to_stored_chart_config` — the exact inverse of the
  `_stored_chart_to_spec` the export resolver uses, tested as a round trip.
  Writing the agent's snake_case `ChartSpec` dump straight into that column
  produced a chart that the export, the story card and the Visualize rail all
  refused to draw, with no error at write time. A spec's `filters` travel with
  it into the stored config's `filters` key (below), so a confirmed proposal
  embeds the slice the agent proposed over, not the whole timeline.

**Deliberate parity boundary:** block edit/move/delete and export stay
analyst-only. Parity covers analytical contribution, not document arrangement
or the attestation act — an export is a human sign-off by design. Revisit
trigger: a user asks the agent to restructure a story.

## The demo case's story

The seeded demo case (`src/vestigo/demo/`) ships one story, and it deliberately
uses **all four block kinds** — it is the only worked example most users see
before writing their own, and a story made of nothing but prose teaches that
the subsystem is a text editor.

- `demo/metadata.py::STORY_BLOCKS` is a tuple of `DemoBlock`s that name their
  referents (`view="Service installs"`, `chart="Upload sizes"`,
  `timeline="Network"`) rather than carrying ids, because ids only exist once
  the case is built. `resolve_story_blocks` swaps in the real ids at seed time
  and resolves each `event_ref` selector against ClickHouse with the same
  `first_event_id` query the demo annotations use — an embed pointing at no
  event fails the seed rather than shipping.
- `demo/metadata.py::CHARTS` are saved `ChartConfig`s (stored camelCase shape,
  `v: 1`) created on the demo's timelines, so the Visualization page is
  populated on first login and the story's `chart_ref` blocks have something
  real to re-run.
- `demo/build.py` runs every block through `validate_block_content` and
  `validate_block_scope` before writing it — the same two gates the HTTP router
  and the agent go through. A wrong referent otherwise surfaces only much later,
  as a frozen `resolution.error` in an export.
- `tests/test_demo_build_clickhouse.py::test_story_embeds_resolve_into_a_snapshot`
  exports the seeded story end to end and asserts no block resolves to an
  error — keep it green when retuning demo sources, since a renamed field or a
  moved phase boundary breaks an embed silently.

## Frontend map (`frontend/src/components/stories/`)

- `StoriesPage` / `StoriesPanel` — list and case-overview entry point.
- `StoryEditor` — block list, 10s polling, conflict resolution, dnd-kit
  reorder. `MarkdownBlock` edits raw markdown (no WYSIWYG); `EmbedCards`
  renders view/chart/event blocks read-only; `BlockPicker` inserts embeds.
  `useStory.ts` owns the story query key and the poll interval for both the
  editor and the page — declaring it in each was how they drifted apart.
  Edit mode is reported *upward* (`onEditingChange` → `editingIds`) so polling
  never clobbers a draft, which makes that path loop-prone: the effect must
  depend on `editing` alone (the callback lives in a ref), and `editingIds.ts`
  must return the *same* Set when membership is unchanged. Both were missing
  and froze the view (#193), and `storyEditorLoop.test.tsx` pins each half
  separately — either alone stops the loop, so a combined test would hide a
  regression in one. The effect also reports `false` on unmount, or a block
  deleted mid-edit stays in `editingIds` forever.
  A view block's rows are windowed (`useVirtualizer`, fixed row height), so
  the preview builds a screenful rather than all 200 embedded rows. Table
  semantics are spelled out with ARIA roles, since virtualization rules out a
  real `<table>`, and truncated cells carry their full text in `title`. The
  count beneath it still describes the full embedded set — the same set the
  export snapshot renders independently.
- `AddToStoryButton` — the push path, mounted on the Explorer filter rail,
  the saved-charts rail, event detail and agent finding cards. Pushes carrying
  live filter state need a persisted View, and go through
  `lib/storyViews.ts::findOrCreateView`, which **reuses** a View already
  encoding exactly those filters (same query, same payload, ignoring
  absent-vs-empty) and only mints one when they are genuinely new. *Deviation
  from the design round,* which sketched opening the `SaveViewDialog` first:
  reuse removes the duplication that motivated the dialog (pushing the same
  filters three times left three identically-named Views) without interrupting
  a push. An event block resolves through a timeline that actually **contains
  its `source_id`** — `eventsApi.getById` is timeline-scoped, and defaulting to
  the case's default timeline made the card claim "deleted" for blocks the
  server-side resolver (which queries by `source_id`) resolves fine. Editor and
  export must not disagree about whether a block resolves.
- `SnapshotRenderer` + `exportHtml` + `ExportsTab` — the export half.
  `exportHtml` takes the snapshot hash and embeds it (see above). Downloads go
  through the shared `lib/download.ts::triggerDownload`, which attaches the
  anchor to the DOM before clicking it (Firefox/Safari need this) and sanitizes
  the filename.

  Charts export as real `<svg>`, drawn at a **pinned width**:
  `SnapshotRenderer` provides `ChartStaticWidthContext` (848px — the
  `max-w-4xl` article minus its `p-6` gutters), which `ChartFrame` uses as its
  starting width. Without it the export drew no charts at all (issue #197):
  `ChartFrame` normally learns its width from a `ResizeObserver` in an effect
  and gates on `width > 0`, and `renderToStaticMarkup` runs neither. A live
  `ResizeObserver` still overrides the pinned value, so on-screen charts are
  unaffected.

Embed cards distinguish **"the target was deleted"** from **"the lookup
failed"**. A 500 or a network blip reported as a deletion is worse than
silence, given the subsystem's promise that a story degrades visibly rather
than misleadingly.

Charts are drawn by `components/viz/ChartCanvas` (live) and its `ChartMarks`
(shared with the snapshot renderer), so a chart looks identical in the
Visualize page, an agent card, a story block and an exported report.
