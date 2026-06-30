/**
 * ExplorerPage — the heart of TraceVector.
 *
 * Panels:
 *   Left:   FilterRail (collapsible via toolbar toggle)
 *   Center: EventGrid (always visible)
 *   Right:  EventDetailPanel + AnalysisPanel (independently closeable)
 *
 * All filter state lives in the URL so investigation links are shareable.
 * Filter-in / Filter-out from the detail panel adds directly to the URL.
 */
import { useState, useCallback, useMemo, useEffect } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useQuery, useInfiniteQuery } from "@tanstack/react-query";
import {
  FlaskConical,
  RefreshCw,
  PanelLeftClose,
  PanelLeftOpen,
  BarChart2,
} from "lucide-react";

import { eventsApi } from "@/api/events";
import { annotationsApi } from "@/api/annotations";
import { viewsApi } from "@/api/views";
import { timelinesApi } from "@/api/timelines";
import { useUiStore, DEFAULT_COLUMNS } from "@/stores/ui";
import { paramsToFilters, filtersToParams } from "@/lib/queryParams";

import { FilterRail } from "@/components/explorer/FilterRail";
import { FilterChips } from "@/components/explorer/FilterChips";
import { EventGrid } from "@/components/explorer/EventGrid";
import { EventDetailPanel } from "@/components/explorer/EventDetailPanel";
import { BulkActionBar } from "@/components/explorer/BulkActionBar";
import { ExportDialog } from "@/components/explorer/ExportDialog";
import { SaveViewDialog } from "@/components/explorer/SaveViewDialog";
import { ColumnPicker } from "@/components/explorer/ColumnPicker";
import { TimelineHistogram } from "@/components/explorer/TimelineHistogram";
import { AnalysisPanel } from "@/components/analysis/AnalysisPanel";
import { TriageMeter } from "@/components/triage/TriageMeter";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";

import type { Event, EventFilters, Annotation } from "@/api/types";

const PAGE_SIZE = 100;

/** Discriminated selection state.
 *  "ids"  — explicit per-row selection (IDs of in-memory events).
 *  "all"  — all events matching the current filter (resolved server-side).
 */
type SelectionState =
  | { mode: "ids"; ids: Set<string> }
  | { mode: "all" };

export function ExplorerPage() {
  const { caseId, timelineId } = useParams<{
    caseId: string;
    timelineId: string;
  }>();
  const [searchParams, setSearchParams] = useSearchParams();

  // ── Filter state (URL-driven) ──────────────────────────────────────────
  const filters = useMemo(() => paramsToFilters(searchParams), [searchParams]);

  const setFilters = useCallback(
    (f: EventFilters) => {
      setSearchParams(filtersToParams(f));
    },
    [setSearchParams],
  );

  const removeFilter = useCallback(
    (key: keyof EventFilters | string, fieldKey?: string, value?: string) => {
      const f = { ...filters };
      if (key === "filters" && fieldKey) {
        const { [fieldKey]: _removed, ...rest } = f.filters ?? {};
        f.filters = rest;
      } else if (key === "exclusions" && fieldKey) {
        if (value !== undefined) {
          const remaining = (f.exclusions?.[fieldKey] ?? []).filter((v) => v !== value);
          if (remaining.length === 0) {
            const { [fieldKey]: _removed, ...rest } = f.exclusions ?? {};
            f.exclusions = rest;
          } else {
            f.exclusions = { ...(f.exclusions ?? {}) as Record<string, string[]>, [fieldKey]: remaining };
          }
        } else {
          const { [fieldKey]: _removed, ...rest } = f.exclusions ?? {};
          f.exclusions = rest;
        }
      } else {
        delete f[key as keyof EventFilters];
      }
      setFilters(f);
    },
    [filters, setFilters],
  );

  /**
   * Handler wired to the detail panel's filter-in/filter-out buttons.
   *
   * Special cases:
   *   - filterKey "q"       → sets the top-level full-text search param
   *   - filterKey "artifact" → sets the dedicated artifact param (include only)
   *   - filterKey "tag"     → sets the dedicated tag param (include only)
   *   - everything else     → goes into filters{} or exclusions{}
   */
  const handleAddFilter = useCallback(
    (fieldKey: string, value: string, include: boolean) => {
      const f = { ...filters };

      if (fieldKey === "q") {
        // Full-text search: always "include" (no exclusion concept for free text)
        f.q = value;
      } else if (fieldKey === "artifact") {
        if (include) {
          f.artifact = value;
        } else {
          const prev = f.exclusions?.artifact ?? [];
          if (!prev.includes(value)) {
            f.exclusions = { ...(f.exclusions ?? {}) as Record<string, string[]>, artifact: [...prev, value] };
          }
        }
      } else if (fieldKey === "tag") {
        if (include) {
          f.tag = value;
        } else {
          f.excludeTag = value;
        }
      } else if (include) {
        f.filters = { ...(f.filters ?? {}), [fieldKey]: value };
      } else {
        const prev = f.exclusions?.[fieldKey] ?? [];
        if (!prev.includes(value)) {
          f.exclusions = { ...(f.exclusions ?? {}) as Record<string, string[]>, [fieldKey]: [...prev, value] };
        }
      }

      setFilters(f);
    },
    [filters, setFilters],
  );

  // ── Panel visibility state ────────────────────────────────────────────
  const filterRailOpen = useUiStore((s) => s.filterRailOpen);
  const setFilterRailOpen = useUiStore((s) => s.setFilterRailOpen);
  const analysisPanelOpen = useUiStore((s) => s.analysisPanelOpen);
  const setAnalysisPanelOpen = useUiStore((s) => s.setAnalysisPanelOpen);
  const [expandedEvent, setExpandedEvent] = useState<Event | null>(null);
  const [selection, setSelection] = useState<SelectionState>({ mode: "ids", ids: new Set() });
  const [similarAnchor, setSimilarAnchor] = useState<Event | null>(null);
  const [saveViewOpen, setSaveViewOpen] = useState(false);
  const tlKey = `${caseId}/${timelineId}`;
  const visibleColumns = useUiStore((s) => s.visibleColumnsByTimeline[tlKey] ?? DEFAULT_COLUMNS);
  const histogramOpen = useUiStore((s) => s.histogramOpen);
  const setHistogramOpen = useUiStore((s) => s.setHistogramOpen);
  const sortDir = useUiStore((s) => s.sortDir);
  const setSortDir = useUiStore((s) => s.setSortDir);

  // Clear selection when filters or sort direction change so stale IDs are
  // never bulk-annotated against a different result set.
  useEffect(() => {
    setSelection({ mode: "ids", ids: new Set() });
  }, [filters, sortDir]);

  // ── Data queries ───────────────────────────────────────────────────────
  const { data: timeline } = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  const { data: timelineSources } = useQuery({
    queryKey: ["timeline-sources", caseId, timelineId],
    queryFn: () => timelinesApi.listSources(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  const {
    data: eventsData,
    isLoading: eventsLoading,
    isFetching,
    isError: eventsError,
    refetch,
    fetchNextPage,
    hasNextPage,
  } = useInfiniteQuery({
    queryKey: ["events", caseId, timelineId, filters, sortDir],
    queryFn: ({ pageParam, signal }) =>
      eventsApi.list(
        caseId!,
        timelineId!,
        { ...filters, limit: PAGE_SIZE, offset: pageParam, order: sortDir },
        signal,
      ),
    initialPageParam: 0,
    getNextPageParam: (lastPage, allPages) => {
      const loaded = allPages.reduce((sum, p) => sum + p.events.length, 0);
      return loaded < lastPage.total ? loaded : undefined;
    },
    enabled: !!(caseId && timelineId),
    placeholderData: (prev) => prev,
  });

  const { data: annotations } = useQuery({
    queryKey: ["annotations", caseId, timelineId],
    queryFn: () => annotationsApi.listForTimeline(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
    refetchInterval: 30_000,
  });

  const { data: views } = useQuery({
    queryKey: ["views", caseId],
    queryFn: () => viewsApi.list(caseId!),
    enabled: !!caseId,
  });

  const { data: tagSuggestions = [] } = useQuery({
    queryKey: ["tags", caseId, timelineId],
    queryFn: () => annotationsApi.listDistinctTags(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  // ── Derived ────────────────────────────────────────────────────────────
  const annotationMap = useMemo<Map<string, Annotation[]>>(() => {
    const m = new Map<string, Annotation[]>();
    for (const a of annotations ?? []) {
      const list = m.get(a.event_id) ?? [];
      list.push(a);
      m.set(a.event_id, list);
    }
    return m;
  }, [annotations]);

  const events = useMemo(() => eventsData?.pages.flatMap((p) => p.events) ?? [], [eventsData]);
  const total = eventsData?.pages[0]?.total ?? 0;
  const hasVectors =
    (timelineSources?.some((s) => s.vector_count > 0) ?? false);

  // Derive a plain Set<string> of selected IDs for components that don't know
  // about the "all" mode (EventGrid checkboxes). In "all" mode we show all
  // loaded rows as selected.
  const selectedIds = useMemo<Set<string>>(() => {
    if (selection.mode === "all") return new Set(events.map((e) => e.event_id));
    return selection.ids;
  }, [selection, events]);

  // Total count shown in BulkActionBar label
  const selectionCount = selection.mode === "all" ? total : selection.ids.size;

  // Show the "select all N matching" banner when all loaded rows are in "ids"
  // mode selection and there are more events not yet loaded.
  const showSelectAllBanner =
    selection.mode === "ids" &&
    selection.ids.size === events.length &&
    events.length > 0 &&
    total > events.length;

  // ── Handlers ───────────────────────────────────────────────────────────
  const handleToggleSelect = useCallback((id: string) => {
    setSelection((prev) => {
      // Clicking a row while in "all" mode collapses back to "ids"
      const ids = prev.mode === "ids" ? new Set(prev.ids) : new Set<string>();
      if (ids.has(id)) ids.delete(id);
      else ids.add(id);
      return { mode: "ids", ids };
    });
  }, []);

  const handleToggleSelectAll = useCallback(() => {
    setSelection((prev) => {
      if (prev.mode === "ids" && prev.ids.size === events.length && events.length > 0) {
        // All loaded are selected → deselect all
        return { mode: "ids", ids: new Set() };
      }
      // Select all currently loaded events
      return { mode: "ids", ids: new Set(events.map((e) => e.event_id)) };
    });
  }, [events]);

  const handleLoadMore = useCallback(() => {
    if (!isFetching && hasNextPage) fetchNextPage();
  }, [isFetching, hasNextPage, fetchNextPage]);

const handleFindSimilar = useCallback((event: Event) => {
    setSimilarAnchor(event);
    setAnalysisPanelOpen(true);
  }, []);

  const handleHistogramRange = useCallback(
    (start: string, end: string) => {
      setFilters({ ...filters, start, end });
    },
    [filters, setFilters],
  );

  const hasActiveFilters = Object.values(filters).some((v) =>
    v && (typeof v === "string" ? v.length > 0 : Object.keys(v).length > 0),
  );

  return (
    <div className="flex h-full overflow-hidden">
      {/* ── Left: Filter rail (collapsible) ─────────────────────────── */}
      {filterRailOpen && (
        <FilterRail
          filters={filters}
          onChange={setFilters}
          views={views ?? []}
          onApplyView={setFilters}
          onSaveView={() => setSaveViewOpen(true)}
          onClose={() => setFilterRailOpen(false)}
        />
      )}

      {/* ── Center + Right panels ───────────────────────────────────── */}
      <div className="flex flex-1 min-w-0 flex-col overflow-hidden">
        {/* Toolbar */}
        <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] bg-[var(--color-bg-surface)] px-3 py-1.5">
          {/* Filter rail toggle */}
          <Tooltip content={filterRailOpen ? "Hide filter panel" : "Show filter panel"}>
            <Button
              variant="ghost"
              size="icon"
              onClick={() => setFilterRailOpen(!filterRailOpen)}
              className={hasActiveFilters && !filterRailOpen ? "text-[var(--color-accent)]" : ""}
            >
              {filterRailOpen ? <PanelLeftClose size={15} /> : <PanelLeftOpen size={15} />}
            </Button>
          </Tooltip>

          {/* Timeline name */}
          <h2 className="text-sm font-semibold text-[var(--color-fg-primary)] truncate max-w-[180px]">
            {timeline?.name ?? "Loading…"}
          </h2>

          {/* Active filter chips (fill remaining space) */}
          <div className="flex-1 min-w-0 overflow-hidden">
            <FilterChips filters={filters} onRemove={removeFilter} />
          </div>

          {/* Right-side actions */}
          <div className="flex items-center gap-1.5 shrink-0 ml-auto">
            <TriageMeter annotations={annotations ?? []} totalEvents={total} />

            <Tooltip content={histogramOpen ? "Hide histogram" : "Show histogram"}>
              <Button
                variant={histogramOpen ? "accent" : "ghost"}
                size="icon"
                onClick={() => setHistogramOpen(!histogramOpen)}
              >
                <BarChart2 size={14} />
              </Button>
            </Tooltip>

            <Tooltip content="Refresh events">
              <Button variant="ghost" size="icon" onClick={() => refetch()}>
                {isFetching ? <Spinner size={14} /> : <RefreshCw size={14} />}
              </Button>
            </Tooltip>

            <ExportDialog
              caseId={caseId!}
              timelineId={timelineId!}
              filters={filters}
              total={total}
            />

            <ColumnPicker caseId={caseId!} timelineId={timelineId!} />

            <Tooltip content={analysisPanelOpen ? "Close analysis panel" : "Open analysis panel"}>
              <Button
                variant={analysisPanelOpen ? "accent" : "outline"}
                size="sm"
                onClick={() => setAnalysisPanelOpen(!analysisPanelOpen)}
              >
                <FlaskConical size={13} />
                Analysis
              </Button>
            </Tooltip>
          </div>
        </div>

        {/* Time histogram */}
        {histogramOpen && caseId && timelineId && (
          <TimelineHistogram
            caseId={caseId}
            timelineId={timelineId}
            filters={filters}
            onRangeSelect={handleHistogramRange}
          />
        )}

        {/* Main area */}
        <div className="flex flex-1 min-h-0 overflow-hidden">
          {eventsLoading && !eventsData ? (
            <div className="flex flex-1 items-center justify-center">
              <Spinner size={24} />
            </div>
          ) : eventsError ? (
            <div className="flex flex-1 items-center justify-center">
              <p className="text-sm text-[var(--color-danger)]">Failed to load events</p>
            </div>
          ) : (
            <div className="flex flex-1 min-w-0 flex-col overflow-hidden">
              {/* Select-all-matching-filter notice */}
              {showSelectAllBanner && (
                <div className="flex shrink-0 items-center gap-2 bg-[var(--color-accent)] px-3 py-1 text-xs text-[var(--color-accent-fg)]">
                  <span className="font-medium">All {events.length.toLocaleString()} loaded events selected.</span>
                  <button
                    className="font-semibold underline hover:no-underline"
                    onClick={() => setSelection({ mode: "all" })}
                  >
                    Select all {total.toLocaleString()} matching this filter
                  </button>
                  <span className="opacity-50">·</span>
                  <button
                    className="opacity-70 hover:opacity-100"
                    onClick={() => setSelection({ mode: "ids", ids: new Set() })}
                  >
                    Clear
                  </button>
                </div>
              )}
              <div className="flex flex-1 min-h-0 overflow-hidden">
                {/* Event grid — always present, fills all available width */}
                <EventGrid
                  events={events}
                  total={total}
                  annotations={annotationMap}
                  selectedIds={selectedIds}
                  caseId={caseId!}
                  timelineId={timelineId!}
                  onToggleSelect={handleToggleSelect}
                  onToggleSelectAll={handleToggleSelectAll}
                  expandedId={expandedEvent?.event_id ?? null}
                  onExpand={setExpandedEvent}
                  onLoadMore={handleLoadMore}
                  isFetching={isFetching}
                  visibleColumns={visibleColumns}
                  sortDir={sortDir}
                  onSortToggle={() => setSortDir(sortDir === "desc" ? "asc" : "desc")}
                />

                {/* Detail panel */}
                {expandedEvent && (
                  <EventDetailPanel
                    event={expandedEvent}
                    annotations={annotationMap.get(expandedEvent.event_id) ?? []}
                    caseId={caseId!}
                    sourceId={expandedEvent.source_id}
                    onClose={() => setExpandedEvent(null)}
                    onFindSimilar={handleFindSimilar}
                    onAddFilter={handleAddFilter}
                    tagSuggestions={tagSuggestions}
                  />
                )}

                {/* Analysis panel */}
                {analysisPanelOpen && timeline && (
                  <AnalysisPanel
                    caseId={caseId!}
                    timelineId={timelineId!}
                    hasVectors={hasVectors}
                    similarAnchor={similarAnchor}
                    onClose={() => {
                      setAnalysisPanelOpen(false);
                      setSimilarAnchor(null);
                    }}
                    onSelectEvent={(ev) => setExpandedEvent(ev)}
                    onSimilarClose={() => setSimilarAnchor(null)}
                  />
                )}
              </div>

              {/* Bulk action bar */}
              <BulkActionBar
                selectedEvents={events.filter((e) => selectedIds.has(e.event_id))}
                selectionCount={selectionCount}
                selectionMode={selection.mode}
                caseId={caseId!}
                timelineId={timelineId!}
                filters={filters}
                onClear={() => setSelection({ mode: "ids", ids: new Set() })}
                tagSuggestions={tagSuggestions}
              />
            </div>
          )}
        </div>
      </div>

      {/* Save view dialog */}
      <SaveViewDialog
        open={saveViewOpen}
        onClose={() => setSaveViewOpen(false)}
        caseId={caseId!}
        filters={filters}
      />
    </div>
  );
}
