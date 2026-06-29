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
import { useState, useCallback, useMemo } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import {
  FlaskConical,
  RefreshCw,
  PanelLeftClose,
  PanelLeftOpen,
  BarChart2,
  ArrowDownAZ,
  ArrowUpAZ,
} from "lucide-react";

import { eventsApi } from "@/api/events";
import { annotationsApi } from "@/api/annotations";
import { viewsApi } from "@/api/views";
import { timelinesApi } from "@/api/timelines";
import { useJobsStore } from "@/stores/jobs";
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

export function ExplorerPage() {
  const { caseId, timelineId } = useParams<{
    caseId: string;
    timelineId: string;
  }>();
  const [searchParams, setSearchParams] = useSearchParams();

  // ── Filter state (URL-driven) ──────────────────────────────────────────
  const filters = useMemo(() => paramsToFilters(searchParams), [searchParams]);
  const [offset, setOffset] = useState(0);

  const setFilters = useCallback(
    (f: EventFilters) => {
      setOffset(0);
      setSearchParams(filtersToParams(f));
    },
    [setSearchParams],
  );

  const removeFilter = useCallback(
    (key: keyof EventFilters | string, fieldKey?: string) => {
      const f = { ...filters };
      if (key === "filters" && fieldKey) {
        const { [fieldKey]: _removed, ...rest } = f.filters ?? {};
        f.filters = rest;
      } else if (key === "exclusions" && fieldKey) {
        const { [fieldKey]: _removed, ...rest } = f.exclusions ?? {};
        f.exclusions = rest;
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
   *   - filterKey "q"      → sets the top-level full-text search param
   *   - filterKey "source" → sets the dedicated source param (include only)
   *   - filterKey "tag"    → sets the dedicated tag param (include only)
   *   - everything else    → goes into filters{} or exclusions{}
   */
  const handleAddFilter = useCallback(
    (fieldKey: string, value: string, include: boolean) => {
      const f = { ...filters };

      if (fieldKey === "q") {
        // Full-text search: always "include" (no exclusion concept for free text)
        f.q = value;
      } else if (fieldKey === "source") {
        if (include) {
          f.source = value;
        } else {
          f.exclusions = { ...(f.exclusions ?? {}), source: value };
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
        f.exclusions = { ...(f.exclusions ?? {}), [fieldKey]: value };
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
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set());
  const [similarAnchor, setSimilarAnchor] = useState<Event | null>(null);
  const [saveViewOpen, setSaveViewOpen] = useState(false);
  const tlKey = `${caseId}/${timelineId}`;
  const visibleColumns = useUiStore((s) => s.visibleColumnsByTimeline[tlKey] ?? DEFAULT_COLUMNS);
  const histogramOpen = useUiStore((s) => s.histogramOpen);
  const setHistogramOpen = useUiStore((s) => s.setHistogramOpen);
  const sortDir = useUiStore((s) => s.sortDir);
  const setSortDir = useUiStore((s) => s.setSortDir);

  // ── Data queries ───────────────────────────────────────────────────────
  const addJob = useJobsStore((s) => s.addJob);

  const { data: timeline } = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  const {
    data: eventsPage,
    isLoading: eventsLoading,
    isFetching,
    isError: eventsError,
    refetch,
  } = useQuery({
    queryKey: ["events", caseId, timelineId, filters, offset, sortDir],
    queryFn: () =>
      eventsApi.list(caseId!, timelineId!, {
        ...filters,
        limit: PAGE_SIZE,
        offset,
        order: sortDir,
      }),
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

  const events = eventsPage?.events ?? [];
  const total = eventsPage?.total ?? 0;
  const hasVectors = (timeline?.vector_count ?? 0) > 0;

  // ── Handlers ───────────────────────────────────────────────────────────
  const handleToggleSelect = useCallback((id: string) => {
    setSelectedIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }, []);

  const handleLoadMore = useCallback(() => {
    if (!isFetching && events.length < total) setOffset((o) => o + PAGE_SIZE);
  }, [isFetching, events.length, total]);

  const handleEmbed = useCallback(async () => {
    if (!caseId || !timelineId || !timeline) return;
    const result = await timelinesApi.embed(caseId, timelineId);
    addJob(result.job_id, `Embedding "${timeline.name}"`, `${caseId}/${timelineId}`);
  }, [caseId, timelineId, timeline, addJob]);

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

            <Tooltip content={sortDir === "desc" ? "Newest first (click for oldest first)" : "Oldest first (click for newest first)"}>
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setSortDir(sortDir === "desc" ? "asc" : "desc")}
              >
                {sortDir === "desc" ? <ArrowDownAZ size={14} /> : <ArrowUpAZ size={14} />}
              </Button>
            </Tooltip>

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
          {eventsLoading && !eventsPage ? (
            <div className="flex flex-1 items-center justify-center">
              <Spinner size={24} />
            </div>
          ) : eventsError ? (
            <div className="flex flex-1 items-center justify-center">
              <p className="text-sm text-[var(--color-danger)]">Failed to load events</p>
            </div>
          ) : (
            <div className="flex flex-1 min-w-0 flex-col overflow-hidden">
              <div className="flex flex-1 min-h-0 overflow-hidden">
                {/* Event grid — always present, fills all available width */}
                <EventGrid
                  events={events}
                  total={total}
                  offset={offset}
                  annotations={annotationMap}
                  selectedIds={selectedIds}
                  caseId={caseId!}
                  timelineId={timelineId!}
                  onToggleSelect={handleToggleSelect}
                  expandedId={expandedEvent?.event_id ?? null}
                  onExpand={setExpandedEvent}
                  onLoadMore={handleLoadMore}
                  isFetching={isFetching}
                  visibleColumns={visibleColumns}
                />

                {/* Detail panel */}
                {expandedEvent && (
                  <EventDetailPanel
                    event={expandedEvent}
                    annotations={annotationMap.get(expandedEvent.event_id) ?? []}
                    caseId={caseId!}
                    timelineId={timelineId!}
                    onClose={() => setExpandedEvent(null)}
                    onFindSimilar={handleFindSimilar}
                    onAddFilter={handleAddFilter}
                  />
                )}

                {/* Analysis panel */}
                {analysisPanelOpen && (
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
                    onEmbed={handleEmbed}
                  />
                )}
              </div>

              {/* Bulk action bar */}
              <BulkActionBar
                selectedIds={[...selectedIds]}
                caseId={caseId!}
                timelineId={timelineId!}
                onClear={() => setSelectedIds(new Set())}
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
