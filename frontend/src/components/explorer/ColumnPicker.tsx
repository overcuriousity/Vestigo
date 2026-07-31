/**
 * ColumnPicker — toolbar popover for configuring event grid columns.
 *
 * Fetches the timeline's field list from /fields (top-level + dynamic
 * attributes) and renders a searchable checkbox list.  Selection is persisted
 * to the UI store (localStorage) via setVisibleColumns.
 *
 * Columns the backend suggested for this timeline (issue #213) are marked, and
 * carry the evidence behind the suggestion in a tooltip — a default an analyst
 * cannot interrogate is a default they have to take on faith.  The footer is
 * where the suggestion is adopted, rejected, or recomputed.
 */
import { useState, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight, Columns3, RotateCcw, Search, Sparkles } from "lucide-react";
import { eventsApi } from "@/api/events";
import { timelinesApi } from "@/api/timelines";
import { useUiStore, DEFAULT_COLUMNS } from "@/stores/ui";
import { useJobsStore } from "@/stores/jobs";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import {
  Popover,
  PopoverTrigger,
  PopoverContent,
} from "@/components/ui/Popover";
import { cn } from "@/lib/cn";
import {
  hasSuggestion,
  isSuggesting,
  resolveVisibleColumns,
  suggestedColumns,
} from "@/lib/columns";
import { splitDerivedKey } from "@/lib/enrichment";
import type { RecommendedColumns } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  /** The timeline's stored suggestion, or null/undefined when it has none. */
  recommended?: RecommendedColumns | null;
  /** Whether this analyst may recompute the shared suggestion. */
  canRecommend?: boolean;
}

/** Human-readable labels for the built-in top-level columns. */
const TOP_LEVEL_LABELS: Record<string, string> = {
  timestamp: "Timestamp",
  source_id: "Source",
  artifact: "Artifact",
  artifact_long: "Artifact (long)",
  display_name: "Display Name",
  message: "Message",
  timestamp_desc: "Timestamp Desc",
};

function ColumnRow({
  id,
  label,
  checked,
  onChange,
  suggestedReason,
}: {
  id: string;
  label: string;
  checked: boolean;
  onChange: (id: string, checked: boolean) => void;
  /** Present when this column is part of the timeline's suggestion. */
  suggestedReason?: string;
}) {
  return (
    <label
      className={cn(
        "flex items-center gap-2.5 rounded px-2 py-1.5 cursor-pointer select-none",
        "text-xs text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] transition-base",
      )}
    >
      <input
        type="checkbox"
        checked={checked}
        onChange={(e) => onChange(id, e.target.checked)}
        className="h-3.5 w-3.5 cursor-pointer rounded border-[var(--color-border-strong)] accent-[var(--color-accent)]"
      />
      <span className={cn("flex-1 truncate", checked && "text-[var(--color-fg-primary)]")}>
        {label}
      </span>
      {suggestedReason !== undefined && (
        <span
          title={suggestedReason ? `Suggested — ${suggestedReason}` : "Suggested"}
          aria-label={
            suggestedReason ? `Suggested column: ${suggestedReason}` : "Suggested column"
          }
          className="shrink-0 text-[var(--color-accent)]"
        >
          <Sparkles size={11} />
        </span>
      )}
    </label>
  );
}

function DerivedGroup({
  childKeys,
  forceExpand,
  visibleSet,
  onToggle,
}: {
  childKeys: string[];
  forceExpand: boolean;
  visibleSet: Set<string>;
  onToggle: (id: string, checked: boolean) => void;
}) {
  const [open, setOpen] = useState(false);
  const expanded = open || forceExpand;
  return (
    <div>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className={cn(
          "flex w-full items-center gap-1 rounded px-2 py-1 pl-6 text-left text-xs",
          "text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-secondary)] transition-base",
        )}
        aria-expanded={expanded}
      >
        {expanded ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
        Derived ({childKeys.length})
      </button>
      {expanded &&
        childKeys.map((key) => (
          <div key={key} className="pl-6">
            <ColumnRow
              id={key}
              label={splitDerivedKey(key)?.field ?? key}
              checked={visibleSet.has(key)}
              onChange={onToggle}
            />
          </div>
        ))}
    </div>
  );
}

export function ColumnPicker({ caseId, timelineId, recommended, canRecommend }: Props) {
  const [search, setSearch] = useState("");
  const tlKey = `${caseId}/${timelineId}`;
  const storedColumns = useUiStore((s) => s.visibleColumnsByTimeline[tlKey]);
  const setVisibleColumnsStore = useUiStore((s) => s.setVisibleColumns);
  const setVisibleColumns = (cols: string[]) => setVisibleColumnsStore(tlKey, cols);
  const suggestion = suggestedColumns(recommended);
  // Through the shared resolver, not a local copy of the same three-way
  // precedence — the ticks here must match what the grid is rendering.
  const visibleColumns = resolveVisibleColumns(storedColumns, recommended);
  const suggestedReasons = hasSuggestion(recommended) ? recommended.reasons : {};
  const suggestedSet = new Set(suggestion ?? []);

  const queryClient = useQueryClient();
  const addJob = useJobsStore((s) => s.addJob);
  const recommendMutation = useMutation({
    mutationFn: () => timelinesApi.recommendColumns(caseId, timelineId),
    onSuccess: (result) => {
      // A null job id means one was already running (or suggestions are off
      // instance-wide) — refetching the timeline is still the right move, since
      // that in-flight job is what the status line is waiting on.
      if (result.job_id) {
        addJob(result.job_id, "Suggesting columns", [
          ["timeline", caseId, timelineId],
          ["fields", caseId, timelineId],
        ]);
      }
      queryClient.invalidateQueries({ queryKey: ["timeline", caseId, timelineId] });
    },
  });
  const recommendRunning = recommendMutation.isPending || isSuggesting(recommended);

  const { data: fields, isLoading } = useQuery({
    queryKey: ["fields", caseId, timelineId],
    queryFn: () => eventsApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });

  const standardAll = useMemo(
    () =>
      (fields?.top_level ?? DEFAULT_COLUMNS).map((id) => ({
        id,
        label: TOP_LEVEL_LABELS[id] ?? id,
      })),
    [fields],
  );

  // Partition dynamic attributes: enrichment-derived keys
  // ("src_ip:geo_country") collapse under their parent attribute so a
  // wide/vendor-inconsistent dataset with many enriched IP columns doesn't
  // balloon the flat list (PR #54 finding #34). Derived keys whose parent
  // isn't itself in the field list fall back to a trailing group.
  const { baseAttrs, derivedByParent, orphanDerived } = useMemo(() => {
    const attrs = fields?.attributes ?? [];
    const attrSet = new Set(attrs);
    const knownSuffixes = new Set(fields?.derived_suffixes ?? []);
    const bases: string[] = [];
    const byParent = new Map<string, string[]>();
    const orphans: string[] = [];
    for (const key of attrs) {
      const parts = splitDerivedKey(key, knownSuffixes);
      if (parts && attrSet.has(parts.parent)) {
        const children = byParent.get(parts.parent) ?? [];
        children.push(key);
        byParent.set(parts.parent, children);
      } else if (parts) {
        orphans.push(key);
      } else {
        bases.push(key);
      }
    }
    return { baseAttrs: bases, derivedByParent: byParent, orphanDerived: orphans };
  }, [fields]);

  const query = search.toLowerCase();
  const matches = (id: string, label?: string) =>
    !query || id.toLowerCase().includes(query) || (label ?? "").toLowerCase().includes(query);

  const standard = standardAll.filter((c) => matches(c.id, c.label));
  // A base attribute stays visible when it matches OR any of its derived
  // children match — a search must never hide a selectable field.
  const dynamicVisible = baseAttrs
    .map((id) => {
      const children = (derivedByParent.get(id) ?? []).filter((k) => matches(k));
      return { id, selfMatch: matches(id), children };
    })
    .filter((entry) => entry.selfMatch || entry.children.length > 0);
  const orphansVisible = orphanDerived.filter((k) => matches(k));

  const visibleSet = new Set(visibleColumns);

  const toggle = (id: string, checked: boolean) => {
    if (checked) {
      // Append at end, preserving existing order
      if (!visibleSet.has(id)) {
        setVisibleColumns([...visibleColumns, id]);
      }
    } else {
      setVisibleColumns(visibleColumns.filter((c) => c !== id));
    }
  };

  const nothingVisible =
    standard.length === 0 && dynamicVisible.length === 0 && orphansVisible.length === 0;

  const activeCount = visibleColumns.filter((c) => c !== "_select" && c !== "_expand").length;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" data-tour="column-picker">
          <Columns3 size={13} />
          Columns
          {activeCount > 0 && (
            <span className="ml-1 rounded bg-[var(--color-accent-dim)] px-1 text-xs font-semibold text-[var(--color-accent)]">
              {activeCount}
            </span>
          )}
        </Button>
      </PopoverTrigger>

      <PopoverContent className="w-64 p-0" align="end" data-tour="column-picker-content">
        {/* Search */}
        <div className="border-b border-[var(--color-border)] p-2">
          <div className="relative">
            <Search
              size={12}
              className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--color-fg-muted)]"
            />
            <Input
              placeholder="Search fields…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              className="pl-6"
            />
          </div>
        </div>

        {/* Column list */}
        <div className="max-h-72 overflow-y-auto px-1 py-1">
          {isLoading ? (
            <div className="flex items-center justify-center py-4">
              <Spinner size={16} />
            </div>
          ) : (
            <>
              {standard.length > 0 && (
                <div>
                  <p className="px-2 pb-1 pt-1.5 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)]">
                    Standard
                  </p>
                  {standard.map((c) => (
                    <ColumnRow
                      key={c.id}
                      id={c.id}
                      label={c.label}
                      checked={visibleSet.has(c.id)}
                      onChange={toggle}
                      suggestedReason={
                        suggestedSet.has(c.id) ? (suggestedReasons[c.id] ?? "") : undefined
                      }
                    />
                  ))}
                </div>
              )}

              {dynamicVisible.length > 0 && (
                <div className={standard.length > 0 ? "mt-1 border-t border-[var(--color-border-subtle)] pt-1" : ""}>
                  <p className="px-2 pb-1 pt-1.5 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)]">
                    Dynamic fields
                  </p>
                  {dynamicVisible.map(({ id, children }) => (
                    <div key={id}>
                      <ColumnRow
                        id={id}
                        label={id}
                        checked={visibleSet.has(id)}
                        onChange={toggle}
                        suggestedReason={
                          suggestedSet.has(id) ? (suggestedReasons[id] ?? "") : undefined
                        }
                      />
                      {children.length > 0 && (
                        <DerivedGroup
                          childKeys={children}
                          // An active search that matched a child must show
                          // it — never hide a selectable field behind a
                          // collapsed disclosure.
                          forceExpand={search.length > 0}
                          visibleSet={visibleSet}
                          onToggle={toggle}
                        />
                      )}
                    </div>
                  ))}
                </div>
              )}

              {orphansVisible.length > 0 && (
                <div className="mt-1 border-t border-[var(--color-border-subtle)] pt-1">
                  <p className="px-2 pb-1 pt-1.5 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)]">
                    Derived fields
                  </p>
                  {orphansVisible.map((key) => (
                    <ColumnRow
                      key={key}
                      id={key}
                      label={key}
                      checked={visibleSet.has(key)}
                      onChange={toggle}
                    />
                  ))}
                </div>
              )}

              {nothingVisible && (
                <p className="px-2 py-3 text-xs text-[var(--color-fg-muted)]">
                  No fields match &ldquo;{search}&rdquo;
                </p>
              )}
            </>
          )}
        </div>

        {/* Reset / suggestion footer */}
        <div className="flex flex-wrap items-center gap-1 border-t border-[var(--color-border)] p-2">
          <button
            className="flex items-center gap-1.5 text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-base"
            onClick={() => setVisibleColumns(DEFAULT_COLUMNS)}
          >
            <RotateCcw size={10} /> Reset to defaults
          </button>
          {suggestion && (
            <Button
              variant="ghost"
              size="sm"
              // Clearing the local override is what makes the suggestion take
              // effect, rather than copying it in — so a later recomputation
              // still reaches this analyst.
              onClick={() => setVisibleColumnsStore(tlKey, undefined)}
              disabled={!storedColumns}
            >
              <Sparkles size={11} /> Use suggested
            </Button>
          )}
          {canRecommend && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => recommendMutation.mutate()}
              disabled={recommendRunning}
            >
              {recommendRunning ? <Spinner size={11} /> : <RotateCcw size={11} />}
              {recommendRunning ? "Suggesting…" : "Re-suggest columns"}
            </Button>
          )}
        </div>
      </PopoverContent>
    </Popover>
  );
}
