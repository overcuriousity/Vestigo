/**
 * Which columns an event grid actually shows, and where that answer comes from.
 *
 * Three sources, in strict precedence order:
 *
 * 1. **The analyst's own choice** for this timeline, in the `vestigo-ui`
 *    zustand store (per browser). Always wins — a suggestion recomputed
 *    because a colleague uploaded a source must never move someone's columns
 *    out from under them mid-investigation.
 * 2. **The timeline's suggestion** (`Timeline.recommended_columns`, issue
 *    #213), derived from the timeline's own field statistics and shared by
 *    everyone with access.
 * 3. **`DEFAULT_COLUMNS`** — timestamp/artifact/message, the original
 *    behaviour, used whenever there is no suggestion to apply.
 *
 * Kept in one place because `ExplorerPage` reads it to render the grid and
 * `ColumnPicker` reads it to render the checkboxes; the two disagreeing would
 * show ticks next to columns that are not on screen.
 */
import type { RecommendedColumns } from "@/api/types";
import { DEFAULT_COLUMNS, sanitizeColumns } from "@/stores/ui";

/** Whether a recommendation has columns ready to apply. */
export function hasSuggestion(
  recommended: RecommendedColumns | null | undefined,
): recommended is RecommendedColumns {
  return recommended?.status === "ok" && recommended.columns.length > 0;
}

/** Whether a recommendation job is currently in flight for this timeline. */
export function isSuggesting(
  recommended: RecommendedColumns | null | undefined,
): boolean {
  return recommended?.status === "running";
}

/**
 * The suggested columns, run through the same sanitizer the stored selections
 * go through — a suggestion is server data and gets no more trust than a
 * persisted one. Null when nothing survives sanitization, so the caller falls
 * back rather than rendering an empty grid.
 */
export function suggestedColumns(
  recommended: RecommendedColumns | null | undefined,
): string[] | null {
  if (!hasSuggestion(recommended)) return null;
  const sanitized = sanitizeColumns(recommended.columns);
  return sanitized.length > 0 ? sanitized : null;
}

/** Resolve the columns to render, applying the precedence above. */
export function resolveVisibleColumns(
  stored: string[] | undefined,
  recommended: RecommendedColumns | null | undefined,
): string[] {
  if (stored) return stored;
  return suggestedColumns(recommended) ?? DEFAULT_COLUMNS;
}
