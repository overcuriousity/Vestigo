# Grid Columns, Empty Filter and Saved-View Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the Explorer's event grid drag-reorderable columns and a header that follows horizontal scroll, add an `empty` field match mode, and make saved views carry the column layout while becoming searchable and deletable without breaking story exports.

**Architecture:** Four of the five items are frontend-only apart from two match-mode allowlist entries; the visible-column array in `useUiStore.visibleColumnsByTimeline` is already ordered and already the per-user override, so reordering and restoring it needs no new backend concept. The fifth item adds one nullable `deleted_at` column to `views`: deleting a view a story block embeds hides it instead of removing it, and an idempotent per-case sweep hard-deletes it once the last reference is gone.

**Tech Stack:** Python 3.13 / FastAPI / SQLAlchemy async / Alembic / ClickHouse; React 19 / TypeScript / TanStack Table + Virtual / Zustand / dnd-kit; pytest + vitest.

Design spec: `docs/superpowers/specs/2026-08-07-grid-columns-and-empty-filter-design.md`.

## Global Constraints

- Ruff config selects `["E", "F", "I", "UP", "B", "C4", "SIM"]`, `line-length = 100`, `E501` ignored — do not wrap a line for length alone. Google-style docstrings.
- Alembic migrations run against SQLite in the test suite: keep them dialect-portable (`sa.func.now()`, never `sa.text('now()')`). Never add schema changes as inspector `ALTER TABLE`s in `init_schema`.
- Do not compare JSON with a dialect-specific SQL operator. `content["view_id"]` is read in Python.
- `require_case_read` endpoints do not write. Nothing in this plan adds an exception.
- Run backend commands from the repo root with `uv run`; frontend commands from `frontend/`.
- Commit after every task. Commit messages are written normally, not in any compressed style.
- Existing behavior that must not regress: `filtersToViewPayload` is shared with the Visualize page's chart config (`frontend/src/components/viz/lib/chartConfig.ts:160,339`) — it must not learn about columns.

---

## File Structure

**Backend — modified**
- `src/vestigo/db/queries.py` — `VALID_MATCH_MODES` (line 539), `_match_column_expr`, `add_field_filter`, `add_field_exclusion`.
- `src/vestigo/api/routers/events.py` — `_VALID_FILTER_MODES` (line 242).
- `src/vestigo/db/postgres.py` — `View` model (line 606), `list_views`/`delete_view` (lines 3060, 3102), new `purge_orphaned_hidden_views`, sweep calls in `delete_story` / `delete_story_block` / `update_story_block`.
- `src/vestigo/db/migrations/versions/0025_view_deleted_at.py` — **created**, nullable `deleted_at`.
- `src/vestigo/stories/refs.py` — `view_ref` liveness check.
- `src/vestigo/api/routers/cases.py` — `delete_view` response gains `hidden`.

**Backend — tests created/modified**
- `tests/test_queries.py` — `empty` mode SQL shape.
- `tests/test_empty_filter_clickhouse.py` — **created**, live-ClickHouse semantics.
- `tests/test_events_api.py` or the nearest router test module — mode validation (locate in Task 1).
- `tests/test_view_lifecycle.py` — **created**, soft-delete/purge.

**Frontend — modified**
- `frontend/src/api/types.ts` — `FieldMatchMode`, `View`.
- `frontend/src/components/explorer/FilterRail.tsx` — `∅` mode, views list search + delete.
- `frontend/src/components/explorer/FilterChips.tsx` — empty-mode chip copy.
- `frontend/src/components/explorer/EventGrid.tsx` — scroll restructure + sortable header.
- `frontend/src/components/explorer/SaveViewDialog.tsx` — writes `columns`.
- `frontend/src/lib/queryParams.ts` — `viewPayloadColumns`.
- `frontend/src/pages/ExplorerPage.tsx` — wiring for reorder, view-apply columns, dialog prop.
- `frontend/src/api/views.ts` — `delete` return type.

**Frontend — tests created/modified**
- `frontend/src/test/filterRail.test.tsx` — `∅` mode rows.
- `frontend/src/test/eventGridHeader.test.tsx` — **created**, scroll structure + reorder.
- `frontend/src/test/savedViewColumns.test.ts` — **created**, payload round-trip.
- `frontend/src/test/savedViewsList.test.tsx` — **created**, search + delete.

**Docs — modified**
- `docs/ROADMAP.md`, `docs/PROGRESS.md`, `docs/STORIES.md`.

---

### Task 1: The `empty` match mode, backend

**Files:**
- Modify: `src/vestigo/db/queries.py:539` (`VALID_MATCH_MODES`), `:826` (`_match_column_expr`), `:843` (`add_field_filter`), `:883` (`add_field_exclusion`)
- Modify: `src/vestigo/api/routers/events.py:242` (`_VALID_FILTER_MODES`)
- Test: `tests/test_queries.py`
- Test: `tests/test_empty_filter_clickhouse.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: the wire contract every later frontend task targets — `field_filters={"k": [""]}` with `filter_modes={"k": "empty"}` means "k has no value"; `field_exclusions={"k": [""]}` with `exclusion_modes={"k": "empty"}` means "k has a value". `VALID_MATCH_MODES` becomes `("exact", "wildcard", "regex", "empty")`.

- [ ] **Step 1: Confirm no third mode allowlist exists**

Run:

```bash
rg -n '"wildcard"' src/vestigo | rg -v 'db/queries.py|routers/events.py'
```

Expected: only doc strings, description text and `_glob_to_like` call sites — no other set/tuple of valid modes. If a third allowlist turns up (e.g. in `agent/tools.py`), add `"empty"` there too and mention it in the commit message.

- [ ] **Step 2: Write the failing query-builder tests**

Append to `tests/test_queries.py`:

```python
def test_empty_mode_filter_matches_absent_or_blank_attribute(
    service: EventQueryService,
) -> None:
    """`empty` include compares the string-cast column against '' so a missing
    Map key (which ClickHouse reads as '') and a stored blank both match."""
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"user_agent": [""]},
            filter_modes={"user_agent": "empty"},
        )
    )
    query, params = _last_query(service)
    assert "ifNull(" in query
    assert ", '') = ''" in query
    assert params is not None
    assert "user_agent" in params.values()


def test_empty_mode_exclusion_keeps_only_rows_with_a_value(
    service: EventQueryService,
) -> None:
    service.query(
        EventQuery(
            case_id="case-1",
            field_exclusions={"user_agent": [""]},
            exclusion_modes={"user_agent": "empty"},
        )
    )
    query, _ = _last_query(service)
    assert ", '') != ''" in query


def test_empty_mode_ignores_the_placeholder_value(service: EventQueryService) -> None:
    """The value list is a placeholder — it must never reach the SQL as a
    literal, or an analyst could smuggle a comparison in through it."""
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"user_agent": ["not-a-real-value"]},
            filter_modes={"user_agent": "empty"},
        )
    )
    query, params = _last_query(service)
    assert ", '') = ''" in query
    assert params is not None
    assert "not-a-real-value" not in params.values()


def test_empty_mode_with_no_values_still_emits_a_predicate(
    service: EventQueryService,
) -> None:
    """An empty value list must not silently drop the predicate the way it
    does for exact/wildcard/regex — there is nothing to have values for."""
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"user_agent": []},
            filter_modes={"user_agent": "empty"},
        )
    )
    query, _ = _last_query(service)
    assert ", '') = ''" in query


def test_empty_mode_on_a_top_level_column_is_null_safe(
    service: EventQueryService,
) -> None:
    """timestamp is not a String column; without the cast + ifNull the
    comparison would either fail to compile or evaluate to NULL, which would
    drop the row from BOTH sides of the filter."""
    service.query(
        EventQuery(
            case_id="case-1",
            field_filters={"display_name": [""]},
            filter_modes={"display_name": "empty"},
        )
    )
    query, _ = _last_query(service)
    assert "ifNull(toString(display_name), '') = ''" in query
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:

```bash
uv run pytest tests/test_queries.py -k empty_mode -v
```

Expected: FAIL — `ValueError: invalid match mode: 'empty'` from `add_field_filter`.

- [ ] **Step 4: Widen the query-builder allowlist**

In `src/vestigo/db/queries.py`, replace the `VALID_MATCH_MODES` definition (line 537-539):

```python
# Match modes accepted for field filters/exclusions. "exact" is the implied
# default everywhere a mode map has no entry for a field key. "empty" is the
# presence predicate: it carries no value, and its value list is a placeholder.
VALID_MATCH_MODES = ("exact", "wildcard", "regex", "empty")
```

- [ ] **Step 5: Make the column expression null-safe for `empty`**

Replace `_ParameterizedQueryBuilder._match_column_expr` in `src/vestigo/db/queries.py`:

```python
    def _match_column_expr(self, key: str, mode: str) -> str:
        """Column expression for a field predicate under *mode*.

        Exact keeps typed comparison (no toString) so `=`/`NOT IN` compare
        against typed literals; wildcard/regex are string operations and need
        non-string top-level columns cast. ``empty`` casts for the same reason
        and additionally coalesces: a NULL top-level column would otherwise
        make ``= ''`` and ``!= ''`` both evaluate to NULL, dropping the row
        from the include and the exclude side alike. An absent ``attributes``
        Map key already reads as ``''`` in ClickHouse, so one predicate covers
        "key missing" and "key present but blank".
        """
        if mode == "exact":
            return self._column_expr(key)
        expr = _field_column_expr(
            key,
            self.parameters,
            self._param_name,
            cast_non_string=True,
            field_mappings=self._field_mappings,
            source_offsets=self._source_offsets,
        )
        return f"ifNull({expr}, '')" if mode == "empty" else expr
```

- [ ] **Step 6: Add the `empty` branch to both predicate builders**

In `add_field_filter`, insert the branch **before** the existing `if not values: return` guard, so an empty value list still emits a predicate:

```python
        if mode == "empty":
            self.conditions.append(f"{self._match_column_expr(key, mode)} = ''")
            return
        if not values:
            return
        column = self._match_column_expr(key, mode)
```

Extend its docstring with a sentence:

```
        ``empty`` ignores *values* entirely and asks only whether the field
        has a value at all.
```

In `add_field_exclusion`, insert the mirror branch as the method's first statement, before `column = self._match_column_expr(...)`:

```python
        if mode == "empty":
            self.conditions.append(f"{self._match_column_expr(key, mode)} != ''")
            return
```

Extend its docstring the same way:

```
        ``empty`` ignores *values* and keeps only rows where the field has a
        value — the negative form of the presence predicate.
```

- [ ] **Step 7: Run the query-builder tests**

Run:

```bash
uv run pytest tests/test_queries.py -k empty_mode -v
```

Expected: PASS (5 tests).

- [ ] **Step 8: Widen the router allowlist and prove the boundary**

In `src/vestigo/api/routers/events.py:242`:

```python
_VALID_FILTER_MODES = {"exact", "wildcard", "regex", "empty"}
```

`_validate_field_regexes` already skips every mode that is not `"regex"`, so `empty` needs no change there — confirm by reading it, do not edit it.

Update the two API descriptions that enumerate modes so the OpenAPI schema stays truthful — `events.py:637-639` and `:825`:

```python
            "JSON object mapping a `filters` field to its match mode "
            '("exact"|"wildcard"|"regex"|"empty"), e.g. {"src_ip":"wildcard"}. '
```

```python
        description='JSON match-mode map for `filters`, e.g. {"src_ip":"wildcard"} or {"ua":"empty"}.',
```

- [ ] **Step 9: Add the router validation test**

Locate the module that already tests `_parse_modes_object` or an invalid match mode:

```bash
rg -ln 'invalid match mode' tests
```

Add to that module (adapting the fixture names it already uses; if no module tests it, create `tests/test_events_filter_modes.py` with a direct unit test of the parser):

```python
def test_empty_is_an_accepted_filter_mode() -> None:
    from vestigo.api.routers.events import _parse_modes_object

    assert _parse_modes_object('{"user_agent": "empty"}') == {"user_agent": "empty"}


def test_unknown_filter_mode_is_still_rejected() -> None:
    import pytest
    from fastapi import HTTPException

    from vestigo.api.routers.events import _parse_modes_object

    with pytest.raises(HTTPException) as exc:
        _parse_modes_object('{"user_agent": "blank"}')
    assert exc.value.status_code == 400
```

- [ ] **Step 10: Write the live-ClickHouse semantics test**

Create `tests/test_empty_filter_clickhouse.py`. Mirror the bootstrap of `tests/test_search_blob_clickhouse.py` — read that file first and copy its store/table setup fixture verbatim, changing only the case id and the events:

```python
"""Live-ClickHouse tests for the `empty` field match mode.

Proves that "no value" means both "attribute key absent" and "attribute
present but blank", that a whitespace-only value counts as a value (it is
what the source recorded — collapsing it into "absent" would make the filter
lie about the evidence), and that include and exclude partition the rows
exactly. Requires the dev compose stack (skipped when ClickHouse is
unreachable), same pattern as ``test_search_blob_clickhouse.py``.
"""

from __future__ import annotations

import uuid

import pytest

from vestigo.db.queries import EventQuery, EventQueryService

pytestmark = pytest.mark.clickhouse

CASE_ID = f"tc-empty-{uuid.uuid4().hex[:8]}"
SOURCE_ID = "src-empty"


def _ids(result) -> set[str]:
    return {row["event_id"] for row in result.events}


@pytest.mark.asyncio
async def test_empty_include_matches_absent_and_blank(service: EventQueryService) -> None:
    result = service.query(
        EventQuery(
            case_id=CASE_ID,
            field_filters={"user_agent": [""]},
            filter_modes={"user_agent": "empty"},
        )
    )
    assert _ids(result) == {"e-absent", "e-blank"}


@pytest.mark.asyncio
async def test_empty_exclude_keeps_whitespace_and_real_values(
    service: EventQueryService,
) -> None:
    result = service.query(
        EventQuery(
            case_id=CASE_ID,
            field_exclusions={"user_agent": [""]},
            exclusion_modes={"user_agent": "empty"},
        )
    )
    assert _ids(result) == {"e-space", "e-curl"}
```

The fixture must insert exactly four events into the case: `e-absent` with no `user_agent` attribute at all, `e-blank` with `user_agent=""`, `e-space` with `user_agent=" "`, and `e-curl` with `user_agent="curl/8.4"`.

- [ ] **Step 11: Run the ClickHouse test**

Run:

```bash
podman compose up -d
uv run pytest tests/test_empty_filter_clickhouse.py -v
```

Expected: PASS (2 tests). If ClickHouse is unreachable the module skips — a skip is **not** a pass; bring the stack up and get real green before committing.

- [ ] **Step 12: Lint and run the affected suites**

Run:

```bash
uv run ruff check . && uv run ruff format --check .
uv run pytest tests/test_queries.py -q
```

Expected: no lint findings; `test_queries.py` fully green (the new tests plus every pre-existing one).

- [ ] **Step 13: Commit**

```bash
git add src/vestigo/db/queries.py src/vestigo/api/routers/events.py tests/test_queries.py tests/test_empty_filter_clickhouse.py
git commit -m "feat(filters): add the 'empty' field match mode

A fourth match mode beside exact/wildcard/regex that asks whether a field
has a value at all: include keeps rows where it does not, exclude keeps
rows where it does. It rides the existing per-key mode maps, so URLs,
saved views, export, bulk-annotate and the histogram inherit it.

The predicate is ifNull(toString(col), '') = '' -- an absent attributes
Map key already reads as '' in ClickHouse, and the coalesce stops a NULL
top-level column from evaluating the comparison to NULL and dropping the
row from both sides. Whitespace-only values count as values.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 2: The `∅` mode in the filter rail

**Files:**
- Modify: `frontend/src/api/types.ts:996` (`FieldMatchMode`)
- Modify: `frontend/src/components/explorer/FilterRail.tsx:41-58` (`MATCH_MODE_OPTIONS`, `MODE_PLACEHOLDER`), `:199` (`addFilter`), `:222` (`addExclusion`), the two value-input blocks
- Modify: `frontend/src/components/explorer/FilterChips.tsx:21` (`MODE_BADGE`), `:108-128` (chip loops)
- Test: `frontend/src/test/filterRail.test.tsx`

**Interfaces:**
- Consumes: the wire contract from Task 1.
- Produces: `FieldMatchMode = "wildcard" | "regex" | "empty"`. Nothing later depends on the rail internals.

- [ ] **Step 1: Write the failing rail tests**

Append to `frontend/src/test/filterRail.test.tsx`, inside the existing `describe("FilterRail field match modes", ...)` block (it already has a `renderRail` helper in scope):

```tsx
  it("adds an empty-mode include filter with no value typed", () => {
    const { onChange } = renderRail();
    fireEvent.change(screen.getByPlaceholderText("field"), {
      target: { value: "user_agent" },
    });
    fireEvent.click(screen.getAllByTitle("No value — field is absent or blank")[0]);
    fireEvent.click(screen.getAllByRole("button", { name: /add filter/i })[0]);
    expect(onChange).toHaveBeenCalledWith(
      expect.objectContaining({
        filters: { user_agent: [""] },
        filterModes: { user_agent: "empty" },
      }),
    );
  });

  it("replaces the value input with a static label in empty mode", () => {
    renderRail();
    fireEvent.change(screen.getByPlaceholderText("field"), {
      target: { value: "user_agent" },
    });
    fireEvent.click(screen.getAllByTitle("No value — field is absent or blank")[0]);
    expect(screen.getByText("(empty)")).toBeInTheDocument();
    expect(screen.queryByPlaceholderText("value")).not.toBeInTheDocument();
  });
```

The exact accessible names of the two add buttons and the field-key inputs differ between the include and exclude rows — read `FilterRail.tsx:560-620` and adjust the queries to whatever the markup actually exposes rather than adding labels to make these queries work. If the add button has no accessible name, give it one (`aria-label="Add filter"` / `aria-label="Add exclusion"`); that is a genuine accessibility improvement, not test-shaped code.

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
cd frontend && npm run test -- filterRail
```

Expected: FAIL — no element with the `∅` tooltip.

- [ ] **Step 3: Widen the mode type**

In `frontend/src/api/types.ts`, replace the `FieldMatchMode` definition:

```ts
/**
 * Non-default field-filter match modes; "exact" is implied by absence.
 * "empty" is a presence predicate rather than a comparison: it carries no
 * value, and its value list is a `[""]` placeholder on the wire.
 */
export type FieldMatchMode = "wildcard" | "regex" | "empty";
```

- [ ] **Step 4: Add the mode to the rail's control**

In `frontend/src/components/explorer/FilterRail.tsx`, extend `MATCH_MODE_OPTIONS`:

```tsx
  {
    mode: "empty",
    label: "∅",
    tooltip: "No value — field is absent or blank",
  },
```

and `MODE_PLACEHOLDER`:

```tsx
  empty: "(empty)",
```

- [ ] **Step 5: Let both add-handlers run without a value**

In `addFilter`, replace the first four lines:

```tsx
  const addFilter = (value?: string) => {
    const key = fieldKey.trim();
    if (!key) return;
    // Empty mode is a presence predicate — there is no value to type, and the
    // one on the wire is a placeholder the backend ignores.
    const v = fieldMode === "empty" ? "" : (value ?? fieldVal).trim();
    if (fieldMode !== "empty" && !v) return;
```

In `addExclusion`, the mirror:

```tsx
  const addExclusion = (value?: string) => {
    const key = excludeKey.trim();
    if (!key) return;
    const v = excludeMode === "empty" ? "" : (value ?? excludeVal).trim();
    if (excludeMode !== "empty" && !v) return;
```

The rest of both functions is unchanged: `"exact"` is still deleted from the mode map and every other mode still stored, so `empty` lands in `filterModes` / `exclusionModes` on its own.

- [ ] **Step 6: Swap the value input for a static label**

In both the include row (around `FilterRail.tsx:575`) and the exclude row (around `:600`), wrap the value `Input`/autocomplete in a conditional. For the include row:

```tsx
            {fieldMode === "empty" ? (
              <span className="flex-1 rounded border border-dashed border-[var(--color-border-strong)] px-2 py-1 text-xs text-[var(--color-fg-muted)] select-none">
                (empty)
              </span>
            ) : (
              /* the existing value input, unchanged */
            )}
```

and the same for the exclude row against `excludeMode`. Read the surrounding JSX before editing — both rows currently render a suggestion-backed input, and the whole element goes inside the `else` branch.

- [ ] **Step 7: Give the chips honest copy**

In `frontend/src/components/explorer/FilterChips.tsx`, extend `MODE_BADGE` so the type stays exhaustive:

```tsx
  empty: { label: "∅", tooltip: "Presence filter — no value at all" },
```

Then replace the two chip loops so an empty-mode chip reads as a sentence rather than as `key: ""`:

```tsx
  for (const [k, vs] of Object.entries(filters.filters ?? {})) {
    if (filters.filterModes?.[k] === "empty") {
      chips.push({
        label: k,
        value: "is empty",
        onRemove: remove("filters", k, vs[0] ?? ""),
        variant: "include",
      });
      continue;
    }
    for (const v of vs) {
      chips.push({
        label: k,
        value: v,
        onRemove: remove("filters", k, v),
        variant: "include",
        mode: filters.filterModes?.[k],
      });
    }
  }
  for (const [k, vs] of Object.entries(filters.exclusions ?? {})) {
    if (filters.exclusionModes?.[k] === "empty") {
      chips.push({
        label: k,
        value: "has a value",
        onRemove: remove("exclusions", k, vs[0] ?? ""),
        variant: "exclude",
      });
      continue;
    }
    for (const v of vs) {
      chips.push({
        label: `!${k}`,
        value: v,
        onRemove: remove("exclusions", k, v),
        variant: "exclude",
        mode: filters.exclusionModes?.[k],
      });
    }
  }
```

Note the exclude branch deliberately drops the `!` prefix for empty mode: `!user_agent: has a value` would read as the opposite of what it does.

- [ ] **Step 8: Run the tests, typecheck and lint**

Run:

```bash
cd frontend && npm run test -- filterRail && npm run typecheck && npm run lint
```

Expected: the two new tests PASS, every pre-existing `filterRail` test still PASSes, no type errors, no lint findings.

- [ ] **Step 9: Verify the removal path by hand**

Run `uv run vestigo-web` in one shell and `npm run dev` in `frontend/` in another, open a timeline, add an `∅` include filter on a field, confirm the chip reads `field is empty`, click its `×`, and confirm the filter and its mode entry both clear (the grid returns to the full result set).

- [ ] **Step 10: Commit**

```bash
git add frontend/src/api/types.ts frontend/src/components/explorer/FilterRail.tsx frontend/src/components/explorer/FilterChips.tsx frontend/src/test/filterRail.test.tsx
git commit -m "feat(explorer): surface the empty match mode in the filter rail

A fourth match-mode button beside =, * and .*; picking it replaces the
value input with a static (empty) label, because the value is a
placeholder the backend ignores. The include row means 'is empty', the
exclude row means 'has a value', and the chips say exactly that rather
than rendering an empty string.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 3: Header follows horizontal scroll

**Files:**
- Modify: `frontend/src/components/explorer/EventGrid.tsx:680-690` (virtualizer), `:703-715` (`handleScroll`), `:812-860` (render root), `:894-900` (row positioning)
- Test: `frontend/src/test/eventGridHeader.test.tsx` (create)

**Interfaces:**
- Consumes: nothing.
- Produces: the DOM structure Task 4 hangs the sortable header off — a single scroll element containing one `minWidth`-constrained wrapper, whose first child is the `sticky top-0` header row and whose second child is the virtualized body. Exposes `data-testid="grid-scroll"`, `data-testid="grid-header"` and `data-testid="grid-body"` for tests.

- [ ] **Step 1: Write the failing structure test**

Create `frontend/src/test/eventGridHeader.test.tsx`:

```tsx
/**
 * The event grid's header must live inside the scroll element, so horizontal
 * scrolling moves it with the columns, and both header and rows must span the
 * content width rather than the viewport width.
 */
import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { EventGrid } from "@/components/explorer/EventGrid";
import type { Event } from "@/api/types";

function evt(i: number): Event {
  return {
    event_id: `e${i}`,
    case_id: "c1",
    source_id: "s1",
    timestamp: `2026-07-0${(i % 9) + 1}T00:00:00Z`,
    artifact: "webserver:access",
    artifact_long: "",
    display_name: "",
    message: `line ${i}`,
    timestamp_desc: "",
    tags: [],
    attributes: {},
  } as unknown as Event;
}

function renderGrid(visibleColumns: string[]) {
  render(
    <TooltipProvider>
      <EventGrid
        events={[evt(1), evt(2)]}
        annotations={new Map()}
        selectedIds={new Set()}
        onToggleSelect={vi.fn()}
        onToggleSelectAll={vi.fn()}
        expandedId={null}
        onExpand={vi.fn()}
        onLoadMore={vi.fn()}
        onLoadEarlier={vi.fn()}
        hasPreviousPage={false}
        hasNextPage={false}
        isFetching={false}
        visibleColumns={visibleColumns}
        sortDir="desc"
        onSortToggle={vi.fn()}
        caseId="c1"
      />
    </TooltipProvider>,
  );
}

describe("EventGrid scroll structure", () => {
  it("nests the header inside the scroll element", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const scroller = screen.getByTestId("grid-scroll");
    expect(scroller).toContainElement(screen.getByTestId("grid-header"));
  });

  it("sticks the header to the top of the scroller", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    expect(screen.getByTestId("grid-header").className).toContain("sticky");
  });

  it("sizes the shared wrapper to the content width, not the viewport", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const wrapper = screen.getByTestId("grid-content");
    expect(wrapper.style.minWidth).toMatch(/^\d+px$/);
  });
});
```

The prop list above must match `EventGrid`'s actual required props — read its `interface Props` (`EventGrid.tsx:50-90`) and add any required prop this omits. Optional props stay omitted.

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd frontend && npm run test -- eventGridHeader
```

Expected: FAIL — no element with `data-testid="grid-scroll"`.

- [ ] **Step 3: Measure the body's offset inside the scroller**

Add to `EventGrid.tsx`, next to the existing `parentRef` declaration:

```tsx
  const bodyRef = useRef<HTMLDivElement | null>(null);
  // Distance from the top of the scrolled content to the first row. The
  // header is part of that content now (so it can scroll horizontally with
  // the columns), which shifts every vertical offset the virtualizer and the
  // scroll handler reason about. Measured rather than hardcoded: the header's
  // height follows the font and the density setting.
  const [bodyOffset, setBodyOffset] = useState(0);
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const measure = () => setBodyOffset(el.offsetTop);
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
```

Add `useLayoutEffect` and `useState` to the existing React import if absent.

- [ ] **Step 4: Teach the virtualizer about the offset**

Replace the `useVirtualizer` call:

```tsx
  const rowVirtualizer = useVirtualizer({
    count: rows.length,
    getScrollElement: () => parentRef.current,
    estimateSize: () => ROW_HEIGHT,
    overscan: OVERSCAN,
    // The header occupies the top of the scrolled content; without this the
    // virtualizer would believe row 0 starts at scrollTop 0 and render the
    // wrong window by exactly the header's height.
    scrollMargin: bodyOffset,
  });
```

- [ ] **Step 5: Restructure the render root**

Replace the JSX from the `{/* Header row */}` comment through the opening of the virtualized body, so the header and the body share one width-constrained wrapper inside the scroll element:

```tsx
    <div className="flex flex-1 min-w-0 flex-col h-full">
      <div
        ref={parentRef}
        data-testid="grid-scroll"
        data-tour="event-grid"
        className="flex-1 overflow-auto"
        onScroll={handleScroll}
      >
        {/* One wrapper at content width, so the header and every row span the
          * same box. Without it the header could not scroll with the columns
          * and rows would be sized to the viewport, cutting their background,
          * hover state and borders off at the right edge. */}
        <div
          data-testid="grid-content"
          style={{ minWidth: table.getTotalSize() }}
        >
          {/* Header row — inside the scroller so it tracks horizontal scroll,
            * sticky so it still holds its place vertically. */}
          <div
            data-testid="grid-header"
            className="sticky top-0 z-20 flex border-b border-[var(--color-border)] bg-[var(--color-bg-surface)]"
          >
            {/* the existing header-cell map, unchanged */}
          </div>

          <div
            ref={bodyRef}
            data-testid="grid-body"
            style={{ height: totalHeight, position: "relative" }}
          >
            {/* the existing virtualItems map, with the row style change below */}
          </div>
        </div>
      </div>
      {/* the existing footer row, unchanged, still outside the scroller */}
    </div>
```

The header-cell map and the row map move verbatim — do not rewrite their contents in this step.

- [ ] **Step 6: Subtract the scroll margin from row offsets**

In the row map, replace `top: vItem.start` with:

```tsx
                  top: vItem.start - bodyOffset,
```

`vItem.start` is measured in scroll-element space once `scrollMargin` is set, while the row is positioned inside `grid-body` — the subtraction converts between the two.

- [ ] **Step 7: Correct the topmost-row computation**

In `handleScroll` (`EventGrid.tsx:703-715`), replace the index expression so the header's height does not count as scrolled-past rows:

```tsx
        ? (rows[
            Math.min(
              rows.length - 1,
              Math.max(0, Math.floor((el.scrollTop - bodyOffset) / ROW_HEIGHT)),
            )
          ]
```

Read the surrounding expression before editing — the existing `Math.min`/`Math.max` clamp stays, only the divided quantity changes. Add `bodyOffset` to the handler's dependency array if it has one.

The prepend anchor (`:734`, `:745`) needs **no** change: it stores and restores a `scrollTop` delta, and a constant offset cancels. Confirm this by reading it rather than assuming, and leave a one-line comment saying so if it is not obvious.

- [ ] **Step 8: Run the structure tests**

Run:

```bash
cd frontend && npm run test -- eventGridHeader && npm run typecheck
```

Expected: 3 tests PASS, no type errors.

- [ ] **Step 9: Run the full frontend suite**

Run:

```bash
cd frontend && npm run test
```

Expected: fully green. `explorerRoutineCollapse` and any test asserting grid scroll behavior are the ones most likely to break — if one does, the restructure changed real behavior and the test is right until proven otherwise.

- [ ] **Step 10: Verify the scroll behavior by hand**

This is the step the whole task exists for; a green suite does not cover it. With `npm run dev` running, open a timeline and add enough columns to overflow horizontally, then confirm all of:

1. Scrolling right moves the header with the columns.
2. Scrolling down keeps the header pinned.
3. Row hover highlight spans the full content width, not just the viewport.
4. Scrolling to the bottom loads the next page; scrolling to the top loads the earlier one, and the viewport does not jump when it arrives.
5. The histogram's current-position indicator tracks the topmost visible row — not offset by roughly one row.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/components/explorer/EventGrid.tsx frontend/src/test/eventGridHeader.test.tsx
git commit -m "fix(explorer): scroll the grid header with the columns

The header row was a sibling of the scroll container, so horizontal
scrolling moved the columns out from under it. It now lives inside the
scroller under a shared content-width wrapper, sticky to the top.

The same wrapper fixes rows being sized to the viewport rather than the
content, which cut their background, hover state and borders off at the
right edge whenever the columns overflowed.

The header is part of the scrolled content now, so the virtualizer takes
a scrollMargin and the topmost-row computation that drives the histogram
position indicator subtracts the same offset.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 4: Drag-reorder columns

**Files:**
- Modify: `frontend/src/components/explorer/EventGrid.tsx` (header block from Task 3, props, imports)
- Modify: `frontend/src/pages/ExplorerPage.tsx:1445` (grid props)
- Test: `frontend/src/test/eventGridHeader.test.tsx`

**Interfaces:**
- Consumes: the `grid-header` structure from Task 3.
- Produces: `EventGrid` gains a required-when-reorderable prop `onReorderColumns?: (next: string[]) => void`, called with the full reordered visible-column array (grid-internal ids excluded, since they were never in it).

- [ ] **Step 1: Write the failing reorder test**

Append to `frontend/src/test/eventGridHeader.test.tsx`:

```tsx
describe("EventGrid column reorder", () => {
  it("makes each visible column header a drag handle", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const header = screen.getByTestId("grid-header");
    expect(header.querySelectorAll("[data-column-drag]")).toHaveLength(3);
  });

  it("does not make the grid-internal columns draggable", () => {
    renderGrid(["timestamp", "artifact", "message"]);
    const header = screen.getByTestId("grid-header");
    const ids = [...header.querySelectorAll("[data-column-drag]")].map(
      (el) => el.getAttribute("data-column-drag"),
    );
    expect(ids).toEqual(["timestamp", "artifact", "message"]);
    expect(ids).not.toContain("_select");
    expect(ids).not.toContain("_annotations");
    expect(ids).not.toContain("_expand");
  });
});
```

`renderGrid` needs an `onReorderColumns` prop added to its `EventGrid` call — add `onReorderColumns: vi.fn()` there and return it so later assertions can reach it.

A jsdom test cannot faithfully simulate a dnd-kit pointer drag, so do **not** write one that asserts the reordered array — it would test dnd-kit, not this code. Instead extract the reducer and test it directly (Step 2).

- [ ] **Step 2: Write the failing reducer test**

Append to the same file:

```tsx
import { reorderColumns } from "@/components/explorer/EventGrid";

describe("reorderColumns", () => {
  it("moves the dragged column to the target's position", () => {
    expect(reorderColumns(["a", "b", "c"], "a", "c")).toEqual(["b", "c", "a"]);
    expect(reorderColumns(["a", "b", "c"], "c", "a")).toEqual(["c", "a", "b"]);
  });

  it("returns the input unchanged when the ids are the same", () => {
    const cols = ["a", "b", "c"];
    expect(reorderColumns(cols, "b", "b")).toBe(cols);
  });

  it("returns the input unchanged when either id is not a visible column", () => {
    const cols = ["a", "b", "c"];
    expect(reorderColumns(cols, "a", "_expand")).toBe(cols);
    expect(reorderColumns(cols, "zz", "b")).toBe(cols);
  });
});
```

- [ ] **Step 3: Run the tests to verify they fail**

Run:

```bash
cd frontend && npm run test -- eventGridHeader
```

Expected: FAIL — `reorderColumns` is not exported, no `data-column-drag` attributes.

- [ ] **Step 4: Add the reducer**

Add near the top of `EventGrid.tsx`, below the existing module constants:

```tsx
/**
 * Move `activeId` to `overId`'s slot in a visible-column list.
 *
 * Returns the input array by identity when nothing moves — same id, or an id
 * that is not a visible column (the grid-internal `_select`/`_annotations`/
 * `_expand` headers are pinned and never enter the sortable set, but a stray
 * drop id must not corrupt the list either).
 */
export function reorderColumns(
  columns: string[],
  activeId: string,
  overId: string,
): string[] {
  if (activeId === overId) return columns;
  const from = columns.indexOf(activeId);
  const to = columns.indexOf(overId);
  if (from === -1 || to === -1) return columns;
  const next = [...columns];
  next.splice(to, 0, next.splice(from, 1)[0]);
  return next;
}
```

- [ ] **Step 5: Make the header cells sortable**

Add the imports:

```tsx
import {
  DndContext,
  KeyboardSensor,
  PointerSensor,
  closestCenter,
  useSensor,
  useSensors,
  type DragEndEvent,
} from "@dnd-kit/core";
import {
  SortableContext,
  horizontalListSortingStrategy,
  sortableKeyboardCoordinates,
  useSortable,
} from "@dnd-kit/sortable";
import { CSS } from "@dnd-kit/utilities";
```

(`StoryEditor.tsx:5-18` imports the same set — `@dnd-kit/utilities` resolves transitively and is not listed in `package.json`, matching existing practice.)

Extract the existing header cell into a component in the same file, so it can call `useSortable`:

```tsx
/** One draggable header cell. Pinned grid-internal columns render through
 *  the plain branch in the header map instead of this. */
function SortableHeaderCell({
  header,
  children,
}: {
  header: Header<Event, unknown>;
  children: React.ReactNode;
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } =
    useSortable({ id: header.column.id });
  return (
    <div
      ref={setNodeRef}
      data-column-drag={header.column.id}
      className={cn(
        "relative min-w-0 overflow-hidden px-[var(--grid-cell-x)] py-2 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)] select-none",
        isDragging && "opacity-50",
      )}
      style={{
        width: header.column.id === "message" ? undefined : header.getSize(),
        flex: header.column.id === "message" ? "1 1 0" : `0 0 ${header.getSize()}px`,
        transform: CSS.Transform.toString(transform),
        transition,
      }}
      {...attributes}
      {...listeners}
    >
      {children}
    </div>
  );
}
```

`Header` comes from `@tanstack/react-table` — add it to that import. `children` is the existing rtl-clipped label span plus the resize handle, moved verbatim from the current cell body.

- [ ] **Step 6: Wire the DndContext**

In the header block from Task 3, wrap the cell map:

```tsx
  const sensors = useSensors(
    // 8px, not the story editor's 4: this header also carries the timestamp
    // sort button, and a plain click on it must never arm a drag.
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const handleDragEnd = useCallback(
    (event: DragEndEvent) => {
      const { active, over } = event;
      if (!over || !onReorderColumns) return;
      const next = reorderColumns(visibleColumns, String(active.id), String(over.id));
      if (next !== visibleColumns) onReorderColumns(next);
    },
    [visibleColumns, onReorderColumns],
  );
```

and in the JSX:

```tsx
          <div
            data-testid="grid-header"
            className="sticky top-0 z-20 flex border-b border-[var(--color-border)] bg-[var(--color-bg-surface)]"
          >
            <DndContext
              sensors={sensors}
              collisionDetection={closestCenter}
              onDragEnd={handleDragEnd}
            >
              <SortableContext
                items={visibleColumns}
                strategy={horizontalListSortingStrategy}
              >
                {table.getHeaderGroups().map((hg) =>
                  hg.headers.map((h) =>
                    visibleColumns.includes(h.column.id) ? (
                      <SortableHeaderCell key={h.id} header={h}>
                        {/* label span + resize handle, as today */}
                      </SortableHeaderCell>
                    ) : (
                      /* the plain pinned cell, as today */
                    ),
                  ),
                )}
              </SortableContext>
            </DndContext>
          </div>
```

- [ ] **Step 7: Stop the resize handle from arming a drag**

The handle already stops `mousedown` and `touchstart`; dnd-kit's `PointerSensor` listens on `pointerdown`, which those do not cover. Add it:

```tsx
                <div
                  onPointerDown={(e) => e.stopPropagation()}
                  onMouseDown={(e) => { e.stopPropagation(); h.getResizeHandler()(e); }}
                  onTouchStart={(e) => { e.stopPropagation(); h.getResizeHandler()(e); }}
                  onClick={(e) => e.stopPropagation()}
```

- [ ] **Step 8: Add the prop and wire the page**

In `EventGrid`'s `interface Props`:

```tsx
  /** Called with the full reordered visible-column list after a header drag.
   *  Omit to render a non-reorderable grid. */
  onReorderColumns?: (next: string[]) => void;
```

In `ExplorerPage.tsx`, add the store setter next to the existing column state (`:418`):

```tsx
  const setVisibleColumns = useUiStore((s) => s.setVisibleColumns);
```

and pass it to the grid (`:1445`, beside `visibleColumns`):

```tsx
                  onReorderColumns={(next) => setVisibleColumns(tlKey, next)}
```

This writes the same per-timeline override a manual column choice writes, so the precedence rules in `lib/columns.ts` apply unchanged.

- [ ] **Step 9: Run the tests, typecheck and lint**

Run:

```bash
cd frontend && npm run test && npm run typecheck && npm run lint
```

Expected: all green, including every pre-existing test.

- [ ] **Step 10: Verify the drag by hand**

With `npm run dev` running: drag a column header onto another position and confirm the grid re-lays out and the order survives a page reload (it is in localStorage). Then confirm the three things that could have broken — clicking the timestamp header still toggles sort, dragging the resize handle still resizes without reordering, and tabbing to a header then pressing space and arrow keys reorders it.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/components/explorer/EventGrid.tsx frontend/src/pages/ExplorerPage.tsx frontend/src/test/eventGridHeader.test.tsx
git commit -m "feat(explorer): reorder grid columns by dragging their headers

Visible columns were already an ordered array driving the render order,
so a drag just rewrites it -- the same per-timeline override a manual
column choice writes, under the precedence rules that already exist.

The checkbox, annotation and expand columns stay pinned outside the
sortable set. The pointer sensor takes an 8px activation distance so a
click on the timestamp sort button still sorts, and the resize handle now
stops pointerdown too, since that is the event dnd-kit listens on.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 5: Saved views carry the column layout

**Files:**
- Modify: `frontend/src/lib/queryParams.ts` (new export)
- Modify: `frontend/src/components/explorer/SaveViewDialog.tsx`
- Modify: `frontend/src/components/explorer/FilterRail.tsx:642` (apply handler)
- Modify: `frontend/src/pages/ExplorerPage.tsx:1175` (`onApplyView`), `:1547` (dialog props)
- Test: `frontend/src/test/savedViewColumns.test.ts` (create)

**Interfaces:**
- Consumes: `sanitizeColumns` from `@/stores/ui`, `setVisibleColumns` wiring from Task 4.
- Produces: `viewPayloadColumns(payload: Record<string, unknown>): string[] | undefined`; `FilterRail`'s `onApplyView` becomes `(f: EventFilters, columns?: string[]) => void`; `SaveViewDialog` gains a required `visibleColumns: string[]` prop.

- [ ] **Step 1: Write the failing helper tests**

Create `frontend/src/test/savedViewColumns.test.ts`:

```ts
/**
 * A saved view carries the column layout it was saved with. Views written
 * before this feature have no `columns` key at all, and applying one must
 * leave the analyst's current layout alone rather than blanking it.
 */
import { describe, it, expect } from "vitest";
import { viewPayloadColumns } from "@/lib/queryParams";

describe("viewPayloadColumns", () => {
  it("returns the sanitized column list", () => {
    expect(viewPayloadColumns({ columns: ["timestamp", "message"] })).toEqual([
      "timestamp",
      "message",
    ]);
  });

  it("remaps retired column ids", () => {
    expect(viewPayloadColumns({ columns: ["source", "message"] })).toEqual([
      "artifact",
      "message",
    ]);
  });

  it("dedupes repeated ids", () => {
    expect(viewPayloadColumns({ columns: ["message", "message"] })).toEqual(["message"]);
  });

  it("returns undefined for a legacy payload with no columns key", () => {
    expect(viewPayloadColumns({ filters: { a: ["b"] } })).toBeUndefined();
  });

  it("returns undefined when the key is not an array of strings", () => {
    expect(viewPayloadColumns({ columns: "message" })).toBeUndefined();
    expect(viewPayloadColumns({ columns: [1, 2] })).toBeUndefined();
  });

  it("treats a list that sanitizes to nothing as absent, not as 'no columns'", () => {
    expect(viewPayloadColumns({ columns: ["_select", "_expand"] })).toBeUndefined();
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
cd frontend && npm run test -- savedViewColumns
```

Expected: FAIL — `viewPayloadColumns` is not exported.

- [ ] **Step 3: Add the helper**

Append to `frontend/src/lib/queryParams.ts`:

```ts
/**
 * The column layout a saved view carries, or undefined when it has none.
 *
 * Deliberately separate from `viewPayloadToFilters`: columns are not filters,
 * and `filtersToViewPayload` is shared with the Visualize page's chart config
 * (`viz/lib/chartConfig.ts`), where a column layout would be meaningless.
 *
 * A list that sanitizes to nothing reads as absent rather than as "this view
 * shows no columns" — the latter is not a state the grid has, and treating it
 * as one would blank an analyst's layout on applying an old view.
 */
export function viewPayloadColumns(
  payload: Record<string, unknown>,
): string[] | undefined {
  const raw = payload.columns;
  if (!Array.isArray(raw)) return undefined;
  if (!raw.every((c) => typeof c === "string")) return undefined;
  const sanitized = sanitizeColumns(raw as string[]);
  return sanitized.length > 0 ? sanitized : undefined;
}
```

Add `import { sanitizeColumns } from "@/stores/ui";` to the file's imports. If that creates an import cycle (`stores/ui` importing from `lib/queryParams`), check with:

```bash
cd frontend && npm run typecheck
```

and if a cycle exists, move `sanitizeColumns` and its two constants into `lib/columns.ts` and re-export from `stores/ui` so both consumers import the same implementation — do not copy the function.

- [ ] **Step 4: Run the helper test**

Run:

```bash
cd frontend && npm run test -- savedViewColumns
```

Expected: 6 tests PASS.

- [ ] **Step 5: Write the columns into the saved payload**

In `SaveViewDialog.tsx`, add the prop and use it:

```tsx
interface Props {
  open: boolean;
  onClose: () => void;
  caseId: string;
  filters: EventFilters;
  /** The grid's current column layout — saved with the view so applying it
   *  later restores what the analyst was actually looking at. */
  visibleColumns: string[];
}
```

```tsx
    mutationFn: () =>
      viewsApi.create(caseId, name.trim(), filters.q ?? "", {
        ...filtersToViewPayload(filters),
        // Added here rather than inside filtersToViewPayload: the Visualize
        // page shares that helper for chart configs, where columns mean
        // nothing.
        columns: visibleColumns,
      }),
```

and add `visibleColumns` to the component's destructured props.

- [ ] **Step 6: Restore them on apply**

In `FilterRail.tsx`, widen the prop type:

```tsx
  /** Applies a saved view. The second argument is the view's column layout,
   *  or undefined for a view saved before layouts were stored. */
  onApplyView: (f: EventFilters, columns?: string[]) => void;
```

and the call site at `:642`:

```tsx
                  onClick={() => {
                    const payload = v.filter as Record<string, unknown>;
                    onApplyView(viewPayloadToFilters(payload), viewPayloadColumns(payload));
                  }}
```

Add `viewPayloadColumns` to the existing `@/lib/queryParams` import.

- [ ] **Step 7: Wire the page**

In `ExplorerPage.tsx`, replace `onApplyView={setFilters}` (`:1175`):

```tsx
          onApplyView={(f, columns) => {
            setFilters(f);
            if (columns) setVisibleColumns(tlKey, columns);
          }}
```

and pass the layout to the dialog (`:1547`):

```tsx
        visibleColumns={visibleColumns}
```

`setVisibleColumns` and `tlKey` are both already in scope from Task 4.

- [ ] **Step 8: Check the other SaveViewDialog callers**

Run:

```bash
cd frontend && rg -n 'SaveViewDialog' src
```

Every render site needs the new required prop. `findOrCreateView` in `lib/storyViews.ts` calls `viewsApi.create` directly for story pushes — leave it alone: a story block references a view for its filters, and pushing one is not a statement about the pusher's column layout. Add a one-line comment there saying so.

- [ ] **Step 9: Run the suite, typecheck and lint**

Run:

```bash
cd frontend && npm run test && npm run typecheck && npm run lint
```

Expected: all green. `storyViews.test.ts` and `findingCardSaveView.test.tsx` are the likely breakages if a call site was missed.

- [ ] **Step 10: Verify the round trip by hand**

With `npm run dev` running: set a distinctive column layout, save a view, change the columns, then apply the saved view and confirm the layout comes back. Then apply a view saved before this change (or one created through a story push, which carries no `columns`) and confirm the current layout is left alone.

- [ ] **Step 11: Commit**

```bash
git add frontend/src/lib/queryParams.ts frontend/src/components/explorer/SaveViewDialog.tsx frontend/src/components/explorer/FilterRail.tsx frontend/src/pages/ExplorerPage.tsx frontend/src/test/savedViewColumns.test.ts
git commit -m "feat(views): save and restore the grid column layout

A saved view now records the columns it was saved with, inside the view's
already-opaque JSON payload -- no migration, no backend change. Applying
it restores them into the same per-timeline override a manual column
choice writes.

The key is added at the SaveViewDialog call site rather than inside
filtersToViewPayload, which the Visualize page shares for chart configs.
Views saved before this change carry no columns key and leave the current
layout untouched, which falls out of the undefined case rather than
needing a version flag.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 6: Reference-safe view deletion, backend

**Files:**
- Modify: `src/vestigo/db/postgres.py:606` (`View` model), `:3060` (`list_views`), `:3102` (`delete_view`), `:3286` (`delete_story`), `:3687` (`delete_story_block`), `update_story_block`
- Create: `src/vestigo/db/migrations/versions/0025_view_deleted_at.py`
- Modify: `src/vestigo/stories/refs.py:36`
- Modify: `src/vestigo/api/routers/cases.py:1335` (`delete_view`)
- Test: `tests/test_view_lifecycle.py` (create)

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces: `PostgresStore.delete_view(case_id, view_id) -> str | None` returning `"deleted"`, `"hidden"` or `None` (not found) — a **changed return type**, previously `bool`. `PostgresStore.purge_orphaned_hidden_views(case_id) -> int` returning the number of rows removed. `DELETE /cases/{id}/views/{view_id}` responds `{"deleted": true, "view_id": ..., "hidden": bool}`.

- [ ] **Step 1: Write the failing lifecycle tests**

Create `tests/test_view_lifecycle.py`:

```python
"""Views outlive their deletion when a story block still embeds them.

Deleting a view a ``view_ref`` block points at would make that story's
export fail (``stories/export.py`` resolves the View live). So a referenced
view is hidden rather than removed, and swept once the last reference goes.
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from vestigo.db.postgres import PostgresStore


@pytest_asyncio.fixture()
async def store(tmp_path):
    db_path = tmp_path / "test_view_lifecycle.db"
    s = PostgresStore(url=f"sqlite+aiosqlite:///{db_path}")
    await s.init_schema()
    yield s
    await s.engine.dispose()


async def _case_with_view(store: PostgresStore) -> str:
    await store.create_case("c1", "Case One")
    await store.create_view("c1", "v1", "My View", "", {"filters": {"a": ["b"]}})
    return "v1"


async def _story_referencing(store: PostgresStore, view_id: str) -> tuple[str, str]:
    story = await store.create_story("c1", "Story One", created_by="alice")
    block = await store.create_story_block(
        story.id, kind="view_ref", content={"view_id": view_id}, user="alice"
    )
    return story.id, block.id


@pytest.mark.asyncio
async def test_unreferenced_view_is_deleted_outright(store):
    await _case_with_view(store)
    assert await store.delete_view("c1", "v1") == "deleted"
    assert await store.get_view("c1", "v1") is None


@pytest.mark.asyncio
async def test_referenced_view_is_hidden_not_removed(store):
    await _case_with_view(store)
    await _story_referencing(store, "v1")
    assert await store.delete_view("c1", "v1") == "hidden"
    # Gone from the analyst's list...
    assert [v.id for v in await store.list_views("c1")] == []
    # ...but still resolvable, which is what keeps the story's export working.
    hidden = await store.get_view("c1", "v1")
    assert hidden is not None
    assert hidden.deleted_at is not None


@pytest.mark.asyncio
async def test_deleting_unknown_view_reports_not_found(store):
    await store.create_case("c1", "Case One")
    assert await store.delete_view("c1", "nope") is None


@pytest.mark.asyncio
async def test_deleting_the_block_purges_the_hidden_view(store):
    await _case_with_view(store)
    _story_id, block_id = await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.delete_story_block(block_id)
    assert await store.get_view("c1", "v1") is None


@pytest.mark.asyncio
async def test_deleting_the_story_purges_the_hidden_view(store):
    await _case_with_view(store)
    story_id, _block_id = await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.delete_story("c1", story_id)
    assert await store.get_view("c1", "v1") is None


@pytest.mark.asyncio
async def test_repointing_the_block_purges_the_hidden_view(store):
    await _case_with_view(store)
    await store.create_view("c1", "v2", "Other View", "", {})
    _story_id, block_id = await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.update_story_block(block_id, {"view_id": "v2"}, user="alice")
    assert await store.get_view("c1", "v1") is None
    assert await store.get_view("c1", "v2") is not None


@pytest.mark.asyncio
async def test_purge_leaves_live_and_still_referenced_views_alone(store):
    await _case_with_view(store)
    await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.create_view("c1", "v2", "Live View", "", {})
    assert await store.purge_orphaned_hidden_views("c1") == 0
    assert await store.get_view("c1", "v1") is not None
    assert await store.get_view("c1", "v2") is not None


@pytest.mark.asyncio
async def test_purge_is_idempotent(store):
    await _case_with_view(store)
    _story_id, block_id = await _story_referencing(store, "v1")
    await store.delete_view("c1", "v1")
    await store.delete_story_block(block_id)
    assert await store.purge_orphaned_hidden_views("c1") == 0
```

`create_story`, `create_story_block` and `update_story_block` signatures must match the real ones — read them in `postgres.py` (search `async def create_story_block`) and fix the helper calls above before running. `delete_story_block` takes an optional `expected_version`; omitting it skips the concurrency guard, which is what these tests want.

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
uv run pytest tests/test_view_lifecycle.py -v
```

Expected: FAIL — `View` has no `deleted_at`, `purge_orphaned_hidden_views` does not exist, `delete_view` returns a bool.

- [ ] **Step 3: Add the model column**

In `src/vestigo/db/postgres.py`, add to `View` after `view_filter`:

```python
    # Set when the analyst deleted this view while a story block still
    # referenced it. The row survives so that story keeps rendering and
    # exporting; it is hard-deleted once the last reference goes away (see
    # ``purge_orphaned_hidden_views``). NULL means live.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
```

and include it in `to_dict`:

```python
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
```

- [ ] **Step 4: Generate and check the migration**

Run:

```bash
uv run alembic revision --autogenerate -m "view deleted_at"
```

Then open the generated file under `src/vestigo/db/migrations/versions/`, rename it to `0025_view_deleted_at.py`, set `revision = "0025"` / `down_revision = "0024"` to match the numbering the existing files use (read `0024_timeline_recommended_columns.py` first), and check the body is exactly an `add_column` plus `create_index` with their `drop_` counterparts in `downgrade`. Remove anything else autogenerate picked up — an unrelated diff in the revision means the model and the migration history had already drifted, and that is a separate problem, not this one's to absorb.

- [ ] **Step 5: Verify the migration runs both ways**

Run:

```bash
uv run pytest tests/test_postgres_store.py -k alembic -v
```

Expected: PASS — `test_init_schema_fresh_db_reaches_alembic_head` and `test_init_schema_adopts_pre_alembic_db` both green, proving the revision applies on SQLite.

- [ ] **Step 6: Filter the list, not the lookup**

Replace `list_views`:

```python
    async def list_views(self, case_id: str) -> list[View]:
        """Return a case's live saved views, newest first.

        Hidden views (``deleted_at`` set — deleted by an analyst while a story
        block still referenced them) are excluded: they exist only to keep
        that story rendering, and must not come back as something anyone can
        pick, rename or embed again.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(View)
                .where(View.case_id == case_id, View.deleted_at.is_(None))
                .order_by(View.created_at.desc())
            )
            return list(result.scalars().all())
```

`get_view` is deliberately **not** filtered — story rendering and export must still resolve a hidden view. Add that sentence to its docstring.

- [ ] **Step 7: Add the reference count and the conditional delete**

Add above `delete_view`:

```python
    async def _view_ref_counts(self, session: Any, case_id: str) -> dict[str, int]:
        """How many ``view_ref`` blocks in *case_id* point at each view id.

        Counted in Python over the case's ``view_ref`` blocks rather than with
        a JSON path operator in SQL: those differ between PostgreSQL and
        SQLite, and the test suite runs on SQLite. The row count here is
        bounded by the case's story blocks, which is small.
        """
        from sqlalchemy import select

        rows = await session.execute(
            select(StoryBlock.content)
            .join(Story, Story.id == StoryBlock.story_id)
            .where(Story.case_id == case_id, StoryBlock.kind == "view_ref")
        )
        counts: dict[str, int] = {}
        for (content,) in rows.all():
            view_id = (content or {}).get("view_id")
            if view_id:
                counts[view_id] = counts.get(view_id, 0) + 1
        return counts
```

Replace `delete_view`:

```python
    async def delete_view(self, case_id: str, view_id: str) -> str | None:
        """Delete a saved view, or hide it when a story block still needs it.

        Returns ``"deleted"`` when the row was removed, ``"hidden"`` when it
        was stamped ``deleted_at`` because a ``view_ref`` block references it,
        and None when there was no such view. Hiding rather than deleting is
        what keeps a story that embeds the view rendering and exporting;
        ``purge_orphaned_hidden_views`` finishes the job once the last
        reference is gone.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            result = await session.execute(
                select(View).where(View.case_id == case_id, View.id == view_id)
            )
            view = result.scalar_one_or_none()
            if view is None:
                return None
            counts = await self._view_ref_counts(session, case_id)
            if counts.get(view_id, 0) > 0:
                if view.deleted_at is None:
                    view.deleted_at = datetime.now(UTC)
                    await session.commit()
                return "hidden"
            await session.delete(view)
            await session.commit()
            return "deleted"
```

- [ ] **Step 8: Add the sweep**

Add after `delete_view`:

```python
    async def purge_orphaned_hidden_views(self, case_id: str) -> int:
        """Hard-delete hidden views nothing references any more; return the count.

        Idempotent and cheap, so every operation that can drop a ``view_ref``
        calls it unconditionally rather than working out whether this
        particular change orphaned anything. Sweeping the case instead of
        tracking one view is what makes it impossible to forget a code path.
        """
        from sqlalchemy import select

        async with self.session_factory() as session:
            counts = await self._view_ref_counts(session, case_id)
            hidden = (
                await session.execute(
                    select(View).where(
                        View.case_id == case_id, View.deleted_at.is_not(None)
                    )
                )
            ).scalars().all()
            removed = 0
            for view in hidden:
                if counts.get(view.id, 0) == 0:
                    await session.delete(view)
                    removed += 1
            if removed:
                await session.commit()
            return removed
```

- [ ] **Step 9: Call the sweep from every reference-dropping path**

Three store methods can drop a `view_ref`. Each already resolves the block or story it is working on, so each can resolve the case id. At the end of `delete_story` (after its `session.commit()`, before the `return`):

```python
        await self.purge_orphaned_hidden_views(case_id)
```

In `delete_story_block` and `update_story_block`, the case id is one hop away — resolve it from the block's story inside the existing session before committing, then sweep after the commit:

```python
        case_id = (
            await session.execute(
                select(Story.case_id).where(Story.id == block.story_id)
            )
        ).scalar_one_or_none()
```

```python
        if case_id:
            await self.purge_orphaned_hidden_views(case_id)
```

Read each method before editing — they differ in how they load the block and whether they return early on a version conflict. The sweep must run only on the success path: a 409 changed nothing.

- [ ] **Step 10: Stop a hidden view becoming a new block's target**

In `src/vestigo/stories/refs.py`, replace the `view_ref` check:

```python
    if kind == "view_ref":
        view = await store.get_view(case_id, content["view_id"])
        # A hidden view (deleted while referenced) keeps its existing block
        # working, but must not become the referent of a new one — that would
        # resurrect an artifact the analyst deleted.
        if view is None or view.deleted_at is not None:
            raise ValueError(f"view {content['view_id']!r} is not in this case")
```

- [ ] **Step 11: Report the outcome from the endpoint**

In `src/vestigo/api/routers/cases.py`, update `delete_view`:

```python
    """Delete a saved view, or hide it when a story block still references it.

    A ``view_ref`` block resolves its View at render and export time, so
    removing one out from under a story would make that story's export fail.
    Such a view is hidden instead and swept once the last block referencing it
    goes away; ``hidden`` in the response is what lets the UI say which of the
    two happened.
    """
    store = get_store()
    outcome = await store.delete_view(case.id, view_id)
    if outcome is None:
        raise HTTPException(status_code=404, detail="View not found")
    return {"deleted": True, "view_id": view_id, "hidden": outcome == "hidden"}
```

- [ ] **Step 12: Find every other `delete_view` caller**

Run:

```bash
rg -n 'delete_view' src tests
```

The return type changed from `bool` to `str | None`. Any caller doing `if await store.delete_view(...)` still works by truthiness, but a caller comparing to `True`/`False` does not. Fix each one found, including the case-delete cascade if it removes views directly.

- [ ] **Step 13: Confirm case transfer carries hidden views**

`transfer/exporter.py:65` exports `("views", View, "case")` and `_row_to_dict` serializes by column introspection, so `deleted_at` rides along and no `deleted_at` filter exists on the export query. Verify both by reading, then prove it:

```bash
rg -n 'deleted_at' src/vestigo/transfer/
uv run pytest tests/test_transfer_roundtrip_clickhouse.py -q
```

Expected: no `deleted_at` filter anywhere in `transfer/`, round-trip test green. If the exporter did filter, a case whose story embeds a hidden view would import into exactly the broken-export state this task exists to prevent.

- [ ] **Step 14: Run the tests and lint**

Run:

```bash
uv run pytest tests/test_view_lifecycle.py tests/test_postgres_store.py tests/test_stories_api.py tests/test_stories_store.py tests/test_stories_export.py -q
uv run ruff check . && uv run ruff format --check .
```

Expected: all green.

- [ ] **Step 15: Commit**

```bash
git add src/vestigo/db/postgres.py src/vestigo/db/migrations/versions/0025_view_deleted_at.py src/vestigo/stories/refs.py src/vestigo/api/routers/cases.py tests/test_view_lifecycle.py
git commit -m "feat(views): keep a deleted view alive while a story embeds it

A view_ref block resolves its View live at render and export time, so
deleting that view made the story's export fail with 'view not found'.
Deleting a referenced view now stamps a new nullable deleted_at instead:
it disappears from every list the analyst sees, the story keeps working,
and an idempotent per-case sweep hard-deletes it once the last
referencing block is gone.

get_view deliberately ignores deleted_at -- resolving a hidden view is
the point. refs.py does not, so a hidden view cannot become a new
block's referent.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 7: Search and delete in the saved-views list

**Files:**
- Modify: `frontend/src/api/views.ts`
- Modify: `frontend/src/api/types.ts` (`View`)
- Modify: `frontend/src/components/explorer/FilterRail.tsx:630-652` (views block)
- Test: `frontend/src/test/savedViewsList.test.tsx` (create)

**Interfaces:**
- Consumes: the `{"deleted": true, "view_id": ..., "hidden": bool}` response from Task 6, the `onApplyView` signature from Task 5.
- Produces: nothing later depends on.

- [ ] **Step 1: Write the failing list tests**

Create `frontend/src/test/savedViewsList.test.tsx`:

```tsx
/**
 * The saved-views list is manageable: substring search once it gets long
 * enough to need it, and per-view deletion that reports whether the view was
 * removed or merely hidden because a story still embeds it.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { render, screen, fireEvent, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { FilterRail } from "@/components/explorer/FilterRail";
import { TooltipProvider } from "@/components/ui/Tooltip";
import { viewsApi } from "@/api/views";
import type { View } from "@/api/types";

vi.mock("@/api/views", async (importOriginal) => {
  const actual = await importOriginal<typeof import("@/api/views")>();
  return {
    viewsApi: {
      ...actual.viewsApi,
      delete: vi.fn().mockResolvedValue({ deleted: true, view_id: "v1", hidden: false }),
    },
  };
});

function view(id: string, name: string): View {
  return {
    id,
    case_id: "c1",
    name,
    query: "",
    filter: {},
    created_at: "2026-07-01T00:00:00Z",
  };
}

function renderRail(views: View[]) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MemoryRouter>
      <QueryClientProvider client={qc}>
        <TooltipProvider>
          <FilterRail
            filters={{}}
            onChange={vi.fn()}
            views={views}
            onApplyView={vi.fn()}
            onSaveView={vi.fn()}
            onSearchSubmit={vi.fn()}
            caseId="c1"
            timelineId="t1"
          />
        </TooltipProvider>
      </QueryClientProvider>
    </MemoryRouter>,
  );
}

const many = [
  view("v1", "Failed logons"),
  view("v2", "PowerShell"),
  view("v3", "Outbound DNS"),
  view("v4", "Lateral movement"),
  view("v5", "Persistence"),
  view("v6", "Exfil candidates"),
];

beforeEach(() => vi.clearAllMocks());

describe("saved views list", () => {
  it("hides the search box for a short list", () => {
    renderRail(many.slice(0, 3));
    expect(screen.queryByPlaceholderText("Search views")).not.toBeInTheDocument();
  });

  it("shows the search box once the list gets long", () => {
    renderRail(many);
    expect(screen.getByPlaceholderText("Search views")).toBeInTheDocument();
  });

  it("filters by case-insensitive substring", () => {
    renderRail(many);
    fireEvent.change(screen.getByPlaceholderText("Search views"), {
      target: { value: "powershell" },
    });
    expect(screen.getByText("PowerShell")).toBeInTheDocument();
    expect(screen.queryByText("Failed logons")).not.toBeInTheDocument();
  });

  it("says so when nothing matches", () => {
    renderRail(many);
    fireEvent.change(screen.getByPlaceholderText("Search views"), {
      target: { value: "zzz" },
    });
    expect(screen.getByText("No views match")).toBeInTheDocument();
  });

  it("deletes a view after confirmation", async () => {
    renderRail(many.slice(0, 2));
    fireEvent.click(screen.getAllByRole("button", { name: /delete view/i })[0]);
    fireEvent.click(screen.getByRole("button", { name: /^delete$/i }));
    await waitFor(() => expect(viewsApi.delete).toHaveBeenCalledWith("c1", "v1"));
  });

  it("does not delete when the confirmation is dismissed", async () => {
    renderRail(many.slice(0, 2));
    fireEvent.click(screen.getAllByRole("button", { name: /delete view/i })[0]);
    fireEvent.click(screen.getByRole("button", { name: /cancel/i }));
    expect(viewsApi.delete).not.toHaveBeenCalled();
  });

  it("applies a view when its row is clicked, not when delete is", () => {
    renderRail(many.slice(0, 2));
    fireEvent.click(screen.getAllByRole("button", { name: /delete view/i })[0]);
    expect(screen.queryByText(/Failed logons.*applied/)).not.toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run:

```bash
cd frontend && npm run test -- savedViewsList
```

Expected: FAIL — no search box, no delete button.

- [ ] **Step 3: Type the delete response**

In `frontend/src/api/views.ts`:

```ts
  delete: (caseId: string, viewId: string) =>
    del<{ deleted: boolean; view_id: string; hidden: boolean }>(
      `/cases/${caseId}/views/${viewId}`,
    ),
```

In `frontend/src/api/types.ts`, add to `View`:

```ts
  /** Set when this view was deleted while a story block still referenced it.
   *  Such views never appear in the list; the field exists so a client that
   *  resolves one directly can tell. */
  deleted_at?: string | null;
```

- [ ] **Step 4: Add search and delete to the rail**

Replace the saved-views block in `FilterRail.tsx` (currently `:630-652`). Add the local state next to the component's other `useState` calls:

```tsx
  const [viewSearch, setViewSearch] = useState("");
  const [pendingDelete, setPendingDelete] = useState<View | null>(null);
```

Add the mutation next to the component's other hooks:

```tsx
  const qc = useQueryClient();
  const deleteView = useMutation({
    mutationFn: (v: View) => viewsApi.delete(caseId, v.id),
    onSuccess: (res) => {
      qc.invalidateQueries({ queryKey: ["views", caseId] });
      setPendingDelete(null);
      pushToast({
        variant: "success",
        title: res.hidden ? "Removed from the list" : "View deleted",
        description: res.hidden
          ? "A story still embeds this view, so it is kept until that block is gone."
          : undefined,
      });
    },
  });
```

Use whatever toast API `stores/toasts.ts` actually exposes — read it first and match its call shape rather than the sketch above.

The list itself:

```tsx
        {views.length > 0 && (
          <div>
            <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
              <BookmarkCheck size={13} /> Saved Views
            </label>
            {/* Search earns its space only once the list is long enough to
              * need it — five rows are faster to scan than to filter. */}
            {views.length > 5 && (
              <Input
                className="mb-1.5"
                placeholder="Search views"
                value={viewSearch}
                onChange={(e) => setViewSearch(e.target.value)}
              />
            )}
            <div className="space-y-1">
              {matchingViews.length === 0 ? (
                <p className="px-2.5 py-1.5 text-xs text-[var(--color-fg-muted)]">
                  No views match
                </p>
              ) : (
                matchingViews.map((v) => (
                  <div
                    key={v.id}
                    className="group flex items-center gap-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] hover:border-[var(--color-accent)] transition-base"
                  >
                    <button
                      className="min-w-0 flex-1 px-2.5 py-1.5 text-left text-xs text-[var(--color-fg-secondary)] group-hover:text-[var(--color-fg-primary)]"
                      onClick={() => {
                        const payload = v.filter as Record<string, unknown>;
                        onApplyView(
                          viewPayloadToFilters(payload),
                          viewPayloadColumns(payload),
                        );
                      }}
                    >
                      <div className="truncate font-medium">{v.name}</div>
                      <div className="text-[var(--color-fg-muted)]">
                        {fmtRelative(v.created_at)}
                      </div>
                    </button>
                    <button
                      aria-label={`Delete view ${v.name}`}
                      className="mr-1 shrink-0 rounded p-1 text-[var(--color-fg-muted)] opacity-0 group-hover:opacity-100 focus-visible:opacity-100 hover:text-[var(--color-danger)] transition-base"
                      onClick={() => setPendingDelete(v)}
                    >
                      <Trash2 size={13} />
                    </button>
                  </div>
                ))
              )}
            </div>
          </div>
        )}
```

with the filter memo beside the component's other `useMemo` calls:

```tsx
  const matchingViews = useMemo(() => {
    const needle = viewSearch.trim().toLowerCase();
    if (!needle) return views;
    return views.filter((v) => v.name.toLowerCase().includes(needle));
  }, [views, viewSearch]);
```

Import `Trash2` from `lucide-react`, `useMutation`/`useQueryClient` from `@tanstack/react-query`, `viewsApi` from `@/api/views`, and `viewPayloadColumns` alongside the existing `viewPayloadToFilters`.

- [ ] **Step 5: Add the confirmation**

Render a `Dialog` (the same primitive `SaveViewDialog` uses) at the end of the rail's JSX:

```tsx
      <Dialog open={!!pendingDelete} onOpenChange={(o) => { if (!o) setPendingDelete(null); }}>
        <DialogContent
          title="Delete view"
          description={
            pendingDelete
              ? `"${pendingDelete.name}" will be removed from this case. This cannot be undone.`
              : ""
          }
        >
          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm" onClick={() => setPendingDelete(null)}>
                Cancel
              </Button>
            </DialogClose>
            <Button
              variant="danger"
              size="sm"
              disabled={deleteView.isPending}
              onClick={() => pendingDelete && deleteView.mutate(pendingDelete)}
            >
              {deleteView.isPending ? "Deleting…" : "Delete"}
            </Button>
          </div>
        </DialogContent>
      </Dialog>
```

Check `components/ui/Button.tsx` for the destructive variant's real name — use whatever it exports rather than assuming `danger`.

- [ ] **Step 6: Run the tests, typecheck and lint**

Run:

```bash
cd frontend && npm run test && npm run typecheck && npm run lint
```

Expected: all green, including the pre-existing `filterRail` tests.

- [ ] **Step 7: Verify both delete outcomes by hand**

With the stack running: save two views. Delete the first — it should vanish with a "View deleted" toast. Add the second to a story as a view block, then delete it — it should vanish from the list with the "still embeds this view" toast, and the story must still render it. Delete that story block, reload, and confirm the view is now really gone (`GET /api/cases/<id>/views` and the story both agree).

- [ ] **Step 8: Commit**

```bash
git add frontend/src/api/views.ts frontend/src/api/types.ts frontend/src/components/explorer/FilterRail.tsx frontend/src/test/savedViewsList.test.tsx
git commit -m "feat(views): search and delete in the saved-views list

The list was an unfiltered stack of buttons with no way to remove one,
even though the DELETE endpoint and its client method already existed.
It now carries a case-insensitive substring search (shown once the list
passes five entries) and a per-row delete behind a confirmation.

The toast distinguishes the two backend outcomes: a plain delete, or the
view being kept because a story block still embeds it.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

### Task 8: Documentation

**Files:**
- Modify: `docs/STORIES.md`, `docs/ROADMAP.md`, `docs/PROGRESS.md`

**Interfaces:**
- Consumes: everything from Tasks 1-7.
- Produces: nothing.

- [ ] **Step 1: Record the new view_ref invariant**

Read `docs/STORIES.md`, find the section describing `view_ref` blocks and their referents, and add the lifetime rule in the voice that section already uses. The substance to convey:

- A `view_ref` block resolves its View live at render and export time.
- Deleting a referenced View therefore does not remove it: it is hidden (`views.deleted_at`), stays out of every list, and keeps the block working.
- It is hard-deleted by `PostgresStore.purge_orphaned_hidden_views` once the last referencing block is deleted or repointed, or the story is deleted.
- A hidden View cannot become the referent of a *new* block — `stories/refs.py` rejects it.
- So a View's lifetime is "until it is both deleted and unreferenced", not "until someone deletes it".

- [ ] **Step 2: Note the new filter mode where the filters are documented**

Run:

```bash
rg -n 'wildcard' docs/*.md
```

Wherever the field-filter match modes are enumerated for users, add `empty` with its semantics: include means the field has no value (absent or blank), exclude means it has one, whitespace counts as a value. If no doc enumerates them, skip this step rather than inventing a section.

- [ ] **Step 3: Clear the roadmap items**

If `docs/ROADMAP.md` carries entries for any of these five changes, delete them — the convention is deletion on landing, not checking a box.

- [ ] **Step 4: Add the progress entry**

Add a new entry at the **top** of `docs/PROGRESS.md`, matching the format of the entry currently there. Cover: the `empty` match mode and why it rides the existing mode maps; the grid header moving inside the scroller and the `scrollMargin` consequence; column drag-reorder writing the existing per-timeline override; saved views carrying the column layout; and reference-safe view deletion with the `deleted_at` column. Keep it to what changed and why — no TODOs, those belong in `ROADMAP.md`.

- [ ] **Step 5: Run the full suites one last time**

Run:

```bash
uv run pytest -q
cd frontend && npm run test && npm run typecheck && npm run lint
```

Expected: everything green. Report the actual counts — a skipped ClickHouse module is not a pass; bring `podman compose up -d` up if any skipped.

- [ ] **Step 6: Commit**

```bash
git add docs/
git commit -m "docs: record the empty match mode and the view lifetime rule

STORIES.md gains the view_ref invariant: a View now lives until it is
both deleted and unreferenced, because deleting one a story embeds hides
it rather than removing it. PROGRESS.md gets the session entry.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>"
```

---

## Self-Review Notes

Spec coverage checked section by section:

- Spec §1 (reorder) → Task 4. Resize-handle collision, 8px activation, pinned internal columns, keyboard sensor, `columnWidths` keyed by id: all present.
- Spec §2 (`empty` mode) → Tasks 1-2. Both allowlists, `ifNull`, values ignored, `[""]` wire shape, whitespace-is-a-value, `_validate_field_regexes` confirmed rather than edited, chip copy.
- Spec §3 (scroll) → Task 3. Shared-width wrapper, sticky header, `scrollMargin`, `handleScroll` offset, prepend anchor verified-not-changed, row width fix.
- Spec §4 (views carry columns) → Task 5. Key added at the call site not in the shared helper, `viewPayloadColumns` sanitize-to-undefined, legacy payloads untouched, cross-timeline accepted.
- Spec §5 (delete + search) → Tasks 6-7. `deleted_at`, filtered `list_views` with unfiltered `get_view`, Python-side JSON counting, idempotent sweep on three paths, `refs.py` liveness, `hidden` in the response, transfer verified, search above five views.
- Spec "Testing" → the test steps in Tasks 1-7; spec "Documentation" → Task 8.

Type consistency: `delete_view` returns `str | None` (Task 6) and is consumed only by the router in the same task and by the frontend as `{hidden: boolean}` (Task 7). `viewPayloadColumns` has one signature, used in Tasks 5 and 7. `onApplyView` is widened once in Task 5 and used in that shape in Task 7. `reorderColumns` and `onReorderColumns` appear only in Task 4.
