/**
 * ExplorerPage — the heart of TraceSignal.
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
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery, useInfiniteQuery, useQueryClient } from "@tanstack/react-query";
import {
  FlaskConical,
  RefreshCw,
  PanelLeftClose,
  PanelLeftOpen,
  BarChart2,
  AreaChart,
} from "lucide-react";

import { eventsApi } from "@/api/events";
import { annotationsApi } from "@/api/annotations";
import { similarityApi } from "@/api/similarity";
import { viewsApi } from "@/api/views";
import { timelinesApi } from "@/api/timelines";
import { useUiStore, DEFAULT_COLUMNS } from "@/stores/ui";
import { useScrollPositionStore } from "@/stores/scrollPosition";
import { paramsToFilters, filtersToParams } from "@/lib/queryParams";
import { useCaseStream } from "@/hooks/useCaseStream";

import { FilterRail } from "@/components/explorer/FilterRail";
import { FilterChips } from "@/components/explorer/FilterChips";
import { EventGrid, type EventGridHandle } from "@/components/explorer/EventGrid";
import { EventDetailPanel } from "@/components/explorer/EventDetailPanel";
import { BulkActionBar } from "@/components/explorer/BulkActionBar";
import { ExportDialog } from "@/components/explorer/ExportDialog";
import { SaveViewDialog } from "@/components/explorer/SaveViewDialog";
import { ColumnPicker } from "@/components/explorer/ColumnPicker";
import { TimelineHistogram } from "@/components/explorer/TimelineHistogram";
import { FieldHistogramModal } from "@/components/viz/FieldHistogramModal";
import { AnalysisPanel } from "@/components/analysis/AnalysisPanel";
import { TriageMeter } from "@/components/triage/TriageMeter";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";

import type { AnomalyMarker, Event, EventFilters, EventPage, Annotation, FieldMatchMode } from "@/api/types";

/** Remove `key`'s match-mode entry; collapse to undefined when the map empties. */
function dropMode(
  modes: Record<string, FieldMatchMode> | undefined,
  key: string,
): Record<string, FieldMatchMode> | undefined {
  if (!modes || !(key in modes)) return modes;
  const { [key]: _removed, ...rest } = modes;
  return Object.keys(rest).length > 0 ? rest : undefined;
}

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

  // Live collaboration: another analyst's tag/annotation shows up here
  // within about a second, no manual refresh needed.
  useCaseStream(caseId);

  // ── Filter state (URL-driven) ──────────────────────────────────────────
  const filters = useMemo(() => paramsToFilters(searchParams), [searchParams]);

  const queryClient = useQueryClient();
  const sortDir = useUiStore((s) => s.sortDir);

  // Anchors the just-committed filter change's query at the timestamp the
  // analyst was already looking at, so e.g. adding a tag filter doesn't
  // silently reset the grid to the top of the result set. Only applies when
  // start/end are untouched — an explicit range change (histogram brush,
  // frequency drill) should show its own new range, not the old anchor.
  const pendingSoftAnchorRef = useRef<{ ts: string; seq: number } | null>(null);
  const softAnchorSeqRef = useRef(0);
  const softAnchorSeededSeqRef = useRef(0);

  const setFilters = useCallback(
    (f: EventFilters) => {
      // Any new filter change invalidates a previously pending soft-anchor
      // scroll — otherwise it can fire against an unrelated later `events`
      // update (e.g. a subsequent range change or jump-to-time).
      pendingSoftAnchorRef.current = null;
      const rangeUnchanged = f.start === filters.start && f.end === filters.end;
      const anchorTs = useScrollPositionStore.getState().currentPositionTs;
      // A bare `{}` is handleJumpToTime's own clear-and-seek call — it seeds
      // the same query key itself, so skip here to avoid both racing on it.
      const isJumpClear = Object.keys(f).length === 0;
      const shouldAnchor = rangeUnchanged && !isJumpClear && anchorTs && caseId && timelineId;

      // Commit the URL/filter change first — matches handleJumpToTime's
      // ordering, so the seed below races the hook's own auto-fetch the same
      // (already-accepted) way a jump-to-time seed does.
      setSearchParams(filtersToParams(f));

      if (shouldAnchor) {
        const seq = ++softAnchorSeqRef.current;
        let nextEffective = f;
        if (f.annotated?.includes("anomaly") && anomalyRunIdRef.current) {
          nextEffective = { ...nextEffective, anomalyRunId: anomalyRunIdRef.current };
        }
        if (semanticSearchIdsRef.current !== null) {
          nextEffective = { ...nextEffective, q: undefined, ids: semanticSearchIdsRef.current };
        }
        const targetKey = ["events", caseId, timelineId, nextEffective, sortDir];
        queryClient.cancelQueries({ queryKey: targetKey }).then(async () => {
          if (softAnchorSeqRef.current !== seq) return;
          const anchorPage = await eventsApi.list(
            caseId,
            timelineId,
            { ...nextEffective, limit: PAGE_SIZE, order: sortDir },
            undefined,
            { before: `${anchorTs},` },
          );
          if (softAnchorSeqRef.current !== seq) return;
          // Cancel again — the URL change above may have let the hook's own
          // auto-fetch (initialPageParam {}) start in the interim, and this
          // seed must be the last write to `targetKey`, not that one.
          await queryClient.cancelQueries({ queryKey: targetKey });
          if (softAnchorSeqRef.current !== seq) return;
          // A restrictive filter change (e.g. drilling to a rare artifact from
          // the analysis panel) can have zero matching events before the old
          // scroll timestamp. Seeding that empty `before`-page would strand the
          // grid: it renders nothing, and a before-mode page reports
          // has_more_after=false with a null next_cursor, so "load more" is
          // dead too — the analyst is stuck until a jump/histogram click
          // reseeds. Skip the seed and let the hook fetch its default first
          // page (top of the filtered result set) instead — invalidate to
          // re-trigger the fetch we cancelled above.
          if (anchorPage.events.length === 0) {
            queryClient.invalidateQueries({ queryKey: targetKey });
            return;
          }
          const anchorPageParam: EventsPageParam = { before: cursorParam(anchorPage.prev_cursor) };
          queryClient.setQueryData(targetKey, {
            pages: [anchorPage],
            pageParams: [anchorPageParam],
          });
          pendingSoftAnchorRef.current = { ts: anchorTs, seq };
          softAnchorSeededSeqRef.current = seq;
        });
      }
    },
    [setSearchParams, filters, caseId, timelineId, queryClient, sortDir],
  );

  // Latest anomalyRunId/semanticSearchIds for setFilters' soft-anchor seek
  // above, which runs before those states are declared further down this
  // component — refs sidestep the ordering (and closure-staleness) issue.
  const anomalyRunIdRef = useRef<string | undefined>(undefined);
  const semanticSearchIdsRef = useRef<string[] | null>(null);

  const removeFilter = useCallback(
    (key: keyof EventFilters | string, fieldKey?: string, value?: string) => {
      const f = { ...filters };
      if (key === "filters" && fieldKey) {
        const { [fieldKey]: _removed, ...rest } = f.filters ?? {};
        f.filters = rest;
        f.filterModes = dropMode(f.filterModes, fieldKey);
      } else if (key === "exclusions" && fieldKey) {
        if (value !== undefined) {
          const remaining = (f.exclusions?.[fieldKey] ?? []).filter((v) => v !== value);
          if (remaining.length === 0) {
            const { [fieldKey]: _removed, ...rest } = f.exclusions ?? {};
            f.exclusions = rest;
            f.exclusionModes = dropMode(f.exclusionModes, fieldKey);
          } else {
            f.exclusions = { ...(f.exclusions ?? {}) as Record<string, string[]>, [fieldKey]: remaining };
          }
        } else {
          const { [fieldKey]: _removed, ...rest } = f.exclusions ?? {};
          f.exclusions = rest;
          f.exclusionModes = dropMode(f.exclusionModes, fieldKey);
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
        // Grid-cell values are literal — reset any pattern mode on the key,
        // otherwise the cell text would be reinterpreted as glob/regex.
        next.filterModes = dropMode(next.filterModes, fieldKey);
      } else {
        const prev = next.exclusions?.[fieldKey] ?? [];
        if (!prev.includes(value)) {
          next.exclusions = { ...(next.exclusions ?? {}) as Record<string, string[]>, [fieldKey]: [...prev, value] };
          // Same literal-value rule; mode is per key, so this also flips any
          // pre-existing pattern-mode values of the key back to exact —
          // visible via the chips' badge disappearing.
          next.exclusionModes = dropMode(next.exclusionModes, fieldKey);
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

  /** Wired to the detail panel's per-field histogram button. */
  const [fieldHistogram, setFieldHistogram] = useState<{ fieldKey: string; value: string } | null>(
    null,
  );
  const handleShowFieldHistogram = useCallback((fieldKey: string, value: string) => {
    setFieldHistogram({ fieldKey, value });
  }, []);

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
  const [anomalyRunId, setAnomalyRunId] = useState<string | undefined>(undefined);
  // Scroll position feeds TimelineHistogram only, via a store subscribed
  // solely by that component (C15) — not page state, so scrolling doesn't
  // re-render EventGrid/FilterRail/AnalysisPanel on every row crossed.
  const setCurrentPositionTs = useScrollPositionStore((s) => s.setCurrentPositionTs);
  const [saveViewOpen, setSaveViewOpen] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const gridRef = useRef<EventGridHandle>(null);
  // Snapshot of `filters` taken right before a "jump to time" cleared them —
  // drives the "back to filtered view" breadcrumb. `rangeHighlight` is purely
  // visual (a Frequency finding's anomalous window), never a URL filter.
  const [preJumpFilters, setPreJumpFilters] = useState<EventFilters | null>(null);
  const [rangeHighlight, setRangeHighlight] = useState<{ start: string; end: string } | null>(null);
  const pendingJumpRef = useRef<{ ts: string; eventId?: string; seq: number } | null>(null);
  // Bumped on every jump; the pending-jump effect only trusts `events` once
  // `seededSeqRef` catches up, so a stray automatic fetch landing mid-jump
  // (or a second jump superseding the first) can't be mistaken for "ready".
  const jumpSeqRef = useRef(0);
  const seededSeqRef = useRef(0);
  const tlKey = `${caseId}/${timelineId}`;
  const visibleColumns = useUiStore((s) => s.visibleColumnsByTimeline[tlKey] ?? DEFAULT_COLUMNS);
  const histogramOpen = useUiStore((s) => s.histogramOpen);
  const setHistogramOpen = useUiStore((s) => s.setHistogramOpen);
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
    // Poll while any source is still ingesting so the banner clears (and the
    // grid picks the source up) without a manual refresh.
    refetchInterval: (query) =>
      query.state.data?.some((s) => s.status !== "ready") ? 4000 : false,
  });

  const hasVectors = timelineSources?.some((s) => s.vector_count > 0) ?? false;
  // Sources still ingesting are excluded from every query by the backend
  // (events._resolve_timeline_scope) — surface that so partial results are
  // never mistaken for complete ones.
  const ingestingSources = timelineSources?.filter((s) => s.status !== "ready") ?? [];

  // Semantic search only runs when the analyst deliberately picked Semantic
  // mode in the filter rail — keyword (the default) never silently becomes
  // semantic, so search semantics are always explicit and reproducible from
  // the URL. `filters.q` stays URL-shareable and drives the broadened keyword
  // search server-side while semantic results are loading or unavailable.
  const semanticMode = filters.qMode === "semantic";
  const { data: semanticSearchData, isFetching: semanticSearchPending } = useQuery({
    queryKey: ["search-filter", caseId, timelineId, filters.q],
    queryFn: () => similarityApi.semanticSearch(caseId!, filters.q!, 200, timelineId),
    enabled: !!(caseId && timelineId && hasVectors && filters.q && semanticMode),
  });
  const semanticSearchIds = useMemo(() => {
    if (!filters.q || !semanticMode || !hasVectors || semanticSearchData?.status !== "ok") {
      return null;
    }
    return semanticSearchData.results.map((r) => r.event_id);
  }, [filters.q, semanticMode, hasVectors, semanticSearchData]);

  useEffect(() => {
    anomalyRunIdRef.current = anomalyRunId;
    semanticSearchIdsRef.current = semanticSearchIds;
  }, [anomalyRunId, semanticSearchIds]);

  // The filter object actually sent to the events/histogram/export queries.
  // `filters` itself stays URL-serializable/shareable — this augments it
  // with the active Analysis tab's persisted run_id and semantic search
  // candidates only while relevant, so switching detector tabs or field
  // selections correctly refetches the filtered view.
  const effectiveFilters = useMemo<EventFilters>(() => {
    let f = filters;
    if (filters.annotated?.includes("anomaly") && anomalyRunId) {
      f = { ...f, anomalyRunId };
    }
    // Semantic candidates replace the broadened keyword search server-side
    // (rather than ANDing with it) — a semantically relevant event may not
    // literally contain the typed words, so intersecting would wrongly drop it.
    if (semanticSearchIds !== null) {
      f = { ...f, q: undefined, ids: semanticSearchIds };
    }
    return f;
  }, [filters, anomalyRunId, semanticSearchIds]);

  const eventsQueryKey = ["events", caseId, timelineId, effectiveFilters, sortDir];

  // When the last ingesting source flips to ready the backend starts
  // including it in query scope — refetch the grid and histogram so the new
  // events appear without a manual refresh.
  const ingestingCount = ingestingSources.length;
  const prevIngestingCount = useRef(ingestingCount);
  useEffect(() => {
    if (prevIngestingCount.current > 0 && ingestingCount === 0) {
      queryClient.invalidateQueries({ queryKey: ["events", caseId, timelineId] });
      queryClient.invalidateQueries({ queryKey: ["histogram", caseId, timelineId] });
    }
    prevIngestingCount.current = ingestingCount;
  }, [ingestingCount, caseId, timelineId, queryClient]);

  const {
    data: eventsData,
    isLoading: eventsLoading,
    isFetching,
    isError: eventsError,
    error: eventsQueryError,
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
    getNextPageParam: (lastPage, _allPages, lastPageParam) => {
      if (lastPage.has_more_after && lastPage.next_cursor) {
        return { after: cursorParam(lastPage.next_cursor) };
      }
      // A jump-to-time anchor with no target event is seeded from a
      // `before`-mode fetch (see handleJumpToTime), and before-mode only
      // ever computes `has_more_before` — `has_more_after` is always false
      // regardless of whether more events actually follow. Synthesize the
      // forward cursor from this page's own last row instead of reporting
      // "no more" when we simply don't know; the resulting `after` fetch is
      // a normal cursor fetch that correctly reports its own has_more_after,
      // so this synthesis is only needed for the seeded page itself.
      if (lastPageParam?.before && lastPage.next_cursor) {
        return { after: cursorParam(lastPage.next_cursor) };
      }
      return undefined;
    },
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

  // Field names across all of this timeline's sources, for the filter rail's
  // Field=Value / Field≠Value key autocomplete (same cache as ColumnPicker).
  const { data: fieldsData } = useQuery({
    queryKey: ["fields", caseId, timelineId],
    queryFn: () => eventsApi.fields(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });
  const fieldSuggestions = useMemo(
    () => [...(fieldsData?.top_level ?? []), ...(fieldsData?.attributes ?? [])],
    [fieldsData],
  );

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
  }, [setAnalysisPanelOpen]);

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
      const seq = ++jumpSeqRef.current;
      pendingJumpRef.current = { ts, eventId, seq };
      setFilters({});

      // `filters` is about to become `{}` (above), so the live query key is
      // about to become this — not the current-render `eventsQueryKey`
      // closure, which still reflects the pre-jump filters. Cancel whatever
      // the automatic refetch triggered by that key change is doing before
      // seeding the cache, or it can resolve after `setQueryData` below and
      // silently overwrite the anchor page with the un-jumped top-of-list page.
      const targetKey = ["events", caseId, timelineId, {}, sortDir];
      await queryClient.cancelQueries({ queryKey: targetKey });

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

      // A newer jump started while this one was in flight — let it win.
      if (jumpSeqRef.current !== seq) return;

      // The no-eventId branch fetches in `before` mode, and before-mode
      // pagination only ever computes `has_more_before` on the backend —
      // `has_more_after` comes back false even when more events follow.
      // Recording `before` in this page's own pageParam lets
      // `getNextPageParam` know its `has_more_after` can't be trusted and
      // synthesize the forward cursor instead of reporting "all loaded".
      const anchorPageParam: EventsPageParam = eventId
        ? {}
        : { before: cursorParam(anchorPage.prev_cursor) };
      queryClient.setQueryData(targetKey, {
        pages: [anchorPage],
        pageParams: [anchorPageParam],
      });
      seededSeqRef.current = seq;
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
   * existing jump-to-time machinery; anything else becomes `filters.q`. The
   * rail's mode control decides how `q` is interpreted: keyword (broadened
   * all-fields server-side search, optionally regex) or semantic (embedding
   * search narrowing the grid via the `semanticSearchIds` override).
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
    if (!semanticMode) {
      // Surface a server-side regex rejection (400) right under the box.
      if (filters.qRegex && eventsError && eventsQueryError instanceof Error
          && eventsQueryError.message.includes("invalid regular expression")) {
        return eventsQueryError.message;
      }
      return filters.qRegex ? "regex search" : undefined;
    }
    if (semanticSearchPending) return undefined; // spinner already shown
    if (semanticSearchData?.status === "ok") {
      return `${semanticSearchData.results.length} semantic match${semanticSearchData.results.length === 1 ? "" : "es"}`;
    }
    if (semanticSearchData?.status === "not_embedded") {
      return "not embedded — showing keyword matches instead";
    }
    if (!hasVectors) return "no embeddings — showing keyword matches instead";
    return undefined;
  }, [
    searchError,
    filters.q,
    filters.qRegex,
    semanticMode,
    hasVectors,
    semanticSearchPending,
    semanticSearchData,
    eventsError,
    eventsQueryError,
  ]);

  // Once the jump target's anchor page has landed in `events`, scroll the
  // grid to it, open its detail panel (so the target is unmistakable — the
  // detail panel's own "expanded" row styling doubles as the highlight),
  // and clear the pending marker. Findings without a specific event (e.g. a
  // Frequency window) have nothing to expand — the range highlight already
  // marks the window instead.
  useEffect(() => {
    const pending = pendingJumpRef.current;
    if (!pending) return;
    // `events` can change for reasons unrelated to this jump — e.g. a
    // still-in-flight automatic fetch resolving, or annotation refetches —
    // so only treat it as "ready" once we know it reflects the page we
    // ourselves seeded for this specific jump.
    if (seededSeqRef.current !== pending.seq) return;
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

  // Once the soft-anchor page seeded in setFilters lands in `events`, scroll
  // the grid back to where the analyst was — otherwise the grid renders at
  // whatever page landed by default and reads as "the view jumped".
  useEffect(() => {
    const pending = pendingSoftAnchorRef.current;
    if (!pending) return;
    if (softAnchorSeededSeqRef.current !== pending.seq) return;
    if (events.length === 0) return;
    gridRef.current?.scrollToTimestamp(pending.ts);
    pendingSoftAnchorRef.current = null;
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
          fieldSuggestions={fieldSuggestions}
          hasVectors={hasVectors}
          caseId={caseId!}
          timelineId={timelineId!}
          onSearchSubmit={handleSearchSubmit}
          searchStatus={searchStatus}
          searchPending={semanticMode && !!filters.q && semanticSearchPending}
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

            <Tooltip content="Open the full visualization page">
              <Button variant="outline" size="sm" asChild>
                <Link to={`visualize?${searchParams.toString()}`}>
                  <AreaChart size={13} />
                  Visualize
                </Link>
              </Button>
            </Tooltip>

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

        {/* Sources still ingesting — excluded from results until ready */}
        {ingestingSources.length > 0 && (
          <div className="flex shrink-0 items-center gap-2 bg-[var(--color-accent-dim)] px-3 py-1 text-xs text-[var(--color-fg-primary)]">
            <Spinner size={11} />
            <span>
              {ingestingSources.length === 1
                ? `Source "${ingestingSources[0].name}" is still ingesting`
                : `${ingestingSources.length} sources are still ingesting`}{" "}
              — excluded from results until complete.
            </span>
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
                  onVisibleTimestampChange={setCurrentPositionTs}
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
                    onShowFieldHistogram={handleShowFieldHistogram}
                    onJumpToTime={handleJumpToTime}
                    tagSuggestions={tagSuggestions}
                  />
                )}

                {fieldHistogram && caseId && timelineId && (
                  <FieldHistogramModal
                    open
                    onOpenChange={(o) => !o && setFieldHistogram(null)}
                    caseId={caseId}
                    timelineId={timelineId}
                    filters={effectiveFilters}
                    fieldKey={fieldHistogram.fieldKey}
                    value={fieldHistogram.value}
                    onAddFilter={handleAddFilter}
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
                    onAnomalyRunId={setAnomalyRunId}
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
                // effectiveFilters (not raw filters) so "select all matching"
                // annotates exactly the displayed result set: in semantic mode
                // it carries the result `ids` (raw filters would fall back to
                // the broadened keyword `q`), and in anomaly mode the run_id
                // narrowing — otherwise mode="all" writes to a wider set than
                // what the grid shows.
                filters={effectiveFilters}
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
