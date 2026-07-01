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
import { useState, useCallback, useMemo, useEffect, useRef } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useQuery, useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import {
  FlaskConical,
  RefreshCw,
  PanelLeftClose,
  PanelLeftOpen,
  BarChart2,
} from "lucide-react";

import { eventsApi } from "@/api/events";
import { annotationsApi } from "@/api/annotations";
import { similarityApi } from "@/api/similarity";
import { viewsApi } from "@/api/views";
import { timelinesApi } from "@/api/timelines";
import { useUiStore, DEFAULT_COLUMNS } from "@/stores/ui";
import { paramsToFilters, filtersToParams } from "@/lib/queryParams";

import { FilterRail } from "@/components/explorer/FilterRail";
import { FilterChips } from "@/components/explorer/FilterChips";
import { EventGrid, type EventGridHandle } from "@/components/explorer/EventGrid";
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

import type { AnomalyMarker, Event, EventFilters, EventPage, Annotation } from "@/api/types";

const PAGE_SIZE = 100;

/** Matches a ClickHouse UUID event_id, used to detect an event_id typed into
 * the filter rail's search box (vs. a keyword/semantic query). */
const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** Keyset pagination page param — `{}` requests the initial offset-0 page. */
type EventsPageParam = { after?: string; before?: string };

function cursorParam(cursor: [string, string] | null): string | undefined {
  return cursor ? `${cursor[0]},${cursor[1]}` : undefined;
}

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
      } else if (key === "artifacts") {
        const remaining = value !== undefined
          ? (f.artifacts ?? []).filter((a) => a !== value)
          : [];
        if (remaining.length > 0) f.artifacts = remaining;
        else delete f.artifacts;
      } else if (key === "tagsInclude") {
        const remaining = value !== undefined
          ? (f.tagsInclude ?? []).filter((t) => t !== value)
          : [];
        if (remaining.length > 0) f.tagsInclude = remaining;
        else delete f.tagsInclude;
      } else if (key === "tagsExclude") {
        const remaining = value !== undefined
          ? (f.tagsExclude ?? []).filter((t) => t !== value)
          : [];
        if (remaining.length > 0) f.tagsExclude = remaining;
        else delete f.tagsExclude;
      } else if (key === "annotated") {
        const remaining = value !== undefined
          ? (f.annotated ?? []).filter((t) => t !== value)
          : [];
        if (remaining.length > 0) {
          f.annotated = remaining as ("tag" | "anomaly")[];
        } else {
          delete f.annotated;
          delete f.annotationTagValue;
        }
      } else {
        delete f[key as keyof EventFilters];
      }
      setFilters(f);
    },
    [filters, setFilters],
  );

  /**
   * Special cases:
   *   - filterKey "q"       → sets the top-level full-text search param
   *   - filterKey "artifact" → sets the dedicated artifact param (include only)
   *   - filterKey "tag"     → sets the dedicated tag param (include only)
   *   - everything else     → goes into filters{} or exclusions{}
   */
  const applyFieldFilter = useCallback(
    (f: EventFilters, fieldKey: string, value: string, include: boolean): EventFilters => {
      const next = { ...f };

      if (fieldKey === "q") {
        // Full-text search: always "include" (no exclusion concept for free text)
        next.q = value;
      } else if (fieldKey === "artifact") {
        if (include) {
          next.artifact = value;
        } else {
          const prev = next.exclusions?.artifact ?? [];
          if (!prev.includes(value)) {
            next.exclusions = { ...(next.exclusions ?? {}) as Record<string, string[]>, artifact: [...prev, value] };
          }
        }
      } else if (fieldKey === "tag") {
        if (include) {
          next.tag = value;
        } else {
          next.excludeTag = value;
        }
      } else if (include) {
        next.filters = { ...(next.filters ?? {}), [fieldKey]: value };
      } else {
        const prev = next.exclusions?.[fieldKey] ?? [];
        if (!prev.includes(value)) {
          next.exclusions = { ...(next.exclusions ?? {}) as Record<string, string[]>, [fieldKey]: [...prev, value] };
        }
      }

      return next;
    },
    [],
  );

  /** Handler wired to the detail panel's filter-in/filter-out buttons. */
  const handleAddFilter = useCallback(
    (fieldKey: string, value: string, include: boolean) => {
      setFilters(applyFieldFilter(filters, fieldKey, value, include));
    },
    [filters, setFilters, applyFieldFilter],
  );

  /** Maps an anomaly-finding field token to a filter-rail filterKey. */
  const mapAnomalyField = useCallback((field: string): string => {
    if (field.startsWith("attr:")) return field.slice(5);
    if (field === "tags") return "tag";
    return field;
  }, []);

  /** Wired to ValueNoveltyView — sets a field=value filter from a rare-value finding. */
  const handleDrillField = useCallback(
    (field: string, value: string) => {
      setFilters(applyFieldFilter(filters, mapAnomalyField(field), value, true));
    },
    [filters, setFilters, applyFieldFilter, mapAnomalyField],
  );

  /**
   * Wired to FrequencyView — narrows the time range to the anomalous window
   * AND filters to the series field=value that spiked, in a single update.
   */
  const handleFrequencyDrill = useCallback(
    (field: string, value: string, start: string, end: string) => {
      setFilters({
        ...applyFieldFilter(filters, mapAnomalyField(field), value, true),
        start,
        end,
      });
    },
    [filters, setFilters, applyFieldFilter, mapAnomalyField],
  );

  // ── Panel visibility state ────────────────────────────────────────────
  const filterRailOpen = useUiStore((s) => s.filterRailOpen);
  const setFilterRailOpen = useUiStore((s) => s.setFilterRailOpen);
  const analysisPanelOpen = useUiStore((s) => s.analysisPanelOpen);
  const setAnalysisPanelOpen = useUiStore((s) => s.setAnalysisPanelOpen);
  const [expandedEvent, setExpandedEvent] = useState<Event | null>(null);
  const [selection, setSelection] = useState<SelectionState>({ mode: "ids", ids: new Set() });
  const [similarAnchor, setSimilarAnchor] = useState<Event | null>(null);
  const [anomalyMarkers, setAnomalyMarkers] = useState<AnomalyMarker[]>([]);
  const [scrollPositionTs, setScrollPositionTs] = useState<string | null>(null);
  const [saveViewOpen, setSaveViewOpen] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const gridRef = useRef<EventGridHandle>(null);
  // Snapshot of `filters` taken right before a "jump to time" cleared them —
  // drives the "back to filtered view" breadcrumb. `rangeHighlight` is purely
  // visual (a Frequency finding's anomalous window), never a URL filter.
  const [preJumpFilters, setPreJumpFilters] = useState<EventFilters | null>(null);
  const [rangeHighlight, setRangeHighlight] = useState<{ start: string; end: string } | null>(null);
  const pendingJumpRef = useRef<{ ts: string; eventId?: string } | null>(null);
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

  // Event IDs currently flagged by the active (not-yet-persisted) Analysis
  // tab. Only meaningful to the events/histogram/export queries when the
  // "Anomaly" filter checkbox is active — merged in below so that filter
  // also matches live findings, not just persisted anomaly annotations.
  const liveAnomalyEventIds = useMemo(
    () =>
      Array.from(
        new Set(anomalyMarkers.map((m) => m.eventId).filter((id): id is string => !!id)),
      ),
    [anomalyMarkers],
  );

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

  const hasVectors = timelineSources?.some((s) => s.vector_count > 0) ?? false;

  // The filter rail's search box runs semantic search in the background once
  // embeddings exist for this timeline, so a free-text query narrows the grid
  // to conceptually related events even when they don't literally contain the
  // typed words. `filters.q` itself stays URL-shareable and drives the
  // broadened keyword search server-side as a fallback while this is loading
  // or when there are no embeddings to search.
  const { data: semanticSearchData, isFetching: semanticSearchPending } = useQuery({
    queryKey: ["search-filter", caseId, timelineId, filters.q],
    queryFn: () => similarityApi.semanticSearch(caseId!, filters.q!, 200, timelineId),
    enabled: !!(caseId && timelineId && hasVectors && filters.q),
  });
  const semanticSearchIds = useMemo(() => {
    if (!filters.q || !hasVectors || semanticSearchData?.status !== "ok") return null;
    return semanticSearchData.results.map((r) => r.event_id);
  }, [filters.q, hasVectors, semanticSearchData]);

  // The filter object actually sent to the events/histogram/export queries.
  // `filters` itself stays URL-serializable/shareable — this augments it
  // with ephemeral live-finding event IDs and semantic search candidates
  // only while relevant, so switching detector tabs or field selections
  // correctly refetches the filtered view.
  const effectiveFilters = useMemo<EventFilters>(() => {
    let f = filters;
    if (filters.annotated?.includes("anomaly") && liveAnomalyEventIds.length > 0) {
      f = { ...f, liveAnomalyEventIds };
    }
    // Semantic candidates replace the broadened keyword search server-side
    // (rather than ANDing with it) — a semantically relevant event may not
    // literally contain the typed words, so intersecting would wrongly drop it.
    if (semanticSearchIds !== null) {
      f = { ...f, q: undefined, ids: semanticSearchIds };
    }
    return f;
  }, [filters, liveAnomalyEventIds, semanticSearchIds]);

  const queryClient = useQueryClient();
  const eventsQueryKey = ["events", caseId, timelineId, effectiveFilters, sortDir];

  const {
    data: eventsData,
    isLoading: eventsLoading,
    isFetching,
    isError: eventsError,
    refetch,
    fetchNextPage,
    hasNextPage,
    fetchPreviousPage,
    hasPreviousPage,
  } = useInfiniteQuery({
    queryKey: eventsQueryKey,
    queryFn: ({ pageParam, signal }) =>
      eventsApi.list(
        caseId!,
        timelineId!,
        { ...effectiveFilters, limit: PAGE_SIZE, order: sortDir },
        signal,
        pageParam,
      ),
    initialPageParam: {} as EventsPageParam,
    getNextPageParam: (lastPage) =>
      lastPage.has_more_after && lastPage.next_cursor
        ? { after: cursorParam(lastPage.next_cursor) }
        : undefined,
    getPreviousPageParam: (firstPage) =>
      firstPage.has_more_before && firstPage.prev_cursor
        ? { before: cursorParam(firstPage.prev_cursor) }
        : undefined,
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

  const { data: mergedTagSuggestions = [] } = useQuery({
    queryKey: ["tags-merged", caseId, timelineId],
    queryFn: () => eventsApi.mergedTags(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  const { data: artifactSuggestions = [] } = useQuery({
    queryKey: ["artifacts", caseId, timelineId],
    queryFn: () => eventsApi.artifacts(caseId!, timelineId!),
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

  // Findings from the active (not-yet-tagged) analysis tab, keyed by event ID —
  // lets the grid mark rows and the detail panel show/persist findings before
  // they're saved as annotations via the "Tag" button.
  const liveAnomaliesByEvent = useMemo<Map<string, AnomalyMarker[]>>(() => {
    const m = new Map<string, AnomalyMarker[]>();
    for (const marker of anomalyMarkers) {
      if (!marker.eventId) continue;
      const list = m.get(marker.eventId) ?? [];
      list.push(marker);
      m.set(marker.eventId, list);
    }
    return m;
  }, [anomalyMarkers]);

  const events = useMemo(() => eventsData?.pages.flatMap((p) => p.events) ?? [], [eventsData]);
  // Only the initial, uncursored page carries a real COUNT(*) — later pages
  // (forward, backward, or a jump-to-time seek) return `total: null`. Keep it
  // `null` rather than defaulting to 0 — a jump-to-time session may never load
  // an offset-mode page, and 0 would read as "no matching events" when the
  // true count is simply unknown.
  const total = eventsData?.pages.find((p) => p.total != null)?.total ?? null;

  // Derive a plain Set<string> of selected IDs for components that don't know
  // about the "all" mode (EventGrid checkboxes). In "all" mode we show all
  // loaded rows as selected.
  const selectedIds = useMemo<Set<string>>(() => {
    if (selection.mode === "all") return new Set(events.map((e) => e.event_id));
    return selection.ids;
  }, [selection, events]);

  // Total count shown in BulkActionBar label
  const selectionCount = selection.mode === "all" ? (total ?? events.length) : selection.ids.size;

  // Show the "select all N matching" banner when all loaded rows are in "ids"
  // mode selection and there are more events not yet loaded.
  const showSelectAllBanner =
    selection.mode === "ids" &&
    selection.ids.size === events.length &&
    events.length > 0 &&
    (total === null || total > events.length);

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

  const handleLoadEarlier = useCallback(() => {
    if (!isFetching && hasPreviousPage) fetchPreviousPage();
  }, [isFetching, hasPreviousPage, fetchPreviousPage]);

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

  /**
   * Wired to the Analysis panel's "jump to time" buttons and the Event
   * Detail panel's "locate in timeline" button. The finding's timestamp may
   * not match the currently active filters at all, so this clears them
   * outright (guaranteeing the target is visible) rather than trying to
   * merge — the analyst can restore the prior view via the breadcrumb this
   * leaves behind. Since the target likely isn't in the already-loaded
   * window, this also seeds the query cache with a fresh page anchored at
   * the target, so bidirectional scroll continues correctly from there.
   *
   * A plain `before`-cursor seek would exclude the target event itself
   * (cursors are strict boundaries — that's correct for normal pagination,
   * where the caller already has the anchor row and wants the *next* batch).
   * For a seek we need the target row present so it can be scrolled to,
   * highlighted (via the detail panel's "expanded" row styling), and opened
   * — so when `eventId` is known, fetch the surrounding pages on both sides
   * and splice the target event itself (via `getById`) in between.
   */
  const handleJumpToTime = useCallback(
    async (ts: string, eventId?: string, windowEnd?: string) => {
      if (!caseId || !timelineId) return;
      setPreJumpFilters((prev) => prev ?? filters);
      setRangeHighlight(windowEnd ? { start: ts, end: windowEnd } : null);
      pendingJumpRef.current = { ts, eventId };
      setFilters({});

      let anchorPage: EventPage;
      if (eventId) {
        const halfBefore = Math.floor(PAGE_SIZE / 2);
        const halfAfter = PAGE_SIZE - halfBefore - 1;
        const [targetEvent, beforePage, afterPage] = await Promise.all([
          eventsApi.getById(caseId, timelineId, eventId),
          eventsApi.list(
            caseId,
            timelineId,
            { limit: halfBefore, order: sortDir },
            undefined,
            { before: `${ts},${eventId}` },
          ),
          eventsApi.list(
            caseId,
            timelineId,
            { limit: halfAfter, order: sortDir },
            undefined,
            { after: `${ts},${eventId}` },
          ),
        ]);
        const combinedEvents = [
          ...beforePage.events,
          ...(targetEvent ? [targetEvent] : []),
          ...afterPage.events,
        ];
        const first = combinedEvents[0];
        const last = combinedEvents[combinedEvents.length - 1];
        anchorPage = {
          total: null,
          offset: 0,
          limit: PAGE_SIZE,
          events: combinedEvents,
          has_more_after: afterPage.has_more_after,
          has_more_before: beforePage.has_more_before,
          next_cursor: last ? [last.timestamp ?? "", last.event_id] : null,
          prev_cursor: first ? [first.timestamp ?? "", first.event_id] : null,
        };
      } else {
        anchorPage = await eventsApi.list(
          caseId,
          timelineId,
          { limit: PAGE_SIZE, order: sortDir },
          undefined,
          { before: `${ts},` },
        );
      }
      queryClient.setQueryData(["events", caseId, timelineId, {}, sortDir], {
        pages: [anchorPage],
        pageParams: [{} as EventsPageParam],
      });
    },
    [caseId, timelineId, filters, setFilters, sortDir, queryClient],
  );

  const handleBackToFiltered = useCallback(() => {
    if (preJumpFilters) setFilters(preJumpFilters);
    setPreJumpFilters(null);
    setRangeHighlight(null);
  }, [preJumpFilters, setFilters]);

  /**
   * Wired to the filter rail's unified search box. Dispatches on the shape of
   * the input: an exact event_id (UUID) jumps straight to that event via the
   * existing jump-to-time machinery; anything else becomes `filters.q`, which
   * drives a broadened all-fields keyword search server-side and — once
   * embeddings exist for this timeline — is also narrowed by a background
   * semantic search (see the `semanticSearchIds` effective-filter override).
   */
  const handleSearchSubmit = useCallback(
    async (raw: string) => {
      setSearchError(null);
      const value = raw.trim();
      if (!value) {
        if (filters.q) setFilters({ ...filters, q: undefined });
        return;
      }
      if (UUID_RE.test(value) && caseId && timelineId) {
        const event = await eventsApi.getById(caseId, timelineId, value);
        if (!event || !event.timestamp) {
          setSearchError("No event found with that id");
          return;
        }
        handleJumpToTime(event.timestamp, event.event_id);
        return;
      }
      setFilters({ ...filters, q: value });
    },
    [filters, setFilters, caseId, timelineId, handleJumpToTime],
  );

  const searchStatus = useMemo(() => {
    if (searchError) return searchError;
    if (!filters.q) return undefined;
    if (!hasVectors) return "keyword search — no embeddings for this timeline";
    if (semanticSearchPending) return undefined; // spinner already shown
    if (semanticSearchData?.status === "ok") {
      return `${semanticSearchData.results.length} semantic match${semanticSearchData.results.length === 1 ? "" : "es"}`;
    }
    if (semanticSearchData?.status === "not_embedded") {
      return "not embedded — keyword fallback";
    }
    return undefined;
  }, [searchError, filters.q, hasVectors, semanticSearchPending, semanticSearchData]);

  // Once the jump target's anchor page has landed in `events`, scroll the
  // grid to it, open its detail panel (so the target is unmistakable — the
  // detail panel's own "expanded" row styling doubles as the highlight),
  // and clear the pending marker. Findings without a specific event (e.g. a
  // Frequency window) have nothing to expand — the range highlight already
  // marks the window instead.
  useEffect(() => {
    const pending = pendingJumpRef.current;
    if (!pending) return;
    const foundEvent = pending.eventId
      ? events.find((e) => e.event_id === pending.eventId)
      : undefined;
    const ready = pending.eventId ? !!foundEvent : events.length > 0;
    if (ready) {
      gridRef.current?.scrollToTimestamp(pending.ts, pending.eventId);
      if (foundEvent) setExpandedEvent(foundEvent);
      pendingJumpRef.current = null;
    }
  }, [events]);

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
          mergedTagSuggestions={mergedTagSuggestions}
          artifactSuggestions={artifactSuggestions}
          onSearchSubmit={handleSearchSubmit}
          searchStatus={searchStatus}
          searchPending={hasVectors && !!filters.q && semanticSearchPending}
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
              filters={effectiveFilters}
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
            filters={effectiveFilters}
            onRangeSelect={handleHistogramRange}
            markers={analysisPanelOpen ? anomalyMarkers : []}
            currentPositionTs={scrollPositionTs}
            highlightRange={rangeHighlight}
          />
        )}

        {/* "Jumped to time" breadcrumb — shown after a jump-to-time cleared filters */}
        {preJumpFilters && (
          <div className="flex shrink-0 items-center gap-2 bg-[var(--color-accent-dim)] px-3 py-1 text-xs text-[var(--color-fg-primary)]">
            <span>Jumped to a point in time — filters cleared.</span>
            <button
              className="font-semibold text-[var(--color-accent)] hover:underline"
              onClick={handleBackToFiltered}
            >
              Back to filtered view
            </button>
          </div>
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
                    Select all {total !== null ? `${total.toLocaleString()} ` : ""}matching this
                    filter
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
                  ref={gridRef}
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
                  onLoadEarlier={handleLoadEarlier}
                  hasPreviousPage={!!hasPreviousPage}
                  hasNextPage={!!hasNextPage}
                  isFetching={isFetching}
                  visibleColumns={visibleColumns}
                  sortDir={sortDir}
                  onSortToggle={() => setSortDir(sortDir === "desc" ? "asc" : "desc")}
                  liveAnomalies={liveAnomaliesByEvent}
                  onVisibleTimestampChange={setScrollPositionTs}
                  highlightRange={rangeHighlight}
                />

                {/* Detail panel */}
                {expandedEvent && (
                  <EventDetailPanel
                    event={expandedEvent}
                    annotations={annotationMap.get(expandedEvent.event_id) ?? []}
                    liveFindings={liveAnomaliesByEvent.get(expandedEvent.event_id) ?? []}
                    caseId={caseId!}
                    sourceId={expandedEvent.source_id}
                    onClose={() => setExpandedEvent(null)}
                    onFindSimilar={handleFindSimilar}
                    onAddFilter={handleAddFilter}
                    onJumpToTime={handleJumpToTime}
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
                    onDrillField={handleDrillField}
                    onFrequencyDrill={handleFrequencyDrill}
                    onAnomalyMarkers={setAnomalyMarkers}
                    onJumpToTime={handleJumpToTime}
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
