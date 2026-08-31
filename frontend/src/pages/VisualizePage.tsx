/**
 * VisualizePage — full statistical visualization workbench.
 *
 * Inherits the Explorer's current filters/time-range from the URL (same
 * `paramsToFilters` the Explorer itself reads), so a chart here always
 * matches whatever the analyst was just looking at in the grid. That
 * inheritance is stated on the canvas by `viz/InheritedFiltersBar` — the
 * scope of an exported figure has to be legible before the chart is read,
 * not only in the caption underneath it.
 *
 * The rail reads in dependency order: scale of measurement, then the chart
 * types that scale allows, then the field that chart charts — each backed by
 * one of the `vizApi` aggregations. It used to read field-first, which stated
 * the dependency backwards: the default time histogram charts no field, so the
 * topmost control was inert until the analyst changed a dropdown *below* it
 * (#298). Nothing in the rail goes quietly dead now — a control that cannot
 * apply says why and offers the way out, the contract Compare already kept —
 * and every re-pick the rail makes on the analyst's behalf (the scale radio
 * clamping an illegal chart type, the field probes choosing a scale) names
 * itself in `autoNotice` rather than moving a control nobody touched.
 *
 * All chart state (type, field, scale, metric, comparison layer, options)
 * lives in the URL as a serialized `ChartConfig` (`c_*` params, see
 * `viz/lib/chartConfig.ts`) alongside the filter params — a Visualize URL is
 * a complete, shareable description of the chart.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { Repeat } from "lucide-react";
import { savedChartsApi, vizApi, type CompareMode } from "@/api/viz";
import { eventsApi } from "@/api/events";
import { timelinesApi } from "@/api/timelines";
import { dispositionsApi } from "@/api/dispositions";
import {
  FILTER_PARAM_KEYS,
  filtersToParams,
  paramsToFilters,
} from "@/lib/queryParams";
import { busyMessage, busyRetry } from "@/lib/queryClient";
import { InheritedFiltersBar } from "@/components/viz/InheritedFiltersBar";
import {
  resolveCollapseRoutine,
  routineSignature,
  type RoutineOverride,
} from "@/lib/routineCollapse";
import { applyFieldEntries, removeFilterEntry } from "@/lib/fieldFilters";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import { ChartActionPopover } from "@/components/viz/ChartActionPopover";
import type { ChartValueClick } from "@/components/viz/lib/interaction";
import { BarChart } from "@/components/viz/charts/BarChart";
import { PieChart } from "@/components/viz/charts/PieChart";
import { WaffleChart } from "@/components/viz/charts/WaffleChart";
import { NumericHistogram } from "@/components/viz/charts/NumericHistogram";
import { BoxPlot } from "@/components/viz/charts/BoxPlot";
import { ViolinPlot } from "@/components/viz/charts/ViolinPlot";
import { GroupedDistribution } from "@/components/viz/charts/GroupedDistribution";
import { LineChart } from "@/components/viz/charts/LineChart";
import { Heatmap } from "@/components/viz/charts/Heatmap";
import { EcdfChart } from "@/components/viz/charts/EcdfChart";
import { CompareHistogram } from "@/components/viz/charts/CompareHistogram";
import { PunchCard } from "@/components/viz/charts/PunchCard";
import { CumulativeStep } from "@/components/viz/charts/CumulativeStep";
import { CalendarHeatmap } from "@/components/viz/charts/CalendarHeatmap";
import { RankedChange } from "@/components/viz/charts/RankedChange";
import { IntervalLanes } from "@/components/viz/charts/IntervalLanes";
import { PivotHeatmap } from "@/components/viz/charts/PivotHeatmap";
import { SankeyFlow } from "@/components/viz/charts/SankeyFlow";
import { ScatterChart } from "@/components/viz/charts/ScatterChart";
import { TableFigure } from "@/components/viz/charts/TableFigure";
import { tableCsv } from "@/components/viz/lib/tableRows";
import {
  CorrMatrix,
  type CorrMethod,
} from "@/components/viz/charts/CorrMatrix";
import {
  CHART_ID_PARAM,
  chartUrlParams,
  histogramToCompare,
  normalizeChartConfig,
  paramsToChartConfig,
  parseStoredChartConfig,
  parseStoredChartFilters,
  unrepresentableFilterMembers,
  type ChartConfig,
  type ChartType,
  type Scale,
} from "@/components/viz/lib/chartConfig";
import { METRIC_INFO, type Metric } from "@/components/viz/lib/transforms";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import {
  resolveChartOptions,
  defaultChartTypeForScale,
} from "@/components/viz/lib/chartOptions";
import { fieldTokenLabel } from "@/components/viz/lib/fieldDisplay";
import { isTimeField, TIME_FIELDS } from "@/components/viz/lib/timeFields";
import {
  cumulativeChartDomain,
  lanesChartDomain,
  timeChartDomain,
  timeseriesChartDomain,
} from "@/components/viz/lib/timeDomain";
import {
  buildCaptionLines,
  type CaptionFacts,
} from "@/components/viz/lib/caption";
import { ChartRail } from "@/components/viz/ChartRail";
import { useResolvedMarks } from "@/components/viz/useResolvedMarks";
import {
  SCALE_DISPLAY,
  treatAsNotice,
} from "@/components/viz/lib/scaleDisplay";
import { pieReadabilityWarning } from "@/components/viz/lib/pieReadability";
import { barReadabilityWarning } from "@/components/viz/lib/barReadability";
import { ChartCaption } from "@/components/viz/primitives/ChartCaption";
import { ExplainerPopover } from "@/components/viz/primitives/ExplainerPopover";
import { NumericStatStrip } from "@/components/viz/NumericStatStrip";
import { ScatterStatsPanel } from "@/components/viz/ScatterStatsPanel";
import type {
  ChangeResponse,
  LanesResponse,
  CompareNumericResponse,
  CompareTermsResponse,
  CompareTimeResponse,
  EventFilters,
} from "@/api/types";

export function VisualizePage() {
  const { caseId, timelineId } = useParams<{
    caseId: string;
    timelineId: string;
  }>();
  const [searchParams, setSearchParams] = useSearchParams();

  // A saved chart can be addressed by id instead of spelled out as `c_*`
  // params, and when it is, storage — not the URL — is the source of both
  // halves. That is the whole point: three members of the filter set
  // (`ids`, `anomalyRunId`, `collapseRoutine`) have no URL representation, so
  // a link that reconstructed the chart from params would quietly widen an
  // agent-scoped chart to the whole timeline. `?c_chart=<id>` cannot.
  const chartId = searchParams.get(CHART_ID_PARAM);
  const savedChartsQuery = useQuery({
    // Same key SavedChartsRail uses, so the two share one fetch.
    queryKey: ["viz-saved-charts", caseId, timelineId],
    queryFn: () => savedChartsApi.list(caseId!, timelineId!),
    enabled: !!(caseId && timelineId && chartId),
  });
  const savedChart = useMemo(
    () =>
      chartId
        ? savedChartsQuery.data?.charts.find((c) => c.id === chartId)
        : undefined,
    [chartId, savedChartsQuery.data],
  );
  const storedConfig = useMemo(
    () => (savedChart ? parseStoredChartConfig(savedChart.config) : null),
    [savedChart],
  );
  // Whether the reference has been resolved as far as it ever will be. An
  // *error* settles it too: a chart list that could not be fetched is not a
  // chart that is still arriving, and treating it as one would suspend the
  // page indefinitely — every defaulting effect below suppressed and
  // `scopeReady` false, so no chart renders, no notice appears, and there is
  // nothing on screen to explain either. Falling through to the params draws
  // the default chart, which is the same graceful degradation a deleted chart
  // already got.
  const chartRefSettled =
    savedChartsQuery.isSuccess || savedChartsQuery.isError;
  // A `c_chart` that names a deleted chart, one saved by an incompatible
  // config version, or one whose list could not be loaded at all falls through
  // to the params — the page still works, and the notice below says why it is
  // not the chart that was linked.
  const chartRefBroken =
    !!chartId &&
    chartRefSettled &&
    (savedChart === undefined || storedConfig === null);
  // While the URL names a chart, *nothing writes the URL automatically* — not
  // while the reference is resolving (where `config` below is still the
  // default chart) and not after it has, where the stored chart already
  // answered every question the defaulting effects exist to answer. Both
  // windows are the same rule, because both would end the same way: any
  // automatic write goes through `takeOver`, which spells the chart out as
  // `c_*` params and drops `c_chart` along with the three filter members
  // params cannot carry. Only the analyst's own edit may do that.
  //
  // A *broken* reference is deliberately not live: a link to a deleted or
  // unreadable chart falls through to the params, where the page is building
  // a chart again and the defaults are wanted.
  const chartRefLive = !!chartId && !chartRefBroken;

  // Why the last `c_chart` could not be honoured. Latched in state for the
  // same reason `droppedScope` is: the moment the reference settles as broken
  // the defaulting effects start writing the default chart into the URL, and
  // `chartConfigToParams` drops `c_chart` along with the rest of the namespace
  // — so the *derived* answer stops being true one tick after it becomes true,
  // and the notice explaining the page would blink out with it. An analyst who
  // clicked a link to a chart is owed the reason it is not on screen for
  // longer than a frame.
  const [brokenChartRef, setBrokenChartRef] = useState<
    "unfetchable" | "unreadable" | "missing" | null
  >(null);
  useEffect(() => {
    if (!chartRefBroken) return;
    setBrokenChartRef(
      savedChartsQuery.isError
        ? "unfetchable"
        : savedChart
          ? "unreadable"
          : "missing",
    );
  }, [chartRefBroken, savedChartsQuery.isError, savedChart]);

  const urlFilters = useMemo(() => {
    if (!(storedConfig && savedChart)) return paramsToFilters(searchParams);
    // Routine collapse is *live* state on this page, never stored state: the
    // disposition set is the single source of truth (#147) and `filters` below
    // re-derives it from scratch. A stored `true` riding along here would
    // survive the reveal toggle — which only ever *adds* the flag, so it could
    // never turn one off — and would be reported by `takeOver` as a narrowing
    // the URL dropped, when the URL never carried it and the page re-derives
    // it either way. The frozen renderers (`ChartBlockCard`, the export
    // resolver) do read it, which is the whole reason it is stored.
    const { collapseRoutine: _live, ...stored } = parseStoredChartFilters(
      savedChart.config,
    );
    return stored;
  }, [storedConfig, savedChart, searchParams]);
  const config = useMemo(
    () => storedConfig ?? paramsToChartConfig(searchParams),
    [storedConfig, searchParams],
  );

  // Routine collapse, derived exactly as on ExplorerPage (#147): a mute is a
  // filter and the charts must aggregate the set the grid displays.
  // `collapseRoutine` is deliberately never URL-serialized, so it cannot
  // arrive via `paramsToFilters` — the disposition set in Postgres is the
  // single source of truth, and deriving from it means a shared URL shows a
  // teammate the same collapsed charts. lib/routineCollapse.ts owns the
  // precedence and why the reveal override self-expires.
  const dispositionsQuery = useQuery({
    queryKey: ["dispositions", caseId, timelineId],
    queryFn: () => dispositionsApi.list(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });
  const routineSig = useMemo(
    () => routineSignature(dispositionsQuery.data?.dispositions ?? []),
    [dispositionsQuery.data],
  );
  const hasRoutineDispositions = routineSig !== "";
  const [routineOverride, setRoutineOverride] = useState<RoutineOverride>(null);
  const collapseRoutine = resolveCollapseRoutine(routineSig, routineOverride);
  // Every chart query waits for the disposition set: an uncollapsed first
  // fetch would render (then refetch and recompute) the muted superset on
  // every page load with mutes — the #147 flash, one page over. One small
  // Postgres query before first paint, usually already warm from Explorer.
  // Same argument one step further for `c_chart`: fetching the default chart's
  // data and then the linked chart's would render the wrong chart first.
  // Settled, not succeeded: a failed chart-list fetch has already fallen back
  // to the params above, and there is nothing left to wait for.
  const scopeReady =
    !!(caseId && timelineId) &&
    dispositionsQuery.isSuccess &&
    (!chartId || chartRefSettled);
  const filters = useMemo(
    () =>
      collapseRoutine ? { ...urlFilters, collapseRoutine: true } : urlFilters,
    [urlFilters, collapseRoutine],
  );

  // Narrowings the URL cannot carry that the analyst's last edit therefore
  // dropped. Held in state rather than derived, because after `takeOver` the
  // params are all that is left — the evidence of what was lost exists only at
  // the moment it is lost.
  const [droppedScope, setDroppedScope] = useState<string[] | null>(null);

  // Editing either half is the analyst taking the chart over: the URL stops
  // naming a saved chart and starts describing this one, spelled out in full.
  // Both halves are written every time, because after this the params are the
  // only record — `chartConfigToParams` drops `c_chart` along with the rest of
  // the `c_*` keys it rewrites, so there is no separate step to forget.
  //
  // Three filter members have no param form, so spelling the chart out *widens*
  // it whenever they were set. That is unavoidable — the URL is the record now —
  // but it must not be silent: a chart scoped to a fixed event set becoming a
  // chart over the whole timeline is exactly the failure `?c_chart=` exists to
  // prevent, and an analyst who is not told reads the wider chart as the one
  // they opened.
  //
  // In practice only two of the three ever reach here: `collapseRoutine` is
  // stripped from `urlFilters` above because this page re-derives it from live
  // dispositions, so a take-over does not lose it and must not claim to. The
  // third stays in `unrepresentableFilterMembers` regardless — the rail saves
  // the *resolved* filters, where the flag is real.
  //
  // `chartUrlParams` rewrites both namespaces and carries everything else in
  // the URL over untouched — this page owns `c_*` and the filter params, not
  // the whole query string.
  const takeOver = useCallback(
    (nextConfig: ChartConfig, nextFilters: EventFilters) => {
      const dropped = unrepresentableFilterMembers(nextFilters);
      setDroppedScope(dropped.length > 0 ? dropped : null);
      setSearchParams((prev) => chartUrlParams(nextConfig, nextFilters, prev), {
        replace: true,
      });
    },
    [setSearchParams],
  );

  // Normalized on the way *out*, not only on the way back in: an edit that
  // makes a stored knob illegal (changing Treat-as under a running sum,
  // picking "No field") would otherwise write it into the URL, and the URL is
  // what a shared link, a take-over and a save all read. Dropping it here
  // means the config the analyst edits, the one in the address bar and the
  // one that gets stored are the same chart. See `normalizeChartConfig`.
  const updateConfig = useCallback(
    (patch: Partial<ChartConfig>) =>
      takeOver(normalizeChartConfig({ ...config, ...patch }), urlFilters),
    [takeOver, config, urlFilters],
  );

  // The one place a filter change is written from this page.
  const updateFilters = useCallback(
    (next: EventFilters) => takeOver(config, next),
    [takeOver, config],
  );

  /** A confirmed scenario: the figure and its suggested filter in one
   * take-over, so the chart never renders for a moment under the old filters.
   * The filter is merged per field rather than replacing the set — a scenario
   * narrows what the analyst already had; it does not discard it. */
  const applyScenario = useCallback(
    (patch: Partial<ChartConfig>, scenarioFilters: EventFilters | null) => {
      const next: EventFilters = scenarioFilters
        ? {
            ...urlFilters,
            ...scenarioFilters,
            filters: { ...urlFilters.filters, ...scenarioFilters.filters },
            filterModes: { ...urlFilters.filterModes, ...scenarioFilters.filterModes },
          }
        : urlFilters;
      takeOver(normalizeChartConfig({ ...config, ...patch }), next);
    },
    [takeOver, config, urlFilters],
  );

  // Loading a saved chart addresses it by id and lets the resolution above
  // read both halves out of storage. Writing its `c_*` params instead would
  // lose exactly the members storage exists to carry.
  const loadSavedChart = useCallback(
    (loadedChartId: string) => {
      // The URL names a chart again, so whatever a previous take-over dropped
      // — and whatever a previous reference failed to resolve — is no longer
      // what is on screen.
      setDroppedScope(null);
      setBrokenChartRef(null);
      // Clears both namespaces this page owns — the stored chart supplies the
      // shape *and* the filters, so a leftover filter param would narrow it
      // further than the analyst who saved it ever saw. Anything outside those
      // two namespaces is not ours to drop.
      setSearchParams(
        (prev) => {
          const params = new URLSearchParams();
          for (const [key, value] of prev.entries()) {
            if (key.startsWith("c_") || FILTER_PARAM_KEYS.has(key)) continue;
            params.append(key, value);
          }
          params.set(CHART_ID_PARAM, loadedChartId);
          return params;
        },
        { replace: true },
      );
    },
    [setSearchParams],
  );

  // Round trip back to the grid these filters came from. Only the filter params
  // travel — the `c_*` chart config means nothing to the Explorer.
  const explorerHref = useMemo(
    () =>
      `/cases/${caseId}/timelines/${timelineId}?${filtersToParams(urlFilters).toString()}`,
    [caseId, timelineId, urlFilters],
  );

  // Click-to-filter: charts report the clicked mark's field=value pair(s);
  // the popover offers filter in / filter out / open in Explorer.
  const [pendingClick, setPendingClick] = useState<ChartValueClick | null>(
    null,
  );
  const handleChartValueClick = useCallback((click: ChartValueClick) => {
    setPendingClick(click);
  }, []);

  const { field, fieldY, scale, chartType, metric } = config;
  const dataKind = CHART_META[chartType].dataKind;
  const requiresSecondField = !!CHART_META[chartType].requiresSecondField;
  // box/violin take an OPTIONAL second field: a categorical grouping
  // variable turning one distribution into one per group.
  const acceptsSecondField = !!CHART_META[chartType].acceptsSecondField;
  // The correlation matrix charts a LIST of fields instead of field/fieldY.
  const multiField = !!CHART_META[chartType].multiField;
  const selectedFields = config.fields ?? [];
  // A modifier on the *numeric* kind only. `table` also declares an optional
  // second field, but it counts that field's distinct values in a column —
  // it has its own endpoint, and `field-numeric-grouped` on a categorical X is
  // a heavy scan whose result nothing on the page ever reads.
  const groupedOn = dataKind === "numeric" && acceptsSecondField && !!fieldY;
  // "time" and "punchcard" chart the whole event count — no field involved.
  const fieldFree = dataKind === "time" || dataKind === "punchcard";
  // "cumulative" and "calendar" take an optional field: every event without
  // one, the field's values with one. Probed like a bar when a field is set.
  const fieldOptional = CHART_META[chartType].inputs.field === "optional";
  const compareOn = config.compare.mode !== "off";
  const lanesPairing = config.inputs.pairing ?? "firstLast";
  const lanesReady =
    lanesPairing === "firstLast" || (!!config.inputs.startFilter && !!config.inputs.endFilter);
  const compareApiSpec: CompareMode | null =
    config.compare.mode === "baseline"
      ? { mode: "baseline" }
      : config.compare.mode === "custom"
        ? { mode: "custom", filters: config.compare.filters }
        : null;

  // Shared with the agent's ChartProposalCard so a proposed chart and a
  // hand-built one resolve their defaults identically.
  const resolved = useMemo(() => resolveChartOptions(config), [config]);
  const {
    topN,
    bins,
    buckets,
    quantity,
    layout,
    limitX,
    limitY,
    sampleLimit,
    groups,
    showPoints,
  } = resolved;

  const svgRef = useRef<SVGSVGElement | null>(null);
  // Which coefficient fills the matrix cells. Purely a client-side read of
  // the same response — both coefficients always ship, so switching never
  // refetches (same reasoning as pivot↔sankey).
  const [corrMethod, setCorrMethod] = useState<CorrMethod>("pearson");
  /** What the rail changed on the analyst's behalf, and why (#298).
   *
   * The scale radio re-picks the chart type, and the field probes reassign both
   * — correct in every case, and until now wordless: controls the analyst never
   * touched moved while they watched. This holds the last such change and is
   * cleared by the next explicit pick, since their own choice needs no excuse. */
  const [autoNotice, setAutoNotice] = useState<string | null>(null);
  const timelineQuery = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  const marksQuery = useResolvedMarks(caseId, timelineId, config);

  const fieldsQuery = useQuery({
    queryKey: ["viz-fields", caseId, timelineId],
    queryFn: () => vizApi.fields(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  // Default to the first field once the list loads — the backend sorts by
  // coverage descending, so this is the highest-coverage field.
  //
  // Never for a field-optional figure. Fieldless is a legal, meaningful state
  // for Cumulative and Calendar — "count every event", which is what
  // `resolveChartOptions` clamps `quantity` to — and the rail renders it as
  // its own selection (`NO_FIELD`). Defaulting here reverted that pick on the
  // very next render, and not cosmetically: with a field set, `/viz/calendar`
  // counts only events whose field is non-empty and the quantity flips back to
  // sum/distinct, so the analyst gets a filtered chart they did not ask for.
  //
  // And only *once*. `field == null` is two different states — nobody has
  // chosen yet, and the analyst chose "No field — count every event" — and
  // this effect cannot tell them apart, because the fresh-load default figure
  // is itself field-free (`time`). Without the latch, clearing the field on a
  // bar chart lands on `time` and the highest-coverage field is written
  // straight back into the URL: invisible in the combo, which renders
  // `NO_FIELD` for a field-free figure, until the next gallery pick charts a
  // field nobody chose.
  const fieldDefaulted = useRef(false);
  useEffect(() => {
    if (chartRefLive || fieldOptional) return;
    if (field != null) {
      fieldDefaulted.current = true;
      return;
    }
    if (fieldDefaulted.current) return;
    if (fieldsQuery.data?.fields.length) {
      fieldDefaulted.current = true;
      updateConfig({ field: fieldsQuery.data.fields[0].token });
    }
  }, [field, fieldsQuery.data, updateConfig, chartRefLive, fieldOptional]);

  // Probe numeric-ness only when actually needed: once per field change (to
  // auto-suggest a scale) and while a numeric chart type is displayed (as its
  // data source). `autoProbedField` gates the auto-suggest to once per field
  // — the analyst's manual scale choice is never overridden afterwards.
  const autoProbedField = useRef<string | null>(field);
  // A virtual `time:` field's scale is known statically, and its SQL yields
  // zero-padded strings — `field_numeric_stats` would scan the timeline only
  // to report `count: 0` and land the analyst on nominal/bar, contradicting
  // TIME_FIELDS. Never probe one.
  const fieldIsTime = field != null && isTimeField(field);
  // A pairing the rail never offers but a saved chart or URL can still carry:
  // a numeric-fed mark over a `time:` field, whose SQL yields strings. Tested
  // on the data kind rather than on `chartTypesForField`, so a URL with an
  // inconsistent scale/chartType pair still falls through to its own handling
  // instead of collecting this (wrong) explanation.
  const chartTypeUnplottable =
    fieldIsTime && (dataKind === "numeric" || dataKind === "scatter");
  const numericQuery = useQuery({
    queryKey: [
      "viz-field-numeric",
      caseId,
      timelineId,
      field,
      filters,
      bins,
      showPoints,
    ],
    queryFn: () =>
      vizApi.fieldNumeric(
        caseId!,
        timelineId!,
        field!,
        filters,
        bins,
        showPoints,
      ),
    // Run only when a numeric chart actually needs the data, or when a
    // *field-dependent* chart needs its one-time scale probe. The field-free
    // charts (time, punchcard) never need it, and the two-field charts have
    // their own endpoints and keep their chart type — skipping the probe
    // there avoids the field_numeric_stats double-scan.
    //
    // `!fieldIsTime` is a top-level conjunct, not part of the probe disjunct:
    // gating only the probe would still fire the scan whenever a numeric
    // chart type happened to be selected.
    enabled:
      scopeReady &&
      !!field &&
      !fieldIsTime &&
      !multiField &&
      !groupedOn &&
      (dataKind === "numeric" ||
        (!chartRefLive &&
          !fieldFree &&
          !requiresSecondField &&
          // The consuming effect returns early for the table (the analyst
          // chose it; a numeric probe must not re-pick the figure under
          // them), so the scan's answer would be discarded on arrival.
          chartType !== "table" &&
          field !== autoProbedField.current)),
    ...busyRetry,
  });

  // A named chart's field arrives *after* mount, so it is never the field the
  // ref was initialized with — without this, resolving the reference looks
  // exactly like the analyst picking a new field and spends the one-shot
  // suggestion on a chart that already has its own answer. Declared before the
  // two suggestion effects below, which React therefore runs after it.
  useEffect(() => {
    if (chartRefLive && field) autoProbedField.current = field;
  }, [chartRefLive, field]);

  // Scale suggestion for a virtual time field — the statically-known answer,
  // no round-trip. Must run before the numeric-probe effect below so the
  // shared `autoProbedField` ref is spent first; React runs effects in
  // declaration order.
  useEffect(() => {
    if (chartRefLive) return;
    if (!field || !fieldIsTime || field === autoProbedField.current) return;
    // Advance the ref even when the early-return below fires: it means "this
    // field's one-shot suggestion is spent", not "we fetched something".
    autoProbedField.current = field;
    if (fieldFree || requiresSecondField || multiField) return;
    // `nextScale`, not `scale`: the outer `scale` is the config's current one,
    // which the guard below compares against — as the numeric probe does.
    const nextScale = TIME_FIELDS[field].scale;
    const label = fieldTokenLabel(field);
    // The same rule the numeric probe below states and this effect used to
    // ignore: the figures that take a field as a lane key, a ranked value, a
    // running quantity or a table were chosen *before* the field, so a scale
    // suggestion may move the treat-as where the figure admits it but never
    // re-picks the figure. Without this, `?c_type=cumulative` plus a pick of
    // `time:hour_of_day` silently became a bar chart.
    if (
      fieldOptional ||
      dataKind === "lanes" ||
      dataKind === "change" ||
      chartType === "table"
    ) {
      if (nextScale === scale) return;
      if (!CHART_META[chartType].scales.includes(nextScale)) {
        setAutoNotice(
          `${label} is a time field — ${CHART_META[chartType].label} charts it as ${SCALE_DISPLAY[scale].label.toLowerCase()}.`,
        );
        return;
      }
      updateConfig({ scale: nextScale });
      setAutoNotice(
        `${label} is a time field — treating it as ${SCALE_DISPLAY[nextScale].label.toLowerCase()}.`,
      );
      return;
    }
    const nextType = defaultChartTypeForScale(nextScale, field);
    updateConfig({ scale: nextScale, chartType: nextType });
    setAutoNotice(
      `${label} is a time field — treating it as ${SCALE_DISPLAY[nextScale].label.toLowerCase()}; figure set to ${CHART_META[nextType].label}.`,
    );
  }, [
    field,
    fieldIsTime,
    fieldFree,
    fieldOptional,
    dataKind,
    chartType,
    scale,
    requiresSecondField,
    multiField,
    updateConfig,
    chartRefLive,
  ]);

  useEffect(() => {
    if (chartRefLive) return;
    // A derived chart is the analyst's explicit answer to the question this
    // probe asks (URL / saved chart): re-picking ratio + histogram here would
    // silently drop the derivation.
    if (config.derive) return;
    if (!field || field === autoProbedField.current) return;
    // Inert for time fields anyway (the query is disabled, so `data` stays
    // undefined) — stated explicitly so the intent survives a refactor.
    if (fieldIsTime) return;
    if (numericQuery.data == null) return;
    autoProbedField.current = field;
    // Don't yank the analyst off the field-independent charts (time,
    // punchcard) or a deliberately-picked two-field chart.
    // The table is legal at nominal and ordinal only, and the analyst chose a
    // table: a numeric probe must not re-pick the figure out from under it.
    if (fieldFree || requiresSecondField || multiField || chartType === "table")
      return;
    const isNumeric = numericQuery.data.count > 0;
    const nextScale: Scale = isNumeric ? "ratio" : "nominal";
    const label = fieldTokenLabel(field);
    // The figures that take a field as a lane key, a ranked value, or a
    // running quantity (lanes, change, cumulative, calendar) were chosen
    // *before* the field: the probe may move the treat-as where the figure
    // admits it — a cumulative sum over a measure — but never the figure.
    if (fieldOptional || dataKind === "lanes" || dataKind === "change") {
      if (nextScale === scale) return;
      if (!CHART_META[chartType].scales.includes(nextScale)) {
        if (isNumeric)
          setAutoNotice(
            `${label} looks numeric — ${CHART_META[chartType].label} charts it as ${SCALE_DISPLAY[scale].label.toLowerCase()}.`,
          );
        return;
      }
      updateConfig({ scale: nextScale });
      setAutoNotice(treatAsNotice(label, nextScale, isNumeric));
      return;
    }
    const nextType: ChartType = isNumeric ? "histogram" : "bar";
    // Say exactly which of the two controls moved, and say nothing when
    // neither did. The non-numeric branch used to re-pick *both* silently —
    // a Histogram on a ratio scale became a Bar on a nominal one with no word
    // about it — and then set the notice to `null`, which also wiped the
    // "Group by cleared" line the analyst's own edit had just put there a few
    // hundred milliseconds earlier. Landing on the scale and type the chart
    // already has is not a change and must not claim to be one.
    if (nextScale === scale && nextType === chartType) return;
    updateConfig({ scale: nextScale, chartType: nextType });
    let notice =
      nextScale !== scale
        ? treatAsNotice(label, nextScale, isNumeric)
        : `${label} ${isNumeric ? "looks numeric" : "has no numeric values"}.`;
    if (nextType !== chartType) {
      notice = `${notice.replace(/\.$/, "")}; figure set to ${CHART_META[nextType].label}.`;
    }
    setAutoNotice(notice);
  }, [
    field,
    fieldIsTime,
    numericQuery.data,
    fieldFree,
    fieldOptional,
    dataKind,
    requiresSecondField,
    multiField,
    scale,
    chartType,
    updateConfig,
    chartRefLive,
  ]);

  // Metric gating: % of baseline needs a comparison layer; delta/rate/
  // cumulative need time-bucketed bins. Clamp the active metric the same way
  // so a chart-type/compare change never leaves an impossible combination.
  const metricAvailable = useCallback(
    (m: Metric): boolean => {
      const info = METRIC_INFO[m];
      if (info.requiresCompare && !compareOn) return false;
      if (info.timeBucketedOnly && dataKind !== "time") return false;
      return m === "count" || dataKind === "time";
    },
    [compareOn, dataKind],
  );
  useEffect(() => {
    if (chartRefLive) return;
    if (!metricAvailable(metric)) updateConfig({ metric: "count" });
  }, [metric, metricAvailable, updateConfig, chartRefLive]);

  const compareTermsOn =
    compareOn && chartType === "bar" && compareApiSpec != null;
  const termsQuery = useQuery({
    queryKey: [
      "viz-field-terms",
      caseId,
      timelineId,
      field,
      filters,
      topN,
      config.derive,
    ],
    queryFn: () =>
      vizApi.fieldTerms(caseId!, timelineId!, field!, filters, topN, {
        derive: config.derive,
      }),
    enabled: scopeReady && !!field && dataKind === "terms" && !compareTermsOn,
    ...busyRetry,
  });

  const compareTermsQuery = useQuery({
    queryKey: [
      "viz-compare-terms",
      caseId,
      timelineId,
      field,
      filters,
      config.compare,
      topN,
      config.derive,
    ],
    queryFn: async () =>
      (await vizApi.compare(caseId!, timelineId!, {
        kind: "terms",
        field: field!,
        primary: filters,
        comparison: compareApiSpec!,
        limit: topN,
        derive: config.derive,
      })) as CompareTermsResponse,
    enabled: scopeReady && !!field && compareTermsOn,
    ...busyRetry,
  });

  const compareNumericOn =
    compareOn && chartType === "histogram" && compareApiSpec != null;
  const compareNumericQuery = useQuery({
    queryKey: [
      "viz-compare-numeric",
      caseId,
      timelineId,
      field,
      filters,
      config.compare,
      bins,
    ],
    queryFn: async () =>
      (await vizApi.compare(caseId!, timelineId!, {
        kind: "numeric",
        field: field!,
        primary: filters,
        comparison: compareApiSpec!,
        // The comparison aggregation negotiates shared bin edges between the
        // two layers and has no auto path — fall back to the manual default.
        bins: bins ?? 30,
      })) as CompareNumericResponse,
    enabled: scopeReady && !!field && compareNumericOn,
    ...busyRetry,
  });

  // Grouped box/violin: one distribution per top-N value of the grouping
  // field, all binned over the same global range (server-side) so the
  // silhouettes are comparable.
  const groupedQuery = useQuery({
    queryKey: [
      "viz-field-numeric-grouped",
      caseId,
      timelineId,
      field,
      fieldY,
      filters,
      groups,
      bins,
      showPoints,
    ],
    queryFn: () =>
      vizApi.fieldNumericGrouped(
        caseId!,
        timelineId!,
        field!,
        fieldY!,
        filters,
        groups,
        bins ?? 30,
        showPoints,
      ),
    enabled: scopeReady && !!field && !!fieldY && groupedOn,
    ...busyRetry,
  });

  const correlationQuery = useQuery({
    queryKey: [
      "viz-field-correlation",
      caseId,
      timelineId,
      selectedFields,
      filters,
    ],
    queryFn: () =>
      vizApi.fieldCorrelation(caseId!, timelineId!, selectedFields, filters),
    enabled: scopeReady && multiField && selectedFields.length >= 2,
    ...busyRetry,
  });

  const timeseriesQuery = useQuery({
    queryKey: [
      "viz-field-timeseries",
      caseId,
      timelineId,
      field,
      filters,
      buckets,
      topN,
      config.derive,
    ],
    queryFn: () =>
      vizApi.fieldTimeseries(
        caseId!,
        timelineId!,
        field!,
        filters,
        buckets,
        topN,
        config.derive,
      ),
    enabled: scopeReady && !!field && dataKind === "timeseries",
    ...busyRetry,
  });

  // Events-over-time: one shared-grid compare call when a comparison layer
  // is on, otherwise the Explorer's own histogram adapted to the same shape.
  const timeQuery = useQuery({
    queryKey: [
      "viz-time",
      caseId,
      timelineId,
      filters,
      config.compare,
      buckets,
    ],
    queryFn: async (): Promise<CompareTimeResponse> => {
      if (compareApiSpec) {
        return (await vizApi.compare(caseId!, timelineId!, {
          kind: "time",
          primary: filters,
          comparison: compareApiSpec,
          buckets,
        })) as CompareTimeResponse;
      }
      return histogramToCompare(
        await eventsApi.histogram(caseId!, timelineId!, filters, buckets),
      );
    },
    enabled: scopeReady && dataKind === "time",
    ...busyRetry,
  });

  const punchcardQuery = useQuery({
    queryKey: ["viz-punchcard", caseId, timelineId, filters],
    queryFn: () => vizApi.punchcard(caseId!, timelineId!, filters),
    enabled: scopeReady && dataKind === "punchcard",
    ...busyRetry,
  });

  const cumulativeQuery = useQuery({
    queryKey: [
      "viz-cumulative",
      caseId,
      timelineId,
      filters,
      field,
      quantity,
      buckets,
    ],
    queryFn: () =>
      vizApi.cumulative(caseId!, timelineId!, filters, {
        field,
        quantity,
        buckets,
      }),
    enabled: scopeReady && dataKind === "cumulative",
    ...busyRetry,
  });

  const calendarQuery = useQuery({
    queryKey: ["viz-calendar", caseId, timelineId, filters, field],
    queryFn: () => vizApi.calendar(caseId!, timelineId!, filters, { field }),
    enabled: scopeReady && dataKind === "calendar",
    ...busyRetry,
  });

  const changeQuery = useQuery({
    queryKey: [
      "viz-change",
      caseId,
      timelineId,
      field,
      filters,
      config.compare,
      topN,
      config.derive,
    ],
    queryFn: async () =>
      (await vizApi.compare(caseId!, timelineId!, {
        kind: "change",
        field: field!,
        primary: filters,
        comparison: compareApiSpec!,
        limit: topN,
        derive: config.derive,
      })) as ChangeResponse,
    enabled: scopeReady && !!field && dataKind === "change" && compareApiSpec != null,
    ...busyRetry,
  });

  const lanesQuery = useQuery({
    queryKey: ["viz-lanes", caseId, timelineId, field, filters, config.inputs, limitY],
    queryFn: async () =>
      (await vizApi.lanes(caseId!, timelineId!, {
        field: field!,
        pairing: lanesPairing,
        primary: filters,
        startFilter: lanesPairing === "nextEnd" ? config.inputs.startFilter : undefined,
        endFilter: lanesPairing === "nextEnd" ? config.inputs.endFilter : undefined,
        limitY,
      })) as LanesResponse,
    enabled: scopeReady && !!field && dataKind === "lanes" && lanesReady,
    ...busyRetry,
  });

  // Shared by the pivot heatmap AND the sankey (same aggregation, two marks)
  // — switching between those chart types refetches nothing.
  const tableQuery = useQuery({
    queryKey: [
      "viz-field-table",
      caseId,
      timelineId,
      field,
      fieldY,
      filters,
      topN,
      resolved.tableSortBy,
      resolved.tableSortDir,
      config.derive,
    ],
    queryFn: () =>
      vizApi.fieldTable(caseId!, timelineId!, field!, filters, topN, {
        secondField: fieldY,
        sortBy: resolved.tableSortBy,
        sortDir: resolved.tableSortDir,
        derive: config.derive,
      }),
    enabled: scopeReady && !!field && dataKind === "table",
    ...busyRetry,
  });

  const pivotQuery = useQuery({
    queryKey: [
      "viz-field-pivot",
      caseId,
      timelineId,
      field,
      fieldY,
      filters,
      limitX,
      limitY,
      config.derive,
    ],
    queryFn: () =>
      vizApi.fieldPivot(
        caseId!,
        timelineId!,
        field!,
        fieldY!,
        filters,
        limitX,
        limitY,
        config.derive,
      ),
    enabled: scopeReady && !!(field && fieldY) && dataKind === "pivot",
    ...busyRetry,
  });

  const scatterQuery = useQuery({
    queryKey: [
      "viz-field-scatter",
      caseId,
      timelineId,
      field,
      fieldY,
      filters,
      sampleLimit,
    ],
    queryFn: () =>
      vizApi.fieldScatter(
        caseId!,
        timelineId!,
        field!,
        fieldY!,
        filters,
        sampleLimit,
      ),
    enabled: scopeReady && !!(field && fieldY) && dataKind === "scatter",
    ...busyRetry,
  });

  /** The chart type the "chart a field instead" button switches to — null when
   * this scale offers no field-charting mark at all, in which case the button
   * would be a dead end and is not rendered. */

  // Data-derived caption facts for the active query — totals, grid width,
  // and top-N capping feed the truthful caption/export lines.
  const facts: CaptionFacts = {};
  if (config.derive) {
    facts.derive =
      termsQuery.data?.derive ??
      compareTermsQuery.data?.derive ??
      timeseriesQuery.data?.derive ??
      pivotQuery.data?.derive_x ??
      changeQuery.data?.derive ??
      tableQuery.data?.derive ??
      null;
  }
  if (dataKind === "time" && timeQuery.data) {
    facts.primaryTotal = timeQuery.data.primary_total;
    if (compareOn) facts.comparisonTotal = timeQuery.data.comparison_total;
    facts.intervalSeconds = timeQuery.data.interval_seconds;
  } else if (dataKind === "terms") {
    if (compareTermsOn && compareTermsQuery.data) {
      facts.primaryTotal = compareTermsQuery.data.primary_total;
      facts.comparisonTotal = compareTermsQuery.data.comparison_total;
      facts.distinct = compareTermsQuery.data.distinct;
      facts.shownValues = compareTermsQuery.data.values.length;
      facts.otherCount = compareTermsQuery.data.primary_other;
    } else if (termsQuery.data) {
      facts.primaryTotal = termsQuery.data.total;
      facts.distinct = termsQuery.data.distinct;
      facts.shownValues = termsQuery.data.values.length;
      facts.otherCount = termsQuery.data.other_count;
    }
  } else if (dataKind === "numeric") {
    if (compareNumericOn && compareNumericQuery.data) {
      facts.primaryTotal = compareNumericQuery.data.primary_total;
      facts.comparisonTotal = compareNumericQuery.data.comparison_total;
      facts.binCount = compareNumericQuery.data.bins.length;
      facts.valueMin = compareNumericQuery.data.min;
      facts.valueMax = compareNumericQuery.data.max;
    } else if (numericQuery.data) {
      facts.overlayShown = numericQuery.data.points?.shown;
      facts.overlayTotal = numericQuery.data.points?.total;
      facts.primaryTotal = numericQuery.data.count;
      facts.binCount = numericQuery.data.bins.length;
      facts.valueMin = numericQuery.data.min;
      facts.valueMax = numericQuery.data.max;
      facts.binRule = numericQuery.data.bin_rule;
      facts.binCountClamped = numericQuery.data.bin_count_clamped;
      facts.skewness = numericQuery.data.skewness;
    }
    if (groupedOn && groupedQuery.data) {
      facts.primaryTotal = groupedQuery.data.total;
      facts.groupField = groupedQuery.data.group_field;
      facts.groupsShown = groupedQuery.data.groups.length;
      facts.groupsOmitted = groupedQuery.data.omitted_groups;
      facts.groupOmittedCount = groupedQuery.data.omitted_count;
      facts.valueMin = groupedQuery.data.min;
      facts.valueMax = groupedQuery.data.max;
      facts.groupDistinct = groupedQuery.data.distinct_groups;
      facts.groupedViolin = config.chartType === "violin";
      facts.binCount = undefined;
      facts.binRule = undefined;
      facts.binCountClamped = undefined;
      facts.skewness = undefined;
      facts.overlayShown = groupedQuery.data.points?.shown;
      facts.overlayTotal = groupedQuery.data.points?.total;
    }
  } else if (dataKind === "timeseries" && timeseriesQuery.data) {
    facts.shownValues = timeseriesQuery.data.series.length;
    facts.intervalSeconds = timeseriesQuery.data.interval_seconds;
    // The series cap is a cut like every other top-N in this page, and the
    // endpoint pays an extra aggregate to report it — so the caption says how
    // many values there were and how many events fell outside the drawn ones.
    // A derived axis makes the cut routine rather than exotic: 53 ISO weeks
    // against a default cap of 12, with `derive` echoing all 53 labels.
    facts.distinct = timeseriesQuery.data.distinct;
    facts.otherCount = timeseriesQuery.data.other_count;
    // One series per value: there is no "Other" line to roll the rest into.
    facts.otherDrawn = false;
  } else if (dataKind === "punchcard" && punchcardQuery.data) {
    facts.primaryTotal = punchcardQuery.data.total;
  } else if (dataKind === "cumulative" && cumulativeQuery.data) {
    const d = cumulativeQuery.data;
    facts.primaryTotal = d.events;
    if (d.interval_seconds > 0) facts.intervalSeconds = d.interval_seconds;
    facts.cumulative = {
      quantity: d.quantity,
      field: d.field,
      total: d.total,
      events: d.events,
      unparsed: d.unparsed,
    };
  } else if (dataKind === "calendar" && calendarQuery.data) {
    const d = calendarQuery.data;
    facts.primaryTotal = d.total;
    facts.calendar = {
      field: d.field,
      start: d.start,
      end: d.end,
      weeks: d.weeks,
      weeksTotal: d.weeks_total,
      truncated: d.truncated,
      dropped: d.dropped,
      total: d.total,
    };
  } else if (dataKind === "change" && changeQuery.data) {
    const d = changeQuery.data;
    facts.primaryTotal = d.primary_total;
    facts.comparisonTotal = d.comparison_total;
    facts.change = {
      topN: d.top_n,
      unionSize: d.union_size,
      rowsShown: d.rows_shown,
      truncated: d.truncated,
      omitted: d.omitted,
      newCount: d.rows.filter((r) => r.status === "new").length,
      vanishedCount: d.rows.filter((r) => r.status === "vanished").length,
    };
  } else if (dataKind === "lanes" && lanesQuery.data) {
    const d = lanesQuery.data;
    facts.lanes = {
      pairing: d.pairing,
      // What `IntervalLanes` actually draws, not what the response carried:
      // under `next_end` a lane whose events are all orphan ends legitimately
      // pairs nothing, and the figure filters it out. `lanesEmpty` is the
      // difference, disclosed rather than left to be read off the canvas.
      lanesShown: d.lanes.filter((l) => l.intervals.length > 0).length,
      lanesEmpty: d.lanes.filter((l) => l.intervals.length === 0).length,
      lanesTotal: d.lanes_total,
      laneCapHit: d.lane_cap_hit,
      otherLanes: d.other_lanes,
      starts: d.starts,
      ends: d.ends,
      unpairedStarts: d.unpaired_starts,
      orphanEnds: d.orphan_ends,
      rowsTruncated: d.rows_truncated,
      rowsPaired: d.rows_paired,
      rowsCap: d.rows_cap,
      undated: d.undated,
      sliceEnd: d.slice_end,
    };
  } else if (dataKind === "table" && tableQuery.data) {
    facts.primaryTotal = tableQuery.data.total;
    facts.tableTotal = tableQuery.data.total;
    facts.distinct = tableQuery.data.distinct;
    facts.shownValues = tableQuery.data.rows.length;
    facts.tableSort = tableQuery.data.sort;
    facts.tableHighlight = resolved.highlight;
    facts.tableRemainder = tableQuery.data.remainder
      ? {
          count: tableQuery.data.remainder.count,
          distinctValues: tableQuery.data.remainder.distinct_values,
        }
      : undefined;
  } else if (dataKind === "pivot" && pivotQuery.data) {
    facts.primaryTotal = pivotQuery.data.total;
    // A bounded `time:` axis reports its domain size, not a measured distinct
    // count, and was charted whole — there is no "rest in Other" to caption.
    // Left undefined rather than relying on `distinct > shown` happening to be
    // false, so the caption cannot claim truncation that did not occur.
    facts.xDistinct = pivotQuery.data.x_bounded
      ? undefined
      : pivotQuery.data.x_distinct;
    facts.xShown = pivotQuery.data.x_values.length;
    facts.yDistinct = pivotQuery.data.y_bounded
      ? undefined
      : pivotQuery.data.y_distinct;
    facts.yShown = pivotQuery.data.y_values.length;
  } else if (dataKind === "corr" && correlationQuery.data) {
    facts.primaryTotal = correlationQuery.data.total;
    facts.corrFields = correlationQuery.data.fields;
    facts.corrPairs = correlationQuery.data.pairs.length;
    facts.corrDropped = correlationQuery.data.dropped_fields.map(
      (d) => d.field,
    );
    facts.corrMinPairN = correlationQuery.data.pairs.length
      ? Math.min(...correlationQuery.data.pairs.map((p) => p.n))
      : undefined;
    facts.corrMaxPairN = correlationQuery.data.pairs.length
      ? Math.max(...correlationQuery.data.pairs.map((p) => p.n))
      : undefined;
  } else if (dataKind === "scatter" && scatterQuery.data) {
    facts.primaryTotal = scatterQuery.data.total;
    facts.sampledPoints = scatterQuery.data.sampled;
    facts.totalPoints = scatterQuery.data.total;
    facts.scatterStats = scatterQuery.data.stats;
  }

  // Advisory only — the pie still renders; the same rule runs in
  // `propose_chart`, so an agent proposal carries the identical caution.
  const pieWarning =
    chartType === "pie" && termsQuery.data
      ? pieReadabilityWarning(termsQuery.data)
      : null;
  if (pieWarning) facts.readabilityWarning = pieWarning;

  // Same advisory footing for the bar axis, which since #297 reaches 500
  // values: only the *vertical* orientation has a fixed frame, so that is the
  // one where a high Top-values renders a texture rather than a chart.
  const barTerms = compareTermsOn ? compareTermsQuery.data : termsQuery.data;
  const barBands =
    barTerms == null
      ? 0
      : barTerms.values.length +
        ((
          compareTermsOn
            ? (compareTermsQuery.data?.primary_other ?? 0) > 0 ||
              (compareTermsQuery.data?.comparison_other ?? 0) > 0
            : (termsQuery.data?.other_count ?? 0) > 0
        )
          ? 1
          : 0);
  // Compare mode draws two half-width sub-bars per band, so the rule counts
  // bars rather than categories — otherwise the threshold silently doubles on
  // the crowded case, and the warning names half of what is on screen.
  const barCount = compareTermsOn ? barBands * 2 : barBands;
  const barWarning =
    chartType === "bar" && barTerms
      ? barReadabilityWarning(barCount, resolved.orientation)
      : null;
  if (barWarning) facts.readabilityWarning = barWarning;
  if (marksQuery.data) {
    facts.marks = marksQuery.data;
    // The axis the figure actually draws, from the same helpers the charts
    // build their x scale with — so the mark lines can say how many marks
    // fall outside it and are therefore not drawn.
    facts.marksDomain =
      dataKind === "time" && timeQuery.data
        ? timeChartDomain(timeQuery.data)
        : dataKind === "timeseries" && timeseriesQuery.data
          ? timeseriesChartDomain(timeseriesQuery.data)
          : dataKind === "cumulative" && cumulativeQuery.data
            ? cumulativeChartDomain(cumulativeQuery.data)
            : dataKind === "lanes" && lanesQuery.data
              ? lanesChartDomain(lanesQuery.data)
              : null;
  }

  const captionLines = buildCaptionLines({
    caseId,
    timelineId,
    chartLabel: CHART_META[chartType].label,
    config,
    filters,
    facts,
  });
  // The table's CSV is built from the response the figure already holds, under
  // the same caption lines the image export carries.
  const csvText =
    chartType === "table" && tableQuery.data
      ? tableCsv(tableQuery.data, config, captionLines)
      : null;

  // The one query behind the chart on screen. Named once rather than
  // re-derived per state, so the spinner, the busy-lane badge and any future
  // read of it can never disagree about which query the canvas is showing.
  // `groupedOn` is a modifier on the numeric kind — box/violin per group have
  // their own endpoint, and numericQuery is disabled while it is on.
  const activeQuery =
    dataKind === "time"
      ? timeQuery
      : dataKind === "terms"
        ? compareTermsOn
          ? compareTermsQuery
          : termsQuery
        : dataKind === "numeric"
          ? groupedOn
            ? groupedQuery
            : compareNumericOn
              ? compareNumericQuery
              : numericQuery
          : dataKind === "timeseries"
            ? timeseriesQuery
            : dataKind === "punchcard"
              ? punchcardQuery
              : dataKind === "cumulative"
                ? cumulativeQuery
                : dataKind === "calendar"
                  ? calendarQuery
                  : dataKind === "change"
                    ? changeQuery
                  : dataKind === "lanes"
                    ? lanesQuery
                  : dataKind === "pivot"
                    ? pivotQuery
                    : dataKind === "table"
                      ? tableQuery
                      : dataKind === "scatter"
                        ? scatterQuery
                        : dataKind === "corr"
                          ? correlationQuery
                          : null;

  const loading = activeQuery?.isLoading ?? false;

  // A busy scan lane (#300) being retried. `error` stays empty until the
  // retries stop, so `failureReason` is the only in-flight signal — without
  // it every chart on this page reads as merely slow for up to four minutes
  // and then fails, which is what the lane was built to avoid.
  const waiting = busyMessage(activeQuery?.failureReason);

  const exportFilename = `${
    dataKind === "time"
      ? "events_over_time"
      : dataKind === "punchcard"
        ? "activity_punchcard"
        : requiresSecondField && field && fieldY
          ? `${field}_x_${fieldY}`
          : (field ?? "visualization")
  }_${chartType}`;

  return (
    <div className="flex h-full overflow-hidden">
      {caseId && timelineId && (
        <ChartRail
          caseId={caseId}
          timelineId={timelineId}
          timelineName={timelineQuery.data?.name}
          explorerHref={explorerHref}
          config={config}
          updateConfig={updateConfig}
          fields={fieldsQuery.data?.fields ?? []}
          resolved={resolved}
          resolvedMarks={marksQuery.data}
          autoBinCount={numericQuery.data?.bins.length}
          autoNotice={autoNotice}
          setAutoNotice={setAutoNotice}
          chartRefLive={chartRefLive}
          brokenChartRef={brokenChartRef}
          droppedScope={droppedScope}
          corrMethod={corrMethod}
          setCorrMethod={setCorrMethod}
          metricAvailable={metricAvailable}
          // The *resolved* filters, routine collapse included. Only this page
          // re-derives collapse from live dispositions; the story card and the
          // frozen export render a saved chart's stored filters verbatim, so
          // leaving it out here is what would make those two show the
          // uncollapsed superset of what was saved.
          currentFilters={filters}
          onApplyScenario={applyScenario}
          onLoadSavedChart={loadSavedChart}
          svgRef={svgRef}
          exportFilename={exportFilename}
          captionLines={captionLines}
          csv={csvText}
        />
      )}

      {/* Canvas */}
      <div className="flex-1 overflow-auto p-4">
        {/* Scope first, chart second: these filters come from the Explorer via
            the URL, and a chart that gets exported into a report has to say
            what it covers before it is read. */}
        <InheritedFiltersBar
          filters={urlFilters}
          explorerHref={explorerHref}
          onRemove={(key, fieldKey, value) =>
            updateFilters(removeFilterEntry(urlFilters, key, fieldKey, value))
          }
          onClearAll={() => updateFilters({})}
          onResetRange={() =>
            updateFilters({ ...urlFilters, start: undefined, end: undefined })
          }
        />
        {/* Nothing hidden silently: whenever routine dispositions shape the
            charts (or have been revealed), say so — the grid's collapsed-count
            stat, one page over. Renders only when the set is non-empty, same
            as the Explorer's toggle. */}
        {hasRoutineDispositions && (
          <div className="mb-2 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
            <span>
              {collapseRoutine
                ? "Routine events collapsed (muted templates and patterns marked routine) — charts match the Explorer grid"
                : "Routine events shown — charts include events muted in the Explorer"}
            </span>
            <Tooltip
              content={
                collapseRoutine
                  ? "Temporarily show routine events — the next mute re-applies collapse"
                  : "Collapse routine events again"
              }
            >
              <button
                type="button"
                onClick={() =>
                  setRoutineOverride({
                    value: !collapseRoutine,
                    signature: routineSig,
                  })
                }
                className={`flex items-center gap-1 rounded border px-1.5 py-0.5 hover:bg-[var(--color-bg-hover)] ${
                  collapseRoutine
                    ? "border-[var(--color-border)]"
                    : "border-[var(--color-accent)] text-[var(--color-accent)]"
                }`}
              >
                <Repeat size={11} />{" "}
                {collapseRoutine ? "Show routine events" : "Collapse routine"}
              </button>
            </Tooltip>
          </div>
        )}
        {multiField && selectedFields.length < 2 ? (
          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-[var(--color-fg-muted)]">
            Pick at least two numeric fields to correlate.
          </div>
        ) : dataKind === "change" && !compareOn ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-[var(--color-fg-muted)]">
            Ranked change needs a second window — turn on Compare (Baseline or Custom filters).
          </div>
        ) : dataKind === "lanes" && field && !lanesReady ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 px-6 text-center text-sm text-[var(--color-fg-muted)]">
            Start → next end pairing needs a start filter and an end filter — set both under the
            figure's inputs.
          </div>
        ) : !fieldFree && !fieldOptional && !multiField && !field ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-[var(--color-fg-muted)]">
            {fieldsQuery.isLoading ? (
              <>
                <Spinner size={20} />
                <span className="text-xs">
                  Scanning fields — can take a while on large timelines…
                </span>
              </>
            ) : (
              "Choose a field to visualize."
            )}
          </div>
        ) : requiresSecondField && !fieldY ? (
          <div className="flex h-full items-center justify-center text-sm text-[var(--color-fg-muted)]">
            Choose a second field (Y) to chart{" "}
            {CHART_META[chartType].label.toLowerCase()}.
          </div>
        ) : chartTypeUnplottable ? (
          // The rail cannot offer this pairing, but a saved chart or a URL can
          // still carry one. Without this branch the numeric probe stays
          // disabled, `numericQuery.data` never arrives, and every render gate
          // below is `data && <Chart/>` — a blank canvas with no spinner and
          // no explanation.
          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-[var(--color-fg-muted)]">
            {fieldTokenLabel(field!)} has no numeric values, so{" "}
            {CHART_META[chartType].label.toLowerCase()} would render empty. Pick
            a categorical chart type — bar, pie or heatmap.
          </div>
        ) : loading ? (
          <div className="flex h-full flex-col items-center justify-center gap-2">
            <Spinner size={24} />
            {waiting && (
              <span className="text-xs text-[var(--color-fg-muted)]">
                {waiting}
              </span>
            )}
          </div>
        ) : (
          <div className="relative rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-4">
            {/* A busy lane while a chart is already on screen. `isLoading` is
                false the moment the key has data, so a filter change on a
                drawn chart would otherwise retry silently behind stale marks.
                Gated on `isFetching`: a retry delay still counts as fetching,
                so it clears when the answer (or the error) lands. */}
            {waiting && activeQuery?.isFetching && (
              <div className="pointer-events-none absolute inset-0 z-10 flex items-center justify-center">
                <span className="rounded bg-[var(--color-bg-elevated)] px-2 py-0.5 text-xs text-[var(--color-fg-muted)] shadow">
                  {waiting}
                </span>
              </div>
            )}
            {chartType === "time" && timeQuery.data && (
              <CompareHistogram
                data={timeQuery.data}
                metric={metric}
                hasComparison={compareOn}
                svgRef={svgRef}
                onRangeSelect={(start, end) =>
                  updateFilters({ ...urlFilters, start, end })
                }
                marks={marksQuery.data?.marks}
              />
            )}
            {chartType === "bar" && barTerms && (
              <>
                {barWarning && (
                  <div className="mb-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-3 py-2 text-xs text-[var(--color-fg-secondary)]">
                    <strong className="text-[var(--color-fg-primary)]">
                      Readability:
                    </strong>{" "}
                    {barWarning}{" "}
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-auto px-0.5 py-0 text-xs font-normal underline hover:text-[var(--color-accent)]"
                      onClick={() =>
                        updateConfig({
                          options: {
                            ...config.options,
                            orientation: "horizontal",
                          },
                        })
                      }
                    >
                      Switch to horizontal
                    </Button>
                  </div>
                )}
                <BarChart
                  terms={compareTermsOn ? undefined : termsQuery.data}
                  compare={compareTermsOn ? compareTermsQuery.data : undefined}
                  orientation={resolved.orientation}
                  sort={resolved.sort}
                  valueOrder={
                    termsQuery.data?.derive?.labels ??
                    compareTermsQuery.data?.derive?.labels
                  }
                  logScale={resolved.logScale}
                  svgRef={svgRef}
                  onValueClick={handleChartValueClick}
                />
              </>
            )}
            {chartType === "pie" && termsQuery.data && (
              <>
                {pieWarning && (
                  <div className="mb-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-3 py-2 text-xs text-[var(--color-fg-secondary)]">
                    <strong className="text-[var(--color-fg-primary)]">
                      Readability:
                    </strong>{" "}
                    {pieWarning}{" "}
                    <button
                      type="button"
                      className="underline hover:text-[var(--color-accent)]"
                      onClick={() => updateConfig({ chartType: "waffle" })}
                    >
                      Switch to waffle
                    </button>
                  </div>
                )}
                <PieChart
                  terms={termsQuery.data}
                  svgRef={svgRef}
                  onValueClick={handleChartValueClick}
                />
              </>
            )}
            {chartType === "waffle" && termsQuery.data && (
              <WaffleChart
                terms={termsQuery.data}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "heatmap" && timeseriesQuery.data && (
              <Heatmap
                data={timeseriesQuery.data}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "line" && timeseriesQuery.data && (
              <LineChart
                data={timeseriesQuery.data}
                seriesMode={resolved.seriesMode}
                // Line markers default ON (Tufte: show where data actually
                // is); the shared resolver defaults showPoints off because
                // box/violin overlays cost an extra scan, so read the raw
                // option here instead of the resolved one.
                showPoints={config.options.showPoints ?? true}
                showLegend={resolved.legend}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
                marks={marksQuery.data?.marks}
              />
            )}
            {chartType === "histogram" &&
              (compareNumericOn
                ? compareNumericQuery.data
                : numericQuery.data) && (
                <NumericHistogram
                  stats={compareNumericOn ? undefined : numericQuery.data}
                  compare={
                    compareNumericOn ? compareNumericQuery.data : undefined
                  }
                  logScale={resolved.logScale}
                  showDensity={resolved.showDensity}
                  showMarkers
                  svgRef={svgRef}
                />
              )}
            {chartType === "histogram" &&
              !compareNumericOn &&
              numericQuery.data && (
                <NumericStatStrip stats={numericQuery.data} />
              )}
            {groupedOn &&
              (chartType === "box" || chartType === "violin") &&
              groupedQuery.data && (
                <GroupedDistribution
                  data={groupedQuery.data}
                  mark={chartType}
                  showPoints={showPoints}
                  svgRef={svgRef}
                  onValueClick={handleChartValueClick}
                />
              )}
            {(chartType === "box" || chartType === "violin") && (
              <div className="mb-1 flex flex-wrap items-center gap-3 text-xs text-[var(--color-fg-muted)]">
                <span className="flex items-center gap-1">
                  Median <ExplainerPopover id="median" />
                </span>
                <span className="flex items-center gap-1">
                  Quartiles <ExplainerPopover id="quartiles" />
                </span>
                <span className="flex items-center gap-1">
                  IQR <ExplainerPopover id="iqr" />
                </span>
                {chartType === "box" ? (
                  <span className="flex items-center gap-1">
                    Whiskers <ExplainerPopover id="whiskers" />
                  </span>
                ) : (
                  <span className="flex items-center gap-1">
                    Density shape <ExplainerPopover id="kde" />
                  </span>
                )}
              </div>
            )}
            {!groupedOn && chartType === "box" && numericQuery.data && (
              <BoxPlot
                stats={numericQuery.data}
                showPoints={showPoints}
                svgRef={svgRef}
              />
            )}
            {!groupedOn && chartType === "violin" && numericQuery.data && (
              <ViolinPlot
                stats={numericQuery.data}
                showPoints={showPoints}
                svgRef={svgRef}
              />
            )}
            {chartType === "ecdf" && numericQuery.data && (
              <EcdfChart stats={numericQuery.data} svgRef={svgRef} />
            )}
            {chartType === "punchcard" && punchcardQuery.data && (
              <PunchCard data={punchcardQuery.data} svgRef={svgRef} />
            )}
            {chartType === "cumulative" && cumulativeQuery.data && (
              <CumulativeStep
                data={cumulativeQuery.data}
                svgRef={svgRef}
                marks={marksQuery.data?.marks}
              />
            )}
            {chartType === "calendar" && calendarQuery.data && (
              <CalendarHeatmap data={calendarQuery.data} svgRef={svgRef} />
            )}
            {chartType === "change" && changeQuery.data && (
              <RankedChange data={changeQuery.data} layout={layout} svgRef={svgRef} />
            )}
            {chartType === "lanes" && lanesQuery.data && (
              <IntervalLanes data={lanesQuery.data} marks={marksQuery.data?.marks} svgRef={svgRef} />
            )}
            {chartType === "table" && tableQuery.data && (
              <TableFigure
                data={tableQuery.data}
                config={config}
                highlight={resolved.highlight}
                svgRef={svgRef}
              />
            )}
            {chartType === "pivot" && pivotQuery.data && (
              <PivotHeatmap
                data={pivotQuery.data}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {chartType === "sankey" && pivotQuery.data && (
              <SankeyFlow
                data={pivotQuery.data}
                svgRef={svgRef}
                onValueClick={handleChartValueClick}
              />
            )}
            {multiField && correlationQuery.data && (
              <CorrMatrix
                data={correlationQuery.data}
                method={corrMethod}
                svgRef={svgRef}
                onPairClick={(x, y) =>
                  updateConfig({
                    chartType: "scatter",
                    field: x,
                    fieldY: y,
                    scale: "ratio",
                  })
                }
              />
            )}
            {chartType === "scatter" && scatterQuery.data && (
              <>
                <ScatterChart
                  data={scatterQuery.data}
                  logScale={resolved.logScale}
                  svgRef={svgRef}
                />
                {scatterQuery.data.stats && (
                  <ScatterStatsPanel stats={scatterQuery.data.stats} />
                )}
              </>
            )}
            <ChartCaption lines={captionLines} />
          </div>
        )}
      </div>

      {pendingClick && caseId && timelineId && (
        <ChartActionPopover
          click={pendingClick}
          // `urlFilters`, not `filters`: the latter carries the live
          // `collapseRoutine` this page derives from dispositions, which the
          // URL never carried and `takeOver` therefore reports as a narrowing
          // the edit dropped. Every other call site passes `urlFilters` for
          // exactly this reason — see the take-over comment above.
          explorerHref={`/cases/${caseId}/timelines/${timelineId}?${filtersToParams(
            applyFieldEntries(urlFilters, pendingClick.entries, true),
          ).toString()}`}
          onFilter={(include) => {
            updateFilters(
              applyFieldEntries(urlFilters, pendingClick.entries, include),
            );
            setPendingClick(null);
          }}
          onClose={() => setPendingClick(null)}
        />
      )}
    </div>
  );
}
