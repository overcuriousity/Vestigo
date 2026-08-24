import { FilterChips } from "@/components/explorer/FilterChips";
import { hasActiveFilters } from "@/lib/fieldFilters";
import type { EventFilters } from "@/api/types";

/**
 * Persistent transparency for issue #205: the agent inherits the operator's
 * live Explorer filters as per-message context. Read-only — editing filters
 * stays in the Explorer; the bar just makes the inheritance visible and
 * states when it takes effect.
 */
export function AgentFiltersBar({ filters }: { filters: EventFilters }) {
  return (
    <div
      data-testid="agent-inherited-filters"
      className="shrink-0 border-b border-[var(--color-border)] px-2.5 py-1 text-[11px] text-[var(--color-fg-secondary)]"
    >
      <div className="mb-0.5 font-medium">
        Agent sees your current Explorer view — changes apply to the next message.
      </div>
      {hasActiveFilters(filters) ? (
        <FilterChips filters={filters} />
      ) : (
        <span>No filters — the agent sees the whole timeline.</span>
      )}
    </div>
  );
}
