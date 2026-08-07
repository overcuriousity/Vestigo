# Grid column reordering, the `empty` match mode, and the header scroll fix

Date: 2026-08-07

Four small changes to the Explorer's event grid, filter rail and saved views,
grouped because two of them touch the same header markup in `EventGrid.tsx` and
a third is what makes a column layout worth persisting.

1. Drag-and-drop reordering of grid columns.
2. A filter that keeps or drops events by whether a field has a value at all.
3. A fix for the header not following horizontal scroll.
4. Saved views carry the column layout.

## 1. Column reorder by dragging the header

### Current state

Visible columns already live as an **ordered array** per timeline in
`useUiStore.visibleColumnsByTimeline`, keyed `"<caseId>/<timelineId>"` and
persisted to localStorage. `EventGrid` builds its TanStack column defs by
iterating that array in order (`EventGrid.tsx:589`), so the render order is the
array order. Nothing else needs to change to make order meaningful.

### Design

Wrap the header row in `DndContext` + `SortableContext` with
`horizontalListSortingStrategy`, from `@dnd-kit/core` / `@dnd-kit/sortable`
(both already dependencies, used by `stories/StoryEditor.tsx`).

- Sortable ids are exactly the `visibleColumns` entries. The grid-internal
  columns `_select`, `_annotations` and `_expand` are rendered outside the
  `SortableContext` and stay pinned at their ends.
- `onDragEnd` applies `arrayMove` to the visible-columns array and writes the
  result through the existing `setVisibleColumns(tlKey, next)`.
- Sensors: `PointerSensor` with `activationConstraint: { distance: 8 }`, plus
  `KeyboardSensor` for arrow-key reordering on a focused header cell.

### Interactions to respect

- **Resize handle.** The handle at `EventGrid.tsx:844` already calls
  `stopPropagation()` on `mousedown`/`touchstart`; it additionally needs
  `onPointerDown` stopped, because dnd-kit's `PointerSensor` listens on
  `pointerdown`. Without that, grabbing the handle would arm a drag.
- **Timestamp sort button.** The 8px activation distance is what keeps a plain
  click on the header's sort button (`EventGrid.tsx:450`) working.
- **Column widths.** `useUiStore.columnWidths` is keyed by column id, so a
  width follows its column across a reorder with no extra work.

### Persistence semantics

A reorder writes the same per-user, per-timeline override that choosing columns
already writes. That override outranks the server-side suggestion
(`Timeline.recommended_columns`) for every *automatic* recompute, and an
explicit "re-suggest" still clears it. This introduces no new precedence rule —
see `CLAUDE.md` on `columns/`.

No backend change.

## 2. The `empty` match mode

### Problem

An analyst wants to drop events where a field carries no value — for example,
every row whose `user_agent` is absent — and, less often, to isolate exactly
those rows.

### Encoding

Field filters already carry a per-key match mode
(`filterModes` / `exclusionModes` on the wire, `filter_modes` /
`exclusion_modes` server-side), validated against an allowlist in two places:

- `src/vestigo/db/queries.py:539` — `VALID_MATCH_MODES`
- `src/vestigo/api/routers/events.py:242` — `_VALID_FILTER_MODES`

We add a fourth mode, `"empty"`, rather than new request parameters. The mode
maps already thread through every filter-carrying surface (event list, export,
bulk annotate-by-filter, histogram, viz) and already round-trip through URLs
and saved views, so the new mode inherits all of that. Dedicated
`hasValue[]` / `isEmpty[]` parameters were considered and rejected: identical
semantics, far larger diff.

Wire shape, using a one-element list because `add_field_filter` early-returns
on an empty value list:

```jsonc
// keep only events where user_agent has no value
{ "filters":    { "user_agent": [""] }, "filterModes":    { "user_agent": "empty" } }
// keep only events where user_agent has a value  (the negative filter)
{ "exclusions": { "user_agent": [""] }, "exclusionModes": { "user_agent": "empty" } }
```

The value is a placeholder and is ignored by the query builder.

### Backend

In `QueryBuilder` (`db/queries.py`):

- `_match_column_expr` returns `ifNull(toString(<col expr>), '')` for mode
  `"empty"`. Attributes are a ClickHouse `Map`, which already yields `''` for a
  missing key, so one predicate covers both "key absent" and "key present but
  blank". The `ifNull` is what stops a NULL top-level column from making the
  comparison NULL, which would silently drop the row from *both* sides.
- `add_field_filter` with mode `"empty"` appends `<expr> = ''`.
- `add_field_exclusion` with mode `"empty"` appends `<expr> != ''`.
- Both ignore `values` entirely.

Add `"empty"` to both allowlists. `_validate_field_regexes` in
`events.py` must skip keys in `"empty"` mode — there is no pattern to compile.

Whitespace-only values are **not** treated as empty. `" "` is a value the
source recorded; collapsing it into "absent" would make the filter lie about
the evidence.

### Frontend

- `FieldMatchMode` in `api/types.ts` gains `"empty"`.
- `FilterRail`'s `MATCH_MODE_OPTIONS` gains a fourth button, `∅`, tooltip
  "No value — field is absent or blank".
- Selecting `∅` replaces the value input with a static, non-editable
  `(empty)` label, so there is no way to type a value that would be ignored.
- The row's existing include/exclude affordance carries the polarity: the
  include row reads **"is empty"**, the exclude row reads **"has a value"**.
- `FilterChips` renders those two phrasings rather than `key: value`.
- Serialization in `lib/queryParams.ts` needs no change; modes already
  round-trip.

`lib/fieldFilters.ts::applyFieldFilter` is untouched — click-to-filter always
filters on a literal clicked value and deliberately resets any mode on the key.

## 3. Header does not follow horizontal scroll

### Cause

The header row (`EventGrid.tsx:814`) is a **sibling** of the scroll container
(`EventGrid.tsx:858`), so the body's `scrollLeft` never reaches it. A second
defect sits in the same markup: virtualized rows are absolutely positioned
`left: 0; right: 0` (`EventGrid.tsx:897`), sizing them to the scroll
container's *client* width rather than its content width, so with columns
overflowing, row background, hover state and borders stop at the viewport edge.

### Design

Move the header inside the scroll container, under one shared-width wrapper:

```
div ref=parentRef  (overflow-auto)          <- the scroll element
  div  style={{ minWidth: totalColumnWidth }}
    div  (sticky top-0, z-20, bg-surface)   <- header row
    div  (height: totalHeight, relative)    <- virtualized body, rows left:0 width:100%
```

`totalColumnWidth` is the sum of the fixed column sizes; the `message` column
keeps `flex: 1 1 0`, so the wrapper only exceeds the viewport when the fixed
widths already do. Both header and rows then span the content width, which
fixes the row-width defect as a consequence rather than as a separate patch.

### The part that needs real verification

Making the header part of the scrolled content shifts every vertical offset by
the header's height. Three call sites read or write `scrollTop` against the
scroll element and all of them must account for it:

- `useVirtualizer` gains `scrollMargin` set to the body wrapper's `offsetTop`,
  and `getVirtualItems()` offsets become `vItem.start - scrollMargin`.
- `handleScroll`'s topmost-row computation (`EventGrid.tsx:710`), which drives
  the histogram's current-position indicator.
- The prepend anchor restore (`EventGrid.tsx:734`, `:745`), which corrects
  `scrollTop` after an earlier page is prepended, and the near-top / near-bottom
  thresholds that trigger paging.

A `scrollMargin` regression here is silent — the grid still renders, it just
pages or reports position wrongly. This is the risk in the change and gets
explicit test coverage plus a manual pass.

## 4. Saved views carry the column layout

### Current state

`View.view_filter` (`db/postgres.py:606`) is opaque JSON the backend only
round-trips — it never interprets it. `SaveViewDialog` writes
`filtersToViewPayload(filters)` into it; `FilterRail.tsx:642` reads it back
through `viewPayloadToFilters` and hands the result to `onApplyView`. Columns
are not filters and appear nowhere in that round trip.

### Design

Store the visible-column array under a `columns` key alongside the filter keys.
No migration, no backend change — the JSON column already accepts it and
`to_dict` already returns the whole object.

**Save.** `SaveViewDialog` takes the current `visibleColumns` as a prop and
writes `{ ...filtersToViewPayload(filters), columns }`, always. The `columns`
key is added at the call site, deliberately **not** inside
`filtersToViewPayload` — the Visualize page's chart config shares that helper
(`viz/lib/chartConfig.ts:160`, `:339`) and a column layout is meaningless there.

**Apply.** `viewPayloadToFilters` keeps its signature and its filters-only job.
A separate exported helper, `viewPayloadColumns(payload): string[] | undefined`,
reads and sanitizes the key: it must be an array of strings, run through the
existing `sanitizeColumns` (retired-id remap, drop grid-internal ids, dedupe),
and yield `undefined` when absent or when sanitizing empties it. `onApplyView`
gains an optional second argument; `ExplorerPage` writes it through
`setVisibleColumns(tlKey, columns)` when present and leaves the current layout
alone when not.

Views predating this change have no `columns` key, so applying one leaves the
analyst's columns untouched. That falls out of the `undefined` case rather than
needing a version flag.

### Cross-timeline behavior

Views are case-scoped; column layouts are per-timeline. Applying a view saved
on a different timeline installs its columns as-is. Attribute columns that
timeline does not have render `—`, the same as any column whose value is
missing on a row. This is accepted rather than guarded: the analyst asked for
that view by name, and a restore that silently does nothing is the worse
surprise. Removing a stray column is one click in the picker.

Restoring columns writes the same per-user, per-timeline override that a manual
column choice or a reorder writes (§1), so it interacts with the server
suggestion under rules that already exist.

## Testing

Backend:

- Query-builder unit tests asserting the SQL shape of `empty` include and
  exclude for an attribute key and for a top-level column.
- A ClickHouse integration test proving a row with the key absent and a row
  with the key present-but-blank both match the include side and are both
  dropped by the exclude side, and that a whitespace-only value is treated as
  present.
- A router test that `filter_modes: {"k": "empty"}` validates and that an
  unknown mode is still rejected.

Frontend (vitest):

- Reorder: dragging a header emits the expected `arrayMove` result into
  `visibleColumnsByTimeline`; `_select` / `_annotations` / `_expand` are not
  sortable; a pointerdown on the resize handle starts no drag.
- `empty` mode: selecting `∅` disables the value input; the emitted
  `EventFilters` matches the wire shape above; chips read "is empty" /
  "has a value".
- Saved views: the create payload carries `columns`; applying a view restores
  them into `visibleColumnsByTimeline`; a legacy payload with no `columns` key
  leaves the current layout untouched; a payload whose `columns` sanitize to
  nothing is treated as absent, not as "no columns".
- Scroll: with the header inside the scroller, the virtualizer's row offsets
  account for `scrollMargin`, and the topmost-row callback reports the same
  index before and after the restructure for a given `scrollTop + headerHeight`.

Manual: a timeline with enough columns to overflow horizontally — header
tracks the body, rows paint full width, paging up and down still works and the
histogram position indicator still tracks.

## Documentation

`ROADMAP.md` (remove the items as they land) and a `PROGRESS.md` entry. No
detector contract is touched, so `ANOMALY_DETECTION.md` is unaffected. The
`empty` mode is analyst-facing filter behavior; if the filter rail's modes are
documented in user-facing copy, that copy gains the fourth mode.

## Out of scope

A grid-level "hide rows with any blank column" toggle was considered and
dropped: its meaning changes silently whenever the visible column set changes,
which conflicts with reproducing a saved view or a shared URL exactly.
