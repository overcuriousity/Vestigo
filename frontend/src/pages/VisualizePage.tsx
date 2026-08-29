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
import { Link, useParams, useSearchParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, HelpCircle, Lightbulb, Repeat, X } from "lucide-react";
import { savedChartsApi, vizApi, type CompareMode } from "@/api/viz";
import { eventsApi } from "@/api/events";
import { timelinesApi } from "@/api/timelines";
import { dispositionsApi } from "@/api/dispositions";
import { FILTER_PARAM_KEYS, filtersToParams, paramsToFilters } from "@/lib/queryParams";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { ExportControls } from "@/components/viz/ExportControls";
import { CompareFilterEditor } from "@/components/viz/CompareFilterEditor";
import { SavedChartsRail } from "@/components/viz/SavedChartsRail";
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
import { PivotHeatmap } from "@/components/viz/charts/PivotHeatmap";
import { SankeyFlow } from "@/components/viz/charts/SankeyFlow";
import { ScatterChart } from "@/components/viz/charts/ScatterChart";
import { CorrMatrix, type CorrMethod } from "@/components/viz/charts/CorrMatrix";
import {
  CHART_ID_PARAM,
  chartUrlParams,
  histogramToCompare,
  paramsToChartConfig,
  parseStoredChartConfig,
  parseStoredChartFilters,
  unrepresentableFilterMembers,
  type ChartConfig,
  type ChartType,
  type Scale,
} from "@/components/viz/lib/chartConfig";
import { METRIC_INFO, type Metric } from "@/components/viz/lib/transforms";
import { CHART_META, SCALES, chartTypesFor } from "@/components/viz/lib/chartMeta";
import {
  resolveChartOptions,
  defaultChartTypeForScale,
  chartTypesForField,
  TOPN_MAX,
  TOPN_MIN,
  TOPN_SLIDER_MAX,
} from "@/components/viz/lib/chartOptions";
import { FieldCombo, type FieldComboOption } from "@/components/ui/FieldCombo";
import { fieldTokenLabel } from "@/components/viz/lib/fieldDisplay";
import { isTimeField, TIME_FIELDS } from "@/components/viz/lib/timeFields";
import { buildCaptionLines, type CaptionFacts } from "@/components/viz/lib/caption";
import { CHART_PRESETS } from "@/components/viz/lib/presets";
import { pieReadabilityWarning } from "@/components/viz/lib/pieReadability";
import { barReadabilityWarning } from "@/components/viz/lib/barReadability";
import { ChartCaption } from "@/components/viz/primitives/ChartCaption";
import { ExplainerPopover } from "@/components/viz/primitives/ExplainerPopover";
import { CHART_HOW_TO_READ } from "@/components/viz/lib/explainers";
import { NumericStatStrip } from "@/components/viz/NumericStatStrip";
import { ScatterStatsPanel } from "@/components/viz/ScatterStatsPanel";
import type {
  CompareNumericResponse,
  CompareTermsResponse,
  CompareTimeResponse,
  EventFilters,
  VizFieldInfo,
} from "@/api/types";

const SCALE_INFO: Record<Scale, { label: string; hint: string }> = {
  nominal: {
    label: "Nominal",
    hint: "Unordered categories — e.g. HTTP method, source IP, artifact type. Identity only; order carries no meaning.",
  },
  ordinal: {
    label: "Ordinal",
    hint: "Ordered categories — e.g. log level (debug < info < warning < error). Order matters, but not the distance between steps.",
  },
  interval: {
    label: "Interval",
    hint: "Numeric with meaningful differences but no true zero — e.g. a timestamp. Differences are meaningful; ratios are not.",
  },
  ratio: {
    label: "Ratio",
    hint: "Numeric with a true zero — e.g. bytes transferred, response time, request count. Differences and ratios are both meaningful.",
  },
};

const METRICS: Metric[] = ["count", "delta", "rate", "ratio", "cumulative"];

/** Radix Select forbids an empty-string item value, so "no grouping" needs a
 * sentinel that cannot collide with a real field token. */
const CLEAR_GROUP = "__viz_no_group__";

/** One field picker option: display name plus a muted qualifier.
 *
 * The qualifier is driven off `isTimeField`, not off a null `distinct`: a
 * virtual field has no measured distinct count, and "time field" tells the
 * analyst more about why than an empty parenthetical would. Ordinary fields
 * guard on null anyway, so an absent count renders nothing rather than
 * "(null distinct)". */
/** "a ratio scale", but "an interval scale" — the scale names are data. */
function articleFor(word: string): string {
  return /^[aeiou]/i.test(word) ? "an" : "a";
}

function fieldComboOption(f: VizFieldInfo): FieldComboOption {
  return {
    value: f.token,
    label: fieldTokenLabel(f.token),
    hint: isTimeField(f.token)
      ? "(time field)"
      : f.distinct != null
        ? `(${f.distinct} distinct)`
        : undefined,
  };
}

/** The chart type to switch to when the analyst wants to chart a field and the
 * current one charts none. `defaultChartTypeForScale` is not enough on its own:
 * its preference list ends in `time`, which is itself field-free, so on a scale
 * whose only other legal marks are field-free it would hand back the state we
 * are trying to leave. */
function firstFieldChartingType(scale: Scale, field: string | null): ChartType | null {
  const legal = chartTypesForField(scale, field).filter(
    (c) => CHART_META[c].dataKind !== "time" && CHART_META[c].dataKind !== "punchcard",
  );
  if (legal.length === 0) return null;
  const preferred = defaultChartTypeForScale(scale, field);
  return legal.includes(preferred) ? preferred : legal[0];
}

/** Why the field picker is inert — shown instead of a bare greyed box, the same
 * way Compare states its own reason rather than disappearing (#298). */
function fieldFreeReason(chartType: ChartType): string {
  return chartType === "punchcard"
    ? "The punchcard counts events by weekday and hour, so it charts no field of its own."
    : "The time histogram counts every event in each bucket, so it charts no field of its own.";
}

/** Why Compare is disabled for a chart type — shown instead of hiding the
 * control (see chartMeta: pie/box/violin/ecdf have no honest two-layer
 * encoding; the newer kinds simply have no compare aggregation yet). */
function compareUnavailableReason(chartType: ChartType): string {
  if (chartType === "punchcard" || chartType === "pivot" || chartType === "sankey" || chartType === "scatter") {
    return "Compare isn't supported for this chart type yet.";
  }
  return "This chart type has no honest two-layer encoding — overlaid layers would misrepresent one of them. Use Bar, Histogram, or the Time histogram to compare.";
}

/**
 * How long the exact-value box waits after the last keystroke before it
 * commits. Every commit re-runs a gated ClickHouse foreground scan, and the
 * digits of "500" are all in range on the way there — undebounced, typing one
 * number spent three of them (5, 50, 500). Long enough to swallow a multi-digit
 * entry, short enough that the live preview the box is built around survives.
 */
const TOPN_COMMIT_DEBOUNCE_MS = 400;

/** The keys a range input moves on — see the Top-values slider's `onKeyUp`. */
const SLIDER_KEYS = new Set([
  "ArrowLeft",
  "ArrowRight",
  "ArrowUp",
  "ArrowDown",
  "PageUp",
  "PageDown",
  "Home",
  "End",
]);

/**
 * The exact-value box beside the Top-values slider (#297).
 *
 * It keeps its own draft string, because coercing every keystroke through
 * `Number` makes the field impossible to retype: `Number("")` is `0`, which is
 * finite, so the first Backspace committed `topN: 1` and the digits of "300"
 * then landed on top of it. A draft that is empty, or that parses outside
 * `[TOPN_MIN, max]`, is simply not committed — it stays on screen until blur
 * or Enter, which clamp to whatever the chart can actually draw.
 */
function TopNInput({
  value,
  max,
  onCommit,
}: {
  value: number;
  max: number;
  onCommit: (n: number) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const pending = useRef<ReturnType<typeof setTimeout> | null>(null);
  const cancelPending = () => {
    if (pending.current != null) {
      clearTimeout(pending.current);
      pending.current = null;
    }
  };
  // A commit scheduled by the last keystroke must not outlive the control.
  useEffect(() => cancelPending, []);
  const parse = (raw: string): number | null => {
    if (raw.trim() === "") return null;
    const n = Number(raw);
    if (!Number.isFinite(n)) return null;
    return Math.round(n);
  };
  const commitNow = (n: number) => {
    cancelPending();
    onCommit(Math.max(TOPN_MIN, Math.min(n, max)));
  };
  return (
    <input
      type="number"
      min={TOPN_MIN}
      max={max}
      step={1}
      value={draft ?? String(value)}
      onChange={(e) => {
        const raw = e.target.value;
        setDraft(raw);
        cancelPending();
        const n = parse(raw);
        // Only an in-range number is a finished answer. "3" on the way to
        // "300" is in range and previews once typing pauses; "900" against a
        // ceiling of 500 waits for blur or Enter rather than snapping
        // mid-keystroke.
        if (n != null && n >= TOPN_MIN && n <= max) {
          pending.current = setTimeout(() => {
            pending.current = null;
            onCommit(n);
          }, TOPN_COMMIT_DEBOUNCE_MS);
        }
      }}
      onKeyDown={(e) => {
        // Enter is the one gesture that says "I am done typing". Without it an
        // out-of-range entry sat on screen, uncommitted and unclamped, until
        // the analyst happened to click elsewhere.
        if (e.key !== "Enter") return;
        e.preventDefault();
        const n = parse(draft ?? String(value));
        if (n != null) commitNow(n);
        setDraft(null);
      }}
      onBlur={() => {
        const n = parse(draft ?? "");
        if (n != null) commitNow(n);
        else cancelPending();
        setDraft(null);
      }}
      aria-label="Top values (exact)"
      className="w-16 shrink-0 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-xs text-[var(--color-fg-primary)] tabular-nums focus:border-[var(--color-accent)] focus:outline-none"
    />
  );
}

export function VisualizePage() {
  const { caseId, timelineId } = useParams<{ caseId: string; timelineId: string }>();
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
    () => (chartId ? savedChartsQuery.data?.charts.find((c) => c.id === chartId) : undefined),
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
  const chartRefSettled = savedChartsQuery.isSuccess || savedChartsQuery.isError;
  // A `c_chart` that names a deleted chart, one saved by an incompatible
  // config version, or one whose list could not be loaded at all falls through
  // to the params — the page still works, and the notice below says why it is
  // not the chart that was linked.
  const chartRefBroken =
    !!chartId && chartRefSettled && (savedChart === undefined || storedConfig === null);
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
      savedChartsQuery.isError ? "unfetchable" : savedChart ? "unreadable" : "missing",
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
    const { collapseRoutine: _live, ...stored } = parseStoredChartFilters(savedChart.config);
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
    !!(caseId && timelineId) && dispositionsQuery.isSuccess && (!chartId || chartRefSettled);
  const filters = useMemo(
    () => (collapseRoutine ? { ...urlFilters, collapseRoutine: true } : urlFilters),
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

  const updateConfig = useCallback(
    (patch: Partial<ChartConfig>) => takeOver({ ...config, ...patch }, urlFilters),
    [takeOver, config, urlFilters],
  );

  // The one place a filter change is written from this page.
  const updateFilters = useCallback(
    (next: EventFilters) => takeOver(config, next),
    [takeOver, config],
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
  const [pendingClick, setPendingClick] = useState<ChartValueClick | null>(null);
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
  const groupedOn = acceptsSecondField && !!fieldY;
  // "time" and "punchcard" chart the whole event count — no field involved.
  const fieldFree = dataKind === "time" || dataKind === "punchcard";
  const compareOn = config.compare.mode !== "off";
  const compareSupported = !!CHART_META[chartType].supportsCompare;
  const compareApiSpec: CompareMode | null =
    config.compare.mode === "baseline"
      ? { mode: "baseline" }
      : config.compare.mode === "custom"
        ? { mode: "custom", filters: config.compare.filters }
        : null;

  // Shared with the agent's ChartProposalCard so a proposed chart and a
  // hand-built one resolve their defaults identically.
  const resolved = useMemo(() => resolveChartOptions(config), [config]);
  const { topN, bins, buckets, limitX, limitY, sampleLimit, groups, showPoints } = resolved;

  const svgRef = useRef<SVGSVGElement | null>(null);
  // Preset strip: open by default on a fresh page (no chart state in the
  // URL yet); a URL that already describes a chart skips the guidance.
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
  /** A token typed into the second-field picker that X already holds (#308).
   *
   * The X and Y pickers must not name the same field, so the Y list drops
   * whatever X holds — but the box takes free text, so the token can still be
   * typed in. Committing it would show the combo's unknown-field disclosure,
   * which is a claim about the wrong problem entirely: the field *is* in this
   * timeline's inventory, it is just spoken for. The X→Y direction resolves
   * the collision by clearing Y and saying so; Y→X cannot mirror that, since X
   * is the axis the chart is built on and emptying it would only have the
   * defaulting effect refill it with a field nobody picked. So refuse, and say
   * why at the picker that refused. */
  const [fieldYTaken, setFieldYTaken] = useState<string | null>(null);
  const [presetsOpen, setPresetsOpen] = useState(() => !searchParams.has("c_type"));

  const applyPreset = (preset: (typeof CHART_PRESETS)[number]) => {
    updateConfig(preset.config);
    // A preset is an explicit pick of type, scale and field at once, so any
    // standing "we moved this for you" note is now about a chart the analyst
    // no longer has — and would read as an excuse for the one they just chose.
    setAutoNotice(null);
    setPresetsOpen(false);
  };

  const timelineQuery = useQuery({
    queryKey: ["timeline", caseId, timelineId],
    queryFn: () => timelinesApi.get(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  const fieldsQuery = useQuery({
    queryKey: ["viz-fields", caseId, timelineId],
    queryFn: () => vizApi.fields(caseId!, timelineId!),
    enabled: !!(caseId && timelineId),
  });

  // Default to the first field once the list loads — the backend sorts by
  // coverage descending, so this is the highest-coverage field.
  useEffect(() => {
    if (chartRefLive) return;
    if (field == null && fieldsQuery.data?.fields.length) {
      updateConfig({ field: fieldsQuery.data.fields[0].token });
    }
  }, [field, fieldsQuery.data, updateConfig, chartRefLive]);

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
  const chartTypeUnplottable = fieldIsTime && (dataKind === "numeric" || dataKind === "scatter");
  const numericQuery = useQuery({
    queryKey: ["viz-field-numeric", caseId, timelineId, field, filters, bins, showPoints],
    queryFn: () =>
      vizApi.fieldNumeric(caseId!, timelineId!, field!, filters, bins, showPoints),
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
    const scale = TIME_FIELDS[field].scale;
    const nextType = defaultChartTypeForScale(scale, field);
    updateConfig({ scale, chartType: nextType });
    setAutoNotice(
      `${fieldTokenLabel(field)} is a time field — scale set to ${scale}, chart set to ${CHART_META[nextType].label}.`,
    );
  }, [field, fieldIsTime, fieldFree, requiresSecondField, multiField, updateConfig, chartRefLive]);

  useEffect(() => {
    if (chartRefLive) return;
    if (!field || field === autoProbedField.current) return;
    // Inert for time fields anyway (the query is disabled, so `data` stays
    // undefined) — stated explicitly so the intent survives a refactor.
    if (fieldIsTime) return;
    if (numericQuery.data == null) return;
    autoProbedField.current = field;
    // Don't yank the analyst off the field-independent charts (time,
    // punchcard) or a deliberately-picked two-field chart.
    if (fieldFree || requiresSecondField || multiField) return;
    const isNumeric = numericQuery.data.count > 0;
    const nextScale: Scale = isNumeric ? "ratio" : "nominal";
    const nextType: ChartType = isNumeric ? "histogram" : "bar";
    // Say exactly which of the two controls moved, and say nothing when
    // neither did. The non-numeric branch used to re-pick *both* silently —
    // a Histogram on a ratio scale became a Bar on a nominal one with no word
    // about it — and then set the notice to `null`, which also wiped the
    // "Group by cleared" line the analyst's own edit had just put there a few
    // hundred milliseconds earlier. Landing on the scale and type the chart
    // already has is not a change and must not claim to be one.
    const moved: string[] = [];
    if (nextScale !== scale) moved.push(`scale set to ${nextScale}`);
    if (nextType !== chartType) moved.push(`chart set to ${CHART_META[nextType].label}`);
    if (moved.length === 0) return;
    updateConfig({ scale: nextScale, chartType: nextType });
    setAutoNotice(
      `${fieldTokenLabel(field)} ${isNumeric ? "looks numeric" : "has no numeric values"} — ${moved.join(", ")}.`,
    );
  }, [
    field,
    fieldIsTime,
    numericQuery.data,
    fieldFree,
    requiresSecondField,
    multiField,
    scale,
    chartType,
    updateConfig,
    chartRefLive,
  ]);

  // Keep chartType valid when the analyst switches scale — clamped at event
  // time rather than in an effect, so there is never a render with an
  // inconsistent scale/chartType pair.
  const handleScaleChange = (s: Scale) => {
    // Also re-picks when the type is legal for the new scale but not for the
    // field — a `time:` field cannot feed a numeric mark at any scale.
    if (!chartTypesForField(s, field).includes(chartType)) {
      const next = defaultChartTypeForScale(s, field);
      updateConfig({ scale: s, chartType: next });
      // The re-pick is correct and used to be silent: two controls where the
      // analyst touched one. Say which moved and why (#298) — and say which of
      // the two reasons it was, since blaming the scale for a clamp the *field*
      // forced is a false statement about the chart the analyst is looking at.
      const legalForScale = chartTypesFor(s).includes(chartType);
      setAutoNotice(
        legalForScale && field
          ? `Chart type switched to ${CHART_META[next].label} — ${CHART_META[chartType].label} can't plot ${fieldTokenLabel(field)}.`
          : `Chart type switched to ${CHART_META[next].label} — ${CHART_META[chartType].label} isn't available on ${articleFor(s)} ${s} scale.`,
      );
    } else {
      updateConfig({ scale: s });
      setAutoNotice(null);
    }
  };

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

  const compareTermsOn = compareOn && chartType === "bar" && compareApiSpec != null;
  const termsQuery = useQuery({
    queryKey: ["viz-field-terms", caseId, timelineId, field, filters, topN],
    queryFn: () => vizApi.fieldTerms(caseId!, timelineId!, field!, filters, topN),
    enabled: scopeReady && !!field && dataKind === "terms" && !compareTermsOn,
    ...busyRetry,
  });

  const compareTermsQuery = useQuery({
    queryKey: ["viz-compare-terms", caseId, timelineId, field, filters, config.compare, topN],
    queryFn: async () =>
      (await vizApi.compare(caseId!, timelineId!, {
        kind: "terms",
        field: field!,
        primary: filters,
        comparison: compareApiSpec!,
        limit: topN,
      })) as CompareTermsResponse,
    enabled: scopeReady && !!field && compareTermsOn,
    ...busyRetry,
  });

  const compareNumericOn = compareOn && chartType === "histogram" && compareApiSpec != null;
  const compareNumericQuery = useQuery({
    queryKey: ["viz-compare-numeric", caseId, timelineId, field, filters, config.compare, bins],
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
    queryKey: ["viz-field-correlation", caseId, timelineId, selectedFields, filters],
    queryFn: () => vizApi.fieldCorrelation(caseId!, timelineId!, selectedFields, filters),
    enabled: scopeReady && multiField && selectedFields.length >= 2,
    ...busyRetry,
  });

  const timeseriesQuery = useQuery({
    queryKey: ["viz-field-timeseries", caseId, timelineId, field, filters, buckets, topN],
    queryFn: () => vizApi.fieldTimeseries(caseId!, timelineId!, field!, filters, buckets, topN),
    enabled: scopeReady && !!field && dataKind === "timeseries",
    ...busyRetry,
  });

  // Events-over-time: one shared-grid compare call when a comparison layer
  // is on, otherwise the Explorer's own histogram adapted to the same shape.
  const timeQuery = useQuery({
    queryKey: ["viz-time", caseId, timelineId, filters, config.compare, buckets],
    queryFn: async (): Promise<CompareTimeResponse> => {
      if (compareApiSpec) {
        return (await vizApi.compare(caseId!, timelineId!, {
          kind: "time",
          primary: filters,
          comparison: compareApiSpec,
          buckets,
        })) as CompareTimeResponse;
      }
      return histogramToCompare(await eventsApi.histogram(caseId!, timelineId!, filters, buckets));
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

  // Shared by the pivot heatmap AND the sankey (same aggregation, two marks)
  // — switching between those chart types refetches nothing.
  const pivotQuery = useQuery({
    queryKey: ["viz-field-pivot", caseId, timelineId, field, fieldY, filters, limitX, limitY],
    queryFn: () => vizApi.fieldPivot(caseId!, timelineId!, field!, fieldY!, filters, limitX, limitY),
    enabled: scopeReady && !!(field && fieldY) && dataKind === "pivot",
    ...busyRetry,
  });

  const scatterQuery = useQuery({
    queryKey: ["viz-field-scatter", caseId, timelineId, field, fieldY, filters, sampleLimit],
    queryFn: () => vizApi.fieldScatter(caseId!, timelineId!, field!, fieldY!, filters, sampleLimit),
    enabled: scopeReady && !!(field && fieldY) && dataKind === "scatter",
    ...busyRetry,
  });

  const availableChartTypes = chartTypesForField(scale, field);
  /** The chart type the "chart a field instead" button switches to — null when
   * this scale offers no field-charting mark at all, in which case the button
   * would be a dead end and is not rendered. */
  const fieldChartingType = firstFieldChartingType(scale, field);

  // Data-derived caption facts for the active query — totals, grid width,
  // and top-N capping feed the truthful caption/export lines.
  const facts: CaptionFacts = {};
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
  } else if (dataKind === "punchcard" && punchcardQuery.data) {
    facts.primaryTotal = punchcardQuery.data.total;
  } else if (dataKind === "pivot" && pivotQuery.data) {
    facts.primaryTotal = pivotQuery.data.total;
    // A bounded `time:` axis reports its domain size, not a measured distinct
    // count, and was charted whole — there is no "rest in Other" to caption.
    // Left undefined rather than relying on `distinct > shown` happening to be
    // false, so the caption cannot claim truncation that did not occur.
    facts.xDistinct = pivotQuery.data.x_bounded ? undefined : pivotQuery.data.x_distinct;
    facts.xShown = pivotQuery.data.x_values.length;
    facts.yDistinct = pivotQuery.data.y_bounded ? undefined : pivotQuery.data.y_distinct;
    facts.yShown = pivotQuery.data.y_values.length;
  } else if (dataKind === "corr" && correlationQuery.data) {
    facts.primaryTotal = correlationQuery.data.total;
    facts.corrFields = correlationQuery.data.fields;
    facts.corrPairs = correlationQuery.data.pairs.length;
    facts.corrDropped = correlationQuery.data.dropped_fields.map((d) => d.field);
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
    chartType === "pie" && termsQuery.data ? pieReadabilityWarning(termsQuery.data) : null;
  if (pieWarning) facts.readabilityWarning = pieWarning;

  // Same advisory footing for the bar axis, which since #297 reaches 500
  // values: only the *vertical* orientation has a fixed frame, so that is the
  // one where a high Top-values renders a texture rather than a chart.
  const barTerms = compareTermsOn ? compareTermsQuery.data : termsQuery.data;
  const barBands =
    barTerms == null
      ? 0
      : barTerms.values.length +
        ((compareTermsOn
          ? (compareTermsQuery.data?.primary_other ?? 0) > 0 ||
            (compareTermsQuery.data?.comparison_other ?? 0) > 0
          : (termsQuery.data?.other_count ?? 0) > 0)
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

  const captionLines = buildCaptionLines({
    caseId,
    timelineId,
    chartLabel: CHART_META[chartType].label,
    config,
    filters,
    facts,
  });

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
              : dataKind === "pivot"
                ? pivotQuery
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

  return (
    <div className="flex h-full overflow-hidden">
      {/* Control rail */}
      <div className="flex w-72 shrink-0 flex-col gap-4 overflow-y-auto border-r border-[var(--color-border)] bg-[var(--color-bg-surface)] p-3">
        <div>
          {/* The resolved filters, not the raw params: under `?c_chart=<id>`
              the URL names a chart and holds no filters at all, and the
              Explorer has no use for `c_*` keys either way. */}
          {caseId && timelineId && (
            <Link
              to={explorerHref}
              className="flex items-center gap-1 text-xs text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
            >
              <ArrowLeft size={12} /> Back to Explorer
            </Link>
          )}
          <h2 className="mt-1 text-sm font-semibold text-[var(--color-fg-primary)]">
            Visualize {timelineQuery.data ? `— ${timelineQuery.data.name}` : ""}
          </h2>
          <button
            onClick={() => setPresetsOpen((v) => !v)}
            className="mt-1 flex items-center gap-1 text-xs text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
          >
            <Lightbulb size={12} /> Presets
          </button>
        </div>

        {/* A link into a chart that is gone, or one this build cannot read.
            Said out loud rather than left to look like a chart that was
            always this shape — the page below is the default chart, not the
            one the link named. */}
        {brokenChartRef && (
          <p role="status" className="text-xs text-[var(--color-warning)]">
            {brokenChartRef === "unfetchable"
              ? "That chart could not be loaded — the saved charts could not be fetched. Showing a default chart instead; reload to try again."
              : brokenChartRef === "unreadable"
                ? "That chart was saved with an incompatible config version and cannot be loaded."
                : "That saved chart no longer exists."}
          </p>
        )}

        {/* Editing a saved chart spells it into the URL, which cannot carry
            these narrowings — so the chart on screen is now wider than the one
            that was loaded. Said out loud, and repeated in the saved-chart rail
            (re-saving from here would freeze the wider slice). */}
        {droppedScope && (
          <p role="status" className="text-xs text-[var(--color-warning)]">
            Editing this chart dropped {droppedScope.join(" and ")} — it now covers the whole
            timeline. Reload the saved chart to get that scope back.
          </p>
        )}

        {/* Scale of measurement */}
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Scale of measurement
          </label>
          <div className="space-y-1">
            {SCALES.map((s) => (
              <label
                key={s}
                className={`flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm ${
                  scale === s ? "bg-[var(--color-accent-dim)]" : "hover:bg-[var(--color-bg-hover)]"
                }`}
              >
                <input
                  type="radio"
                  name="scale"
                  checked={scale === s}
                  onChange={() => handleScaleChange(s)}
                  className="accent-[var(--color-accent)]"
                />
                {SCALE_INFO[s].label}
                <Tooltip content={SCALE_INFO[s].hint} side="right">
                  <HelpCircle size={12} className="text-[var(--color-fg-muted)]" />
                </Tooltip>
              </label>
            ))}
          </div>
        </div>

        {/* Chart type */}
        <div>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Chart type
          </label>
          <Select
            value={chartType}
            onValueChange={(v) => {
              updateConfig({ chartType: v as ChartType });
              setAutoNotice(null);
            }}
          >
            <SelectTrigger className="text-sm">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {availableChartTypes.map((c) => (
                <SelectItem key={c} value={c}>
                  {CHART_META[c].label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
          {/* Never under a live chart reference: the page does not remount when
              a saved chart is opened (`chartRefLive` is derived from `c_chart`
              on the same route), so a notice about the chart the analyst was
              building would otherwise survive onto a stored chart the rail
              re-picked nothing for — a "we moved this for you" line about a
              move that never happened here. */}
          {!chartRefLive && autoNotice && (
            <p className="mt-1 text-xs text-[var(--color-info)]">{autoNotice}</p>
          )}
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            {CHART_HOW_TO_READ[chartType]}
          </p>
        </div>

        {/* Field picker — hidden for the correlation matrix, which charts a
            list of fields instead (its own picker is below). */}
        <div className={multiField ? "hidden" : undefined}>
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            {requiresSecondField ? "Field (X)" : "Field"}
          </label>
          {fieldFree ? (
            /* Rendered and inert with the reason shown, never silently dead —
               the same contract Compare keeps a few blocks down. Greying a
               control the analyst cannot see the cause of is what made this
               read as a glitch (#298). */
            <>
              <div className="rounded border border-[var(--color-border)] px-3 py-1.5 text-sm text-[var(--color-fg-muted)]">
                — event count —
              </div>
              <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                {fieldFreeReason(chartType)}
              </p>
              {fieldChartingType && (
                <Button
                  variant="outline"
                  size="sm"
                  className="mt-1.5 w-full"
                  onClick={() => {
                    updateConfig({ chartType: fieldChartingType });
                    setAutoNotice(null);
                  }}
                >
                  Chart a field instead ({CHART_META[fieldChartingType].label})
                </Button>
              )}
            </>
          ) : (
            <FieldCombo
              aria-label={requiresSecondField ? "Field (X)" : "Field"}
              placeholder="Choose a field…"
              options={(fieldsQuery.data?.fields ?? []).map(fieldComboOption)}
              value={field ?? ""}
              onChange={(v) => {
                // X and Y must differ, and the Y list drops whatever X holds —
                // so setting X to the token Y already had left an unreachable
                // value sitting there under "not in this timeline's reported
                // fields", which is a claim about the wrong problem entirely.
                const takesOverY = !!v && v === fieldY;
                updateConfig(takesOverY ? { field: v, fieldY: null } : { field: v });
                setAutoNotice(
                  takesOverY
                    ? `${acceptsSecondField ? "Group by" : "Field (Y)"} cleared — ${fieldTokenLabel(v)} is now the X field.`
                    : null,
                );
              }}
            />
          )}
        </div>

        {/* Field list — the correlation matrix charts 2–8 fields at once */}
        {multiField && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Fields to correlate ({selectedFields.length}/8){" "}
              <ExplainerPopover id="correlationMatrix" />
            </label>
            <div className="mb-1 flex flex-wrap gap-1">
              {selectedFields.map((token) => (
                <button
                  key={token}
                  type="button"
                  onClick={() =>
                    updateConfig({ fields: selectedFields.filter((f) => f !== token) })
                  }
                  className="flex items-center gap-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-xs text-[var(--color-fg-secondary)] hover:border-[var(--color-accent)]"
                  title="Remove from the matrix"
                >
                  {fieldTokenLabel(token)} <X size={10} />
                </button>
              ))}
              {selectedFields.length === 0 && (
                <span className="text-xs text-[var(--color-fg-muted)]">
                  Pick at least two numeric fields.
                </span>
              )}
            </div>
            <div className="mb-1 flex gap-1">
              {(["pearson", "spearman"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setCorrMethod(m)}
                  className={`flex-1 rounded border px-2 py-1 text-xs ${
                    corrMethod === m
                      ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-fg-primary)]"
                      : "border-[var(--color-border)] text-[var(--color-fg-secondary)] hover:border-[var(--color-accent)]"
                  }`}
                >
                  {m === "pearson" ? "Pearson r" : "Spearman ρ"}
                </button>
              ))}
              <ExplainerPopover id={corrMethod === "pearson" ? "pearson" : "spearman"} />
            </div>
            <FieldCombo
              aria-label="Add a field to correlate"
              placeholder="Add a field…"
              // The box stays empty: this picker adds to the chip list above
              // rather than holding a selection of its own — which also means
              // its `value` can never carry the unknown-token disclosure. So
              // close the set instead: the matrix charts fields the inventory
              // reported, and a typo appended as a chip returns an empty
              // matrix with nothing naming the cause.
              allowFreeText={false}
              value=""
              options={(fieldsQuery.data?.fields ?? [])
                .filter((f) => !selectedFields.includes(f.token) && !isTimeField(f.token))
                .map(fieldComboOption)}
              onChange={(v) =>
                v && updateConfig({ fields: [...selectedFields, v].slice(0, 8) })
              }
            />
          </div>
        )}

        {/* Second field picker — pivot/sankey/scatter chart both axes;
            box/violin use it optionally as a grouping variable */}
        {(requiresSecondField || acceptsSecondField) && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              {acceptsSecondField ? "Group by (optional)" : "Field (Y)"}
            </label>
            <FieldCombo
              aria-label={acceptsSecondField ? "Group by (optional)" : "Field (Y)"}
              placeholder={acceptsSecondField ? "No grouping" : "Choose a second field…"}
              options={[
                ...(acceptsSecondField
                  ? [{ value: CLEAR_GROUP, label: "No grouping" }]
                  : []),
                ...(fieldsQuery.data?.fields ?? [])
                  .filter((f) => f.token !== field)
                  .map(fieldComboOption),
              ]}
              value={fieldY ?? ""}
              onChange={(v) => {
                const next = v === CLEAR_GROUP || !v ? null : v;
                if (next && next === field) {
                  setFieldYTaken(next);
                  return;
                }
                setFieldYTaken(null);
                updateConfig({ fieldY: next });
              }}
            />
            {/* Compared against the *current* X, so the line clears itself the
                moment X moves off the token it was about. */}
            {fieldYTaken === field && field && (
              <p className="mt-1 text-xs text-[var(--color-info)]">
                {fieldTokenLabel(field)} is already the{" "}
                {requiresSecondField ? "X field" : "charted field"} — pick a different one.
              </p>
            )}
          </div>
        )}

        {/* Compare — time histogram, bar (grouped), numeric histogram (overlay).
            Always rendered; disabled (with the reason) for chart types without
            an honest two-layer encoding, instead of silently disappearing. */}
        <div>
          <label className="mb-1 flex items-center gap-1 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Compare
            <Tooltip
              content={
                compareSupported
                  ? "Adds a second layer evaluated on the same time grid: the whole timeline (baseline) or a second filter set. Both layers always share the time range and bucket width, so they are directly comparable."
                  : compareUnavailableReason(chartType)
              }
              side="right"
            >
              <HelpCircle size={12} className="text-[var(--color-fg-muted)]" />
            </Tooltip>
          </label>
          <div className="space-y-1">
            {(
              [
                { mode: "off", label: "Off" },
                { mode: "baseline", label: "Baseline (all events)" },
                { mode: "custom", label: "Custom filters" },
              ] as const
            ).map((opt) => (
              <label
                key={opt.mode}
                className={`flex items-center gap-2 rounded px-2 py-1.5 text-sm ${
                  !compareSupported
                    ? "cursor-not-allowed opacity-50"
                    : config.compare.mode === opt.mode
                      ? "cursor-pointer bg-[var(--color-accent-dim)]"
                      : "cursor-pointer hover:bg-[var(--color-bg-hover)]"
                }`}
              >
                <input
                  type="radio"
                  name="compare"
                  disabled={!compareSupported}
                  checked={compareSupported && config.compare.mode === opt.mode}
                  onChange={() =>
                    updateConfig({
                      compare:
                        opt.mode === "custom"
                          ? { mode: "custom", filters: {} }
                          : { mode: opt.mode },
                    })
                  }
                  className="accent-[var(--color-accent)]"
                />
                {opt.label}
              </label>
            ))}
          </div>
          {!compareSupported && (
            <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
              {compareUnavailableReason(chartType)}
            </p>
          )}
          {compareSupported && config.compare.mode === "custom" && (
            <div className="mt-2 rounded border border-[var(--color-border)] p-2">
              <CompareFilterEditor
                filters={config.compare.filters}
                onChange={(f) => updateConfig({ compare: { mode: "custom", filters: f } })}
                fields={fieldsQuery.data?.fields ?? []}
              />
            </div>
          )}
        </div>

        {/* Metric */}
        {dataKind === "time" && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Metric
            </label>
            <Select
              value={metric}
              onValueChange={(v) => updateConfig({ metric: v as Metric })}
            >
              <SelectTrigger className="text-sm">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {METRICS.filter(metricAvailable).map((m) => (
                  <SelectItem key={m} value={m}>
                    {METRIC_INFO[m].label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            {metric !== "count" && (
              <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                {METRIC_INFO[metric].formula}
              </p>
            )}
            {!compareOn && (
              <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                Turn on Compare to unlock “% of baseline”.
              </p>
            )}
          </div>
        )}

        {/* Per-chart options */}
        {(chartType === "bar" ||
          chartType === "histogram" ||
          chartType === "time" ||
          chartType === "line") && (
          <details className="rounded border border-[var(--color-border)]">
            <summary className="cursor-pointer px-2 py-1.5 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Options
            </summary>
            <div className="space-y-3 px-2 pb-2 pt-1">
              {chartType === "bar" && (
                <>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                      Orientation
                    </label>
                    <Select
                      value={config.options.orientation ?? "horizontal"}
                      onValueChange={(v) =>
                        updateConfig({
                          options: {
                            ...config.options,
                            orientation: v as "horizontal" | "vertical",
                          },
                        })
                      }
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="horizontal">Horizontal</SelectItem>
                        <SelectItem value="vertical">Vertical</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                      Sort
                    </label>
                    <Select
                      value={config.options.sort ?? "count"}
                      onValueChange={(v) =>
                        updateConfig({
                          options: { ...config.options, sort: v as "count" | "value" },
                        })
                      }
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="count">By count (descending)</SelectItem>
                        <SelectItem value="value">By value (A→Z)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                </>
              )}
              {(chartType === "bar" || chartType === "histogram") && (
                <label className="flex cursor-pointer items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                  <input
                    type="checkbox"
                    checked={config.options.logScale ?? false}
                    onChange={(e) =>
                      updateConfig({
                        options: { ...config.options, logScale: e.target.checked },
                      })
                    }
                    className="accent-[var(--color-accent)]"
                  />
                  Log-scale count axis
                </label>
              )}
              {chartType === "time" && (
                <div>
                  <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                    Buckets: {buckets}
                  </label>
                  <input
                    type="range"
                    min={10}
                    max={200}
                    step={10}
                    value={buckets}
                    onChange={(e) =>
                      updateConfig({
                        options: { ...config.options, buckets: Number(e.target.value) },
                      })
                    }
                    className="w-full accent-[var(--color-accent)]"
                  />
                </div>
              )}
              {chartType === "line" && (
                <>
                  <div>
                    <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                      Series mode
                    </label>
                    <Select
                      value={config.options.seriesMode ?? "overlay"}
                      onValueChange={(v) =>
                        updateConfig({
                          options: {
                            ...config.options,
                            seriesMode: v as "overlay" | "stacked",
                          },
                        })
                      }
                    >
                      <SelectTrigger className="h-7 text-xs">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="overlay">Overlay (lines)</SelectItem>
                        <SelectItem value="stacked">Stacked (areas)</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                  <label className="flex cursor-pointer items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                    <input
                      type="checkbox"
                      checked={config.options.legend ?? true}
                      onChange={(e) =>
                        updateConfig({
                          options: { ...config.options, legend: e.target.checked },
                        })
                      }
                      className="accent-[var(--color-accent)]"
                    />
                    Show legend
                  </label>
                  <label className="flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                    <input
                      type="checkbox"
                      checked={config.options.showPoints ?? true}
                      onChange={(e) =>
                        updateConfig({
                          options: { ...config.options, showPoints: e.target.checked },
                        })
                      }
                      className="accent-[var(--color-accent)]"
                    />
                    Mark measured points
                  </label>
                </>
              )}
            </div>
          </details>
        )}

        {/* Options */}
        {dataKind === "numeric" && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Bins: {bins ?? `auto (${numericQuery.data?.bins.length ?? "…"})`}{" "}
              <ExplainerPopover id="fdRule" />
            </label>
            <label className="mb-1 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
              <input
                type="checkbox"
                checked={bins == null}
                onChange={(e) =>
                  updateConfig({
                    options: {
                      ...config.options,
                      bins: e.target.checked ? undefined : (numericQuery.data?.bins.length ?? 30),
                    },
                  })
                }
                className="accent-[var(--color-accent)]"
              />
              Automatic bin width (Freedman–Diaconis)
            </label>
            {(chartType === "box" || chartType === "violin") && (
              <>
                <label className="mb-1 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                  <input
                    type="checkbox"
                    checked={showPoints}
                    onChange={(e) =>
                      updateConfig({
                        options: { ...config.options, showPoints: e.target.checked },
                      })
                    }
                    className="accent-[var(--color-accent)]"
                  />
                  Overlay data points <ExplainerPopover id="sampledPoints" />
                </label>
                {groupedOn && (
                  <div className="mb-1">
                    <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                      Groups: {groups}
                    </label>
                    <input
                      type="range"
                      min={2}
                      max={8}
                      step={1}
                      value={groups}
                      onChange={(e) =>
                        updateConfig({
                          options: { ...config.options, groups: Number(e.target.value) },
                        })
                      }
                      className="w-full accent-[var(--color-accent)]"
                    />
                  </div>
                )}
              </>
            )}
            {chartType === "histogram" && (
              <label className="mb-1 flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
                <input
                  type="checkbox"
                  checked={resolved.showDensity}
                  onChange={(e) =>
                    updateConfig({
                      options: { ...config.options, showDensity: e.target.checked },
                    })
                  }
                  className="accent-[var(--color-accent)]"
                />
                Density curve (KDE) <ExplainerPopover id="kde" />
              </label>
            )}
            {bins != null && (
              <input
                type="range"
                min={5}
                max={100}
                step={5}
                value={bins}
                onChange={(e) =>
                  updateConfig({ options: { ...config.options, bins: Number(e.target.value) } })
                }
                className="w-full accent-[var(--color-accent)]"
              />
            )}
          </div>
        )}
        {(dataKind === "terms" || dataKind === "timeseries") && (
          <div>
            <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Top values: {topN}
            </label>
            <div className="flex items-center gap-2">
              {/* The slider covers the range most charts want; the number
                  beside it is the escape hatch up to this chart type's
                  ceiling, so asking for 300 bars does not need 300 pixels of
                  slider travel (#297).

                  Both share `TOPN_MIN`: a slider whose `min` sat above the
                  box's floor let the two disagree — the DOM clamps the thumb
                  to its own `min`, so a typed 1 showed a slider reading 3, and
                  dragging to exactly 3 then fired no change event at all.

                  Clamping the *displayed* value reintroduces that same failure
                  at the other end: with a typed 300 the thumb already sits at
                  the slider's 50, so dragging it there changes nothing in the
                  DOM and fires no change event — the chart went on drawing 300.
                  Committing on release closes it, since a range input's value
                  always follows the pointer, so what the thumb reads when the
                  analyst lets go is the answer they gave. */}
              <input
                type="range"
                aria-label="Top values"
                min={TOPN_MIN}
                max={TOPN_SLIDER_MAX[chartType]}
                step={1}
                value={Math.min(topN, TOPN_SLIDER_MAX[chartType])}
                onChange={(e) =>
                  updateConfig({ options: { ...config.options, topN: Number(e.target.value) } })
                }
                onPointerUp={(e) => {
                  const n = Number(e.currentTarget.value);
                  if (n !== topN) updateConfig({ options: { ...config.options, topN: n } });
                }}
                onKeyUp={(e) => {
                  // Only the keys that actually move a range input. A bare Tab
                  // *into* the slider also fires keyup on it, and with a typed
                  // 300 the clamped thumb reads 50 — committing there would
                  // rewrite the value for merely focusing the control.
                  if (!SLIDER_KEYS.has(e.key)) return;
                  const n = Number(e.currentTarget.value);
                  if (n !== topN) updateConfig({ options: { ...config.options, topN: n } });
                }}
                className="min-w-0 flex-1 accent-[var(--color-accent)]"
              />
              <TopNInput
                value={topN}
                max={TOPN_MAX[chartType]}
                onCommit={(n) => updateConfig({ options: { ...config.options, topN: n } })}
              />
            </div>
            {/* The escape hatch has to be named wherever it exists, and the
                gap is *widest* on the timeseries charts (slider 20, ceiling
                50) — naming it only on the terms branch left the number box
                undiscoverable exactly where it matters most. */}
            <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
              {`Up to ${TOPN_MAX[chartType]}`}
              {TOPN_SLIDER_MAX[chartType] < TOPN_MAX[chartType]
                ? ` — past the slider's ${TOPN_SLIDER_MAX[chartType]}, type an exact number.`
                : "."}
              {dataKind === "timeseries" &&
                " Each value is its own series, so a crowded chart stops being readable well before that."}
            </p>
          </div>
        )}
        {dataKind === "pivot" && (
          <>
            <div>
              <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                Top X values: {limitX}
              </label>
              <input
                type="range"
                min={3}
                max={50}
                step={1}
                value={limitX}
                onChange={(e) =>
                  updateConfig({ options: { ...config.options, limitX: Number(e.target.value) } })
                }
                className="w-full accent-[var(--color-accent)]"
              />
            </div>
            <div>
              <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                Top Y values: {limitY}
              </label>
              <input
                type="range"
                min={3}
                max={50}
                step={1}
                value={limitY}
                onChange={(e) =>
                  updateConfig({ options: { ...config.options, limitY: Number(e.target.value) } })
                }
                className="w-full accent-[var(--color-accent)]"
              />
            </div>
          </>
        )}
        {dataKind === "scatter" && (
          <div className="space-y-3">
            <div>
              <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                Sample size
              </label>
              <Select
                value={String(sampleLimit)}
                onValueChange={(v) =>
                  updateConfig({ options: { ...config.options, sampleLimit: Number(v) } })
                }
              >
                <SelectTrigger className="text-sm">
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="1000">1,000 points</SelectItem>
                  <SelectItem value="5000">5,000 points</SelectItem>
                  <SelectItem value="10000">10,000 points</SelectItem>
                </SelectContent>
              </Select>
            </div>
            <label className="flex cursor-pointer items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
              <input
                type="checkbox"
                checked={config.options.logScale ?? false}
                onChange={(e) =>
                  updateConfig({ options: { ...config.options, logScale: e.target.checked } })
                }
                className="accent-[var(--color-accent)]"
              />
              Log-scale axes (positive values only)
            </label>
          </div>
        )}

        <div className="mt-auto space-y-3 border-t border-[var(--color-border)] pt-3">
          {caseId && timelineId && (
            <SavedChartsRail
              caseId={caseId}
              timelineId={timelineId}
              currentConfig={config}
              // The *resolved* filters, routine collapse included. Only this
              // page re-derives collapse from live dispositions; the story
              // card and the frozen export render a saved chart's stored
              // filters verbatim, so leaving it out here is what would make
              // those two show the uncollapsed superset of what was saved.
              currentFilters={filters}
              onLoad={loadSavedChart}
            />
          )}
          <ExportControls
            svgRef={svgRef}
            filename={`${
              dataKind === "time"
                ? "events_over_time"
                : dataKind === "punchcard"
                  ? "activity_punchcard"
                  : requiresSecondField && field && fieldY
                    ? `${field}_x_${fieldY}`
                    : (field ?? "visualization")
            }_${chartType}`}
            captionLines={captionLines}
          />
        </div>
      </div>

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
          onResetRange={() => updateFilters({ ...urlFilters, start: undefined, end: undefined })}
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
                  setRoutineOverride({ value: !collapseRoutine, signature: routineSig })
                }
                className={`flex items-center gap-1 rounded border px-1.5 py-0.5 hover:bg-[var(--color-bg-hover)] ${
                  collapseRoutine
                    ? "border-[var(--color-border)]"
                    : "border-[var(--color-accent)] text-[var(--color-accent)]"
                }`}
              >
                <Repeat size={11} /> {collapseRoutine ? "Show routine events" : "Collapse routine"}
              </button>
            </Tooltip>
          </div>
        )}
        {presetsOpen && (
          <div className="mb-4 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] p-3">
            <div className="mb-2 flex items-center justify-between">
              <span className="text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
                What do you want to find out?
              </span>
              <button
                onClick={() => setPresetsOpen(false)}
                className="text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
                aria-label="Close presets"
              >
                <X size={14} />
              </button>
            </div>
            <div className="grid gap-2 sm:grid-cols-2 lg:grid-cols-3">
              {CHART_PRESETS.map((p) => (
                <button
                  key={p.id}
                  onClick={() => applyPreset(p)}
                  className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-2.5 text-left hover:border-[var(--color-accent)]"
                >
                  <div className="text-sm font-medium text-[var(--color-fg-primary)]">
                    {p.label}
                  </div>
                  <div className="mt-1 text-xs text-[var(--color-fg-muted)]">{p.question}</div>
                </button>
              ))}
            </div>
          </div>
        )}
        {multiField && selectedFields.length < 2 ? (
          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-[var(--color-fg-muted)]">
            Pick at least two numeric fields to correlate.
          </div>
        ) : !fieldFree && !multiField && !field ? (
          <div className="flex h-full flex-col items-center justify-center gap-2 text-sm text-[var(--color-fg-muted)]">
            {fieldsQuery.isLoading ? (
              <>
                <Spinner size={20} />
                <span className="text-xs">Scanning fields — can take a while on large timelines…</span>
              </>
            ) : (
              "Choose a field to visualize."
            )}
          </div>
        ) : requiresSecondField && !fieldY ? (
          <div className="flex h-full items-center justify-center text-sm text-[var(--color-fg-muted)]">
            Choose a second field (Y) to chart {CHART_META[chartType].label.toLowerCase()}.
          </div>
        ) : chartTypeUnplottable ? (
          // The rail cannot offer this pairing, but a saved chart or a URL can
          // still carry one. Without this branch the numeric probe stays
          // disabled, `numericQuery.data` never arrives, and every render gate
          // below is `data && <Chart/>` — a blank canvas with no spinner and
          // no explanation.
          <div className="flex h-full items-center justify-center px-6 text-center text-sm text-[var(--color-fg-muted)]">
            {fieldTokenLabel(field!)} has no numeric values, so{" "}
            {CHART_META[chartType].label.toLowerCase()} would render empty. Pick a categorical
            chart type — bar, pie or heatmap.
          </div>
        ) : loading ? (
          <div className="flex h-full flex-col items-center justify-center gap-2">
            <Spinner size={24} />
            {waiting && <span className="text-xs text-[var(--color-fg-muted)]">{waiting}</span>}
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
                onRangeSelect={(start, end) => updateFilters({ ...urlFilters, start, end })}
              />
            )}
            {chartType === "bar" && barTerms && (
              <>
                {barWarning && (
                  <div className="mb-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-3 py-2 text-xs text-[var(--color-fg-secondary)]">
                    <strong className="text-[var(--color-fg-primary)]">Readability:</strong>{" "}
                    {barWarning}{" "}
                    <Button
                      variant="ghost"
                      size="sm"
                      className="h-auto px-0.5 py-0 text-xs font-normal underline hover:text-[var(--color-accent)]"
                      onClick={() =>
                        updateConfig({
                          options: { ...config.options, orientation: "horizontal" },
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
                    <strong className="text-[var(--color-fg-primary)]">Readability:</strong>{" "}
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
                <PieChart terms={termsQuery.data} svgRef={svgRef} onValueClick={handleChartValueClick} />
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
              <Heatmap data={timeseriesQuery.data} svgRef={svgRef} onValueClick={handleChartValueClick} />
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
              />
            )}
            {chartType === "histogram" &&
              (compareNumericOn ? compareNumericQuery.data : numericQuery.data) && (
                <NumericHistogram
                  stats={compareNumericOn ? undefined : numericQuery.data}
                  compare={compareNumericOn ? compareNumericQuery.data : undefined}
                  logScale={resolved.logScale}
                  showDensity={resolved.showDensity}
                  showMarkers
                  svgRef={svgRef}
                />
              )}
            {chartType === "histogram" && !compareNumericOn && numericQuery.data && (
              <NumericStatStrip stats={numericQuery.data} />
            )}
            {groupedOn && (chartType === "box" || chartType === "violin") && groupedQuery.data && (
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
              <BoxPlot stats={numericQuery.data} showPoints={showPoints} svgRef={svgRef} />
            )}
            {!groupedOn && chartType === "violin" && numericQuery.data && (
              <ViolinPlot stats={numericQuery.data} showPoints={showPoints} svgRef={svgRef} />
            )}
            {chartType === "ecdf" && numericQuery.data && (
              <EcdfChart stats={numericQuery.data} svgRef={svgRef} />
            )}
            {chartType === "punchcard" && punchcardQuery.data && (
              <PunchCard data={punchcardQuery.data} svgRef={svgRef} />
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
                  updateConfig({ chartType: "scatter", field: x, fieldY: y, scale: "ratio" })
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
          explorerHref={`/cases/${caseId}/timelines/${timelineId}?${filtersToParams(
            applyFieldEntries(filters, pendingClick.entries, true),
          ).toString()}`}
          onFilter={(include) => {
            updateFilters(applyFieldEntries(filters, pendingClick.entries, include));
            setPendingClick(null);
          }}
          onClose={() => setPendingClick(null)}
        />
      )}
    </div>
  );
}
