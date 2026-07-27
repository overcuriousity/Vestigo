/**
 * The Visualize canvas's statement of scope.
 *
 * Charts here aggregate whatever the Explorer grid was showing, because both
 * read their filters from the same URL params. That inheritance used to be
 * almost invisible: an unfiltered chart and a chart of one narrow slice looked
 * identical, and the only full account of the active filters was the caption
 * *below* the chart. For a figure that gets exported into a report, the scope
 * has to be legible before the chart is read, not after.
 *
 * Lives under `viz/` rather than `ui/` because it knows `EventFilters` — the
 * same reason `FilterChips`, which it reuses, lives under `explorer/`.
 */
import { Link } from "react-router-dom";
import { Filter, RotateCcw } from "lucide-react";
import type { EventFilters } from "@/api/types";
import { FilterChips } from "@/components/explorer/FilterChips";
import { InfoHint } from "@/components/ui/InfoHint";
import { hasActiveFilters } from "@/lib/fieldFilters";

interface Props {
  /** The URL-derived filters. Pass the raw set, not one augmented with
   * `collapseRoutine`: that is not URL-serialized and has its own row, so a
   * chip would misrepresent it as part of the shareable filter state. */
  filters: EventFilters;
  /** Deep link back to the Explorer with these same filters. */
  explorerHref: string;
  onRemove: (key: keyof EventFilters | string, fieldKey?: string, value?: string) => void;
  onClearAll: () => void;
  /** Clears both time bounds at once — the one thing per-chip removal of
   * `from`/`to` doesn't do in a single click after a brush-zoom. */
  onResetRange: () => void;
}

const ACTION_CLASS =
  "flex items-center gap-1 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] transition-base";

export function InheritedFiltersBar({
  filters,
  explorerHref,
  onRemove,
  onClearAll,
  onResetRange,
}: Props) {
  const hasRange = !!(filters.start || filters.end);
  // Shares `FilterChips`'s own definition of "filtered" rather than comparing
  // against caption prose: this decides between the chips and the "no filters"
  // empty state, so disagreeing with what the chips render would leave an
  // empty chip row or hide real ones.
  const hasFilters = hasActiveFilters(filters);

  return (
    <div
      data-testid="inherited-filters"
      className="mb-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-2.5 py-2 text-xs"
    >
      <div className="flex flex-wrap items-center gap-2">
        <span className="flex items-center gap-1.5 font-medium text-[var(--color-accent)]">
          <Filter size={11} />
          Inherited from Explorer
        </span>
        <InfoHint content="Charts aggregate exactly the events the Explorer grid is showing. The filters live in this page's URL, so sharing the link reproduces the same chart for a teammate." />
        <span className="flex-1" />
        {hasRange && (
          <button type="button" onClick={onResetRange} className={ACTION_CLASS}>
            <RotateCcw size={11} /> Reset range
          </button>
        )}
        {hasFilters && (
          <button type="button" onClick={onClearAll} className={ACTION_CLASS}>
            Clear all
          </button>
        )}
        <Link to={explorerHref} className={ACTION_CLASS}>
          Edit in Explorer
        </Link>
      </div>
      <div className="mt-1.5">
        {hasFilters ? (
          <FilterChips filters={filters} onRemove={onRemove} />
        ) : (
          <span className="text-[var(--color-fg-muted)]">
            No filters — charting the whole timeline.
          </span>
        )}
      </div>
    </div>
  );
}
