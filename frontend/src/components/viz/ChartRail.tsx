/**
 * The Visualize page's control rail, read top-down in dependency order:
 * Field → Treat as → Figure → what the figure asks for → Compare → Metric →
 * Options. Everything a figure asks for is rendered from the registry
 * (`CHART_META[c].inputs`), so there is no figure-specific block here that a
 * registry row could disagree with — `tests/test_chart_meta.py` and
 * `chartRail.test.tsx` pin the two sides to each other.
 *
 * Every automatic re-pick names itself through `setAutoNotice` (#298); the
 * analyst's own pick clears it. `docs/VISUALIZE.md` §"The rail".
 */
import { useEffect, useRef, useState, type RefObject } from "react";
import { Link } from "react-router-dom";
import { ArrowLeft, HelpCircle, X } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { FieldCombo, type FieldComboOption } from "@/components/ui/FieldCombo";
import type {
  EventFilters,
  ResolvedMarksResponse,
  VizFieldInfo,
} from "@/api/types";
import { ExportControls } from "./ExportControls";
import { CompareFilterEditor } from "./CompareFilterEditor";
import { MarksEditor } from "./MarksEditor";
import { SavedChartsRail } from "./SavedChartsRail";
import { ExplainerPopover } from "./primitives/ExplainerPopover";
import { FigureThumbnail } from "./primitives/FigureThumbnail";
import type { CorrMethod } from "./charts/CorrMatrix";
import type {
  ChartConfig,
  ChartType,
  DeriveSpec,
  Scale,
  TableColumn,
  TableSortColumn,
  TimePart,
} from "./lib/chartConfig";
import { DEFAULT_TABLE_COLUMNS, TABLE_COLUMN_LABELS } from "./lib/tableRows";
import {
  CHART_META,
  SCALES,
  chartTypesFor,
  type DataKind,
} from "./lib/chartMeta";
import {
  chartTypesForField,
  defaultChartTypeForScale,
  TOPN_MAX,
  TOPN_MIN,
  TOPN_SLIDER_MAX,
  type ResolvedChartOptions,
} from "./lib/chartOptions";
import { METRIC_INFO, type Metric } from "./lib/transforms";
import { fieldTokenLabel } from "./lib/fieldDisplay";
import { isTimeField } from "./lib/timeFields";
import { CHART_HOW_TO_READ } from "./lib/explainers";
import { SCALE_DISPLAY, scaleTooltip } from "./lib/scaleDisplay";
import { galleryEntries } from "./lib/figureGallery";
import {
  defaultDerive,
  deriveOptionsFor,
  describeDerive,
  effectiveScale,
  singleFixFor,
  TIME_PART_LABELS,
  type DeriveKind,
} from "./lib/derive";

export interface ChartRailProps {
  caseId: string;
  timelineId: string;
  timelineName: string | undefined;
  explorerHref: string;
  config: ChartConfig;
  updateConfig: (patch: Partial<ChartConfig>) => void;
  fields: VizFieldInfo[];
  /** The server's resolution of `config.marks`, for the per-source status lines. */
  resolvedMarks?: ResolvedMarksResponse;
  resolved: ResolvedChartOptions;
  /** The automatic (Freedman–Diaconis) bin count, once the numeric scan landed. */
  autoBinCount: number | undefined;
  autoNotice: string | null;
  setAutoNotice: (notice: string | null) => void;
  chartRefLive: boolean;
  brokenChartRef: "unfetchable" | "unreadable" | "missing" | null;
  droppedScope: string[] | null;
  corrMethod: CorrMethod;
  setCorrMethod: (m: CorrMethod) => void;
  metricAvailable: (m: Metric) => boolean;
  /** The resolved filters, routine collapse included — what a save freezes. */
  currentFilters: EventFilters;
  onLoadSavedChart: (chartId: string) => void;
  svgRef: RefObject<SVGSVGElement | null>;
  exportFilename: string;
  captionLines: string[];
  /** CSV text for the table figure, else null — adds a CSV export format. */
  csv?: string | null;
}

/** Radix Select and the field combo forbid an empty value, so "count every
 * event" needs a sentinel that cannot collide with a real field token. */
const NO_FIELD = "__viz_no_field__";

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
function firstFieldChartingType(
  scale: Scale,
  field: string | null,
): ChartType | null {
  const legal = chartTypesForField(scale, field).filter(
    (c) =>
      CHART_META[c].dataKind !== "time" &&
      CHART_META[c].dataKind !== "punchcard",
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
  if (
    chartType === "punchcard" ||
    chartType === "pivot" ||
    chartType === "sankey" ||
    chartType === "scatter"
  ) {
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

/** Comma-separated edges, committed on blur/Enter when they parse as a
 * strictly increasing list; otherwise the draft stays and says why. */
const TABLE_COLUMN_CHOICES: TableColumn[] = [
  "count",
  "share",
  "first_seen",
  "last_seen",
  "distinct_second",
];
const TABLE_SORT_CHOICES: TableSortColumn[] = [
  "value",
  ...TABLE_COLUMN_CHOICES,
];

/** Comma-separated values whose rows are highlighted; committed on blur/Enter. */
function HighlightInput({
  values,
  onCommit,
}: {
  values: string[];
  onCommit: (values: string[]) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const shown = draft ?? values.join(", ");
  const commit = () => {
    const parsed = shown
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s !== "");
    setDraft(null);
    onCommit(parsed);
  };
  return (
    <div>
      <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
        Highlight values
      </label>
      <input
        type="text"
        aria-label="Highlight values"
        value={shown}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
        placeholder="alice, bob"
        className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-xs text-[var(--color-fg-primary)] focus:border-[var(--color-accent)] focus:outline-none"
      />
      <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
        Presentation only — the caption says which rows were highlighted.
      </p>
    </div>
  );
}

function EdgesInput({
  edges,
  onCommit,
}: {
  edges: number[];
  onCommit: (edges: number[]) => void;
}) {
  const [draft, setDraft] = useState<string | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const shown = draft ?? edges.join(", ");
  const commit = () => {
    const parsed = shown
      .split(",")
      .map((s) => s.trim())
      .filter((s) => s !== "")
      .map(Number);
    const ok =
      parsed.length > 0 &&
      parsed.every(
        (n, i) => Number.isFinite(n) && (i === 0 || n > parsed[i - 1]),
      );
    if (!ok) {
      setProblem(
        "Edges must be numbers in increasing order, e.g. 0, 1024, 10240",
      );
      return;
    }
    setProblem(null);
    setDraft(null);
    onCommit(parsed);
  };
  return (
    <div>
      <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
        Edges
      </label>
      <input
        type="text"
        aria-label="Range edges"
        value={shown}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") commit();
        }}
        className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-xs text-[var(--color-fg-primary)] tabular-nums focus:border-[var(--color-accent)] focus:outline-none"
      />
      {problem && (
        <p className="mt-1 text-xs text-[var(--color-warning)]">{problem}</p>
      )}
    </div>
  );
}

/**
 * The Top-values control: a slider over the range most charts want, the exact
 * box beside it as the escape hatch to the chart type's ceiling (#297), and
 * the sentence naming that escape hatch.
 *
 * The slider keeps a draft while the analyst is interacting and commits once,
 * on release, for two reasons that pull the same way.
 *
 * Cost: every commit re-runs a gated ClickHouse foreground scan, the same
 * reason the box beside it debounces. Committing on `change` spent one per
 * intermediate step — dragging across a bar chart's full travel was fifty
 * scans and fifty history entries for one gesture.
 *
 * Truth: `topN` may sit above the slider's own maximum, so the thumb is
 * pinned and reads a smaller number than the label by design. A drag that
 * ends there reports its answer only through the release — but a release is
 * an answer only if something moved. A click that lands on the pinned thumb
 * and lets go moved nothing, and must not silently rewrite a typed 500 down
 * to 50. `moved` is that distinction: a drag away from the pinned position
 * and back fires `change` events on the way, so it commits; a press that
 * never leaves it is indistinguishable from a click and does not. Above the
 * slider's range the label and the exact box stay authoritative.
 */
function TopNControl({
  chartType,
  dataKind,
  value,
  onCommit,
}: {
  chartType: ChartType;
  dataKind: DataKind;
  value: number;
  onCommit: (n: number) => void;
}) {
  const sliderMax = TOPN_SLIDER_MAX[chartType];
  const [draft, setDraft] = useState<number | null>(null);
  const moved = useRef(false);
  const shown = draft ?? Math.min(value, sliderMax);
  const commit = (n: number) => {
    setDraft(null);
    moved.current = false;
    if (n !== value) onCommit(n);
  };
  return (
    <div>
      <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
        Top values: {draft ?? value}
      </label>
      <div className="flex items-center gap-2">
        <input
          type="range"
          aria-label="Top values"
          min={TOPN_MIN}
          max={sliderMax}
          step={1}
          value={shown}
          onChange={(e) => {
            moved.current = true;
            setDraft(Number(e.target.value));
          }}
          onPointerDown={() => {
            moved.current = false;
          }}
          onPointerUp={(e) => {
            if (moved.current) commit(Number(e.currentTarget.value));
            else setDraft(null);
          }}
          onPointerCancel={() => {
            setDraft(null);
            moved.current = false;
          }}
          onKeyUp={(e) => {
            // Only the keys that actually move a range input. A bare Tab
            // *into* the slider also fires keyup on it, and with a typed 500
            // the clamped thumb reads 50 — committing there would rewrite the
            // value for merely focusing the control.
            if (!SLIDER_KEYS.has(e.key)) return;
            commit(Number(e.currentTarget.value));
          }}
          onBlur={() => {
            // A gesture that ended without its own release event (a keypress
            // interrupted by a click elsewhere) still has an answer on screen.
            if (draft != null) commit(draft);
          }}
          className="min-w-0 flex-1 accent-[var(--color-accent)]"
        />
        <TopNInput
          value={value}
          max={TOPN_MAX[chartType]}
          onCommit={onCommit}
        />
      </div>
      {/* The escape hatch has to be named wherever it exists, and the gap is
          *widest* on the timeseries charts (slider 20, ceiling 50) — naming it
          only on the terms branch left the number box undiscoverable exactly
          where it matters most. */}
      <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
        {`Up to ${TOPN_MAX[chartType]}`}
        {sliderMax < TOPN_MAX[chartType]
          ? ` — past the slider's ${sliderMax}, type an exact number.`
          : "."}
        {dataKind === "timeseries" &&
          " Each value is its own series, so a crowded chart stops being readable well before that."}
      </p>
    </div>
  );
}

/** The gallery tile's caption: the label up to its parenthetical, except
 * where two figures would otherwise read alike. */
function shortLabel(chartType: ChartType): string {
  if (chartType === "pivot") return "Field × field";
  if (chartType === "sankey") return "Flow";
  return CHART_META[chartType].label.split(" (")[0];
}

export function ChartRail({
  caseId,
  timelineId,
  timelineName,
  explorerHref,
  config,
  updateConfig,
  fields,
  resolved,
  resolvedMarks,
  autoBinCount,
  autoNotice,
  setAutoNotice,
  chartRefLive,
  brokenChartRef,
  droppedScope,
  corrMethod,
  setCorrMethod,
  metricAvailable,
  currentFilters,
  onLoadSavedChart,
  svgRef,
  exportFilename,
  captionLines,
  csv = null,
}: ChartRailProps) {
  const { field, fieldY, scale, chartType, metric } = config;
  const dataKind = CHART_META[chartType].dataKind;
  const requiresSecondField = CHART_META[chartType].requiresSecondField;
  const acceptsSecondField = CHART_META[chartType].acceptsSecondField;
  const multiField = CHART_META[chartType].multiField;
  const selectedFields = config.fields ?? [];
  const groupedOn = acceptsSecondField && !!fieldY;
  const fieldFree = dataKind === "time" || dataKind === "punchcard";
  // A third state: the figure charts every event without a field and the
  // field's values with one. "No field" is selectable and keeps the figure.
  const fieldOptional = CHART_META[chartType].inputs.field === "optional";
  const compareOn = config.compare.mode !== "off";
  const compareSupported = CHART_META[chartType].supportsCompare;
  const {
    topN,
    bins,
    buckets,
    quantity,
    limitX,
    limitY,
    sampleLimit,
    groups,
    showPoints,
  } = resolved;

  /** A token typed into the second-field picker that X already holds (#308).
   * Refused with the reason at the picker, since clearing X would only have
   * the defaulting effect refill it with a field nobody picked. */
  const [fieldYTaken, setFieldYTaken] = useState<string | null>(null);

  // A derivation is a change of scale: the field is treated as `scale`, but
  // the figure sees `effScale` — ordered categories whenever one is active.
  const derive = config.derive;
  const deriveKinds = deriveOptionsFor(scale, field);
  const effScale = effectiveScale(scale, derive);

  // Keep chartType valid when the analyst changes what the field is treated
  // as — clamped at event time rather than in an effect, so there is never a
  // render with an inconsistent scale/chartType pair.
  const handleScaleChange = (s: Scale) => {
    // A derivation the new treat-as no longer offers is dropped with it.
    const nextDerive =
      derive && deriveOptionsFor(s, field).includes(derive.kind)
        ? derive
        : null;
    const eff = effectiveScale(s, nextDerive);
    const patch: Partial<ChartConfig> = { scale: s };
    if (nextDerive !== derive) patch.derive = nextDerive;
    if (!chartTypesForField(eff, field).includes(chartType)) {
      const next = defaultChartTypeForScale(eff, field);
      updateConfig({ ...patch, chartType: next });
      // Say which of the two reasons forced the re-pick: blaming the scale for
      // a clamp the *field* forced is a false statement about the chart.
      const legalForScale = chartTypesFor(eff).includes(chartType);
      setAutoNotice(
        legalForScale && field
          ? `Figure switched to ${CHART_META[next].label} — ${CHART_META[chartType].label} can't plot ${fieldTokenLabel(field)}.`
          : `Figure switched to ${CHART_META[next].label} — ${CHART_META[chartType].label} isn't available for a field treated as ${SCALE_DISPLAY[s].label.toLowerCase()}.`,
      );
    } else {
      updateConfig(patch);
      setAutoNotice(null);
    }
  };

  /** Apply *next* (or none). A derived field is ordered categories, so the
   * figure is re-picked at that scale when the current one is illegal there,
   * and the bar axis defaults to value order — ranges read in order. */
  const applyDerive = (next: DeriveSpec | null, chartOverride?: ChartType) => {
    const eff = effectiveScale(scale, next);
    const patch: Partial<ChartConfig> = { derive: next };
    const target = chartOverride ?? chartType;
    if (!chartTypesForField(eff, field).includes(target)) {
      patch.chartType = defaultChartTypeForScale(eff, field);
    } else if (chartOverride) {
      patch.chartType = chartOverride;
    }
    if (
      next &&
      (patch.chartType ?? chartType) === "bar" &&
      config.options.sort == null
    ) {
      patch.options = { ...config.options, sort: "value" };
    }
    updateConfig(patch);
  };

  const fieldOptions: FieldComboOption[] = [
    {
      value: NO_FIELD,
      label: "No field — count every event",
      hint: "(events over time, punch card, cumulative, calendar)",
    },
    ...fields.map(fieldComboOption),
  ];

  const secondFieldLabel =
    chartType === "table"
      ? "Count distinct of (optional)"
      : acceptsSecondField
        ? "Group by (optional)"
        : "Field (Y)";

  return (
    <div className="flex w-72 shrink-0 flex-col gap-4 overflow-y-auto border-r border-[var(--color-border)] bg-[var(--color-bg-surface)] p-3">
      <div data-rail-section="header">
        {/* The resolved filters, not the raw params: under `?c_chart=<id>`
            the URL names a chart and holds no filters at all, and the
            Explorer has no use for `c_*` keys either way. */}
        <Link
          to={explorerHref}
          className="flex items-center gap-1 text-xs text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
        >
          <ArrowLeft size={12} /> Back to Explorer
        </Link>
        <h2 className="mt-1 text-sm font-semibold text-[var(--color-fg-primary)]">
          Visualize {timelineName ? `— ${timelineName}` : ""}
        </h2>
        {/* A link into a chart that is gone, or one this build cannot read.
            Said out loud rather than left to look like a chart that was
            always this shape — the page below is the default chart, not the
            one the link named. */}
        {brokenChartRef && (
          <p role="status" className="mt-2 text-xs text-[var(--color-warning)]">
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
          <p role="status" className="mt-2 text-xs text-[var(--color-warning)]">
            Editing this chart dropped {droppedScope.join(" and ")} — it now
            covers the whole timeline. Reload the saved chart to get that scope
            back.
          </p>
        )}
      </div>

      {/* Field — always live. The field-free figures are reached through the
          first entry rather than by an inert box, so the topmost control is
          never dead (#298). Hidden for the correlation matrix, which charts a
          list of fields (its own picker sits under the gallery). */}
      <div
        data-rail-section="field"
        className={multiField ? "hidden" : undefined}
      >
        <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
          {requiresSecondField ? "Field (X)" : "Field"}
        </label>
        <FieldCombo
          aria-label={requiresSecondField ? "Field (X)" : "Field"}
          placeholder="Choose a field…"
          options={fieldOptions}
          value={
            fieldFree || (fieldOptional && !field) ? NO_FIELD : (field ?? "")
          }
          onChange={(v) => {
            if (v === NO_FIELD) {
              if (fieldOptional) updateConfig({ field: null, derive: null });
              else if (!fieldFree)
                updateConfig({ field: null, chartType: "time" });
              setAutoNotice(null);
              return;
            }
            // X and Y must differ, and the Y list drops whatever X holds — so
            // setting X to the token Y already had left an unreachable value
            // sitting there under "not in this timeline's reported fields",
            // which is a claim about the wrong problem entirely.
            const takesOverY = !!v && v === fieldY;
            const patch: Partial<ChartConfig> = takesOverY
              ? { field: v, fieldY: null }
              : { field: v };
            // A derivation the new field does not offer goes with the old one.
            if (derive && !deriveOptionsFor(scale, v).includes(derive.kind))
              patch.derive = null;
            if (fieldFree) {
              // Leaving a field-free figure: land on the first figure that
              // charts a field, and say so — the analyst picked a field, not
              // a figure.
              const next = firstFieldChartingType(scale, v);
              if (next) patch.chartType = next;
            }
            updateConfig(patch);
            setAutoNotice(
              takesOverY
                ? `${acceptsSecondField ? "Group by" : "Field (Y)"} cleared — ${fieldTokenLabel(v)} is now the X field.`
                : fieldFree && patch.chartType
                  ? `Figure set to ${CHART_META[patch.chartType].label} — the ${CHART_META[chartType].label} charts no field of its own.`
                  : null,
            );
          }}
        />
        {fieldFree && (
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            {fieldFreeReason(chartType)}
          </p>
        )}
        {fieldOptional && (
          <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
            Optional — without a field this figure counts every event.
          </p>
        )}
      </div>

      {/* Treat as — the scale of measurement in the analyst's words; the
          Stevens term lives in the tooltip. Per chart, stored as `scale`. */}
      <fieldset data-rail-section="scale" role="group" aria-label="Treat as">
        <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
          Treat as
        </label>
        <div className="space-y-1">
          {SCALES.map((s) => (
            <label
              key={s}
              className={`flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm ${
                scale === s
                  ? "bg-[var(--color-accent-dim)]"
                  : "hover:bg-[var(--color-bg-hover)]"
              }`}
            >
              <input
                type="radio"
                name="scale"
                aria-label={SCALE_DISPLAY[s].label}
                checked={scale === s}
                onChange={() => handleScaleChange(s)}
                className="accent-[var(--color-accent)]"
              />
              {SCALE_DISPLAY[s].label}
              <Tooltip content={scaleTooltip(s)} side="right">
                <HelpCircle
                  size={12}
                  className="text-[var(--color-fg-muted)]"
                />
              </Tooltip>
            </label>
          ))}
        </div>
        {/* Never under a live chart reference: the page does not remount when
            a saved chart is opened (`chartRefLive` is derived from `c_chart`
            on the same route), so a notice about the chart the analyst was
            building would otherwise survive onto a stored chart the rail
            re-picked nothing for. */}
        {!chartRefLive && autoNotice && (
          <p className="mt-1 text-xs text-[var(--color-info)]">{autoNotice}</p>
        )}
      </fieldset>

      {/* Derive — a change of scale, offered only where the treat-as admits
          one (a measure → ranges; a number or time → ranges or a calendar
          part; never a `time:` field, which is already a part). Computed in
          ClickHouse before aggregation; the caption names the edges. */}
      {deriveKinds.length > 0 && (
        <fieldset data-rail-section="derive" role="group" aria-label="Derive">
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Derive
          </label>
          <div className="space-y-1">
            {(
              [
                { key: null, label: "Use the value as is" },
                ...(deriveKinds.includes("bins")
                  ? [{ key: "bins" as const, label: "Group into ranges" }]
                  : []),
                ...(deriveKinds.includes("timePart")
                  ? [{ key: "timePart" as const, label: "Calendar part" }]
                  : []),
              ] as { key: DeriveKind | null; label: string }[]
            ).map((opt) => (
              <label
                key={opt.key ?? "none"}
                className={`flex cursor-pointer items-center gap-2 rounded px-2 py-1.5 text-sm ${
                  (derive?.kind ?? null) === opt.key
                    ? "bg-[var(--color-accent-dim)]"
                    : "hover:bg-[var(--color-bg-hover)]"
                }`}
              >
                <input
                  type="radio"
                  name="derive"
                  aria-label={opt.label}
                  checked={(derive?.kind ?? null) === opt.key}
                  onChange={() =>
                    applyDerive(opt.key ? defaultDerive(opt.key, scale) : null)
                  }
                  className="accent-[var(--color-accent)]"
                />
                {opt.label}
              </label>
            ))}
          </div>
          {derive?.kind === "bins" && (
            <div className="mt-2 space-y-2">
              <Select
                value={derive.mode}
                onValueChange={(v) =>
                  applyDerive(
                    v === "custom"
                      ? { kind: "bins", mode: "custom", edges: [0] }
                      : {
                          kind: "bins",
                          mode: v as "width" | "log",
                          count: derive.mode === "custom" ? 8 : derive.count,
                        },
                  )
                }
              >
                <SelectTrigger
                  className="h-7 text-xs"
                  aria-label="Range spacing"
                >
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value="width">Equal-width ranges</SelectItem>
                  <SelectItem value="log">
                    Log-spaced ranges (bytes, durations)
                  </SelectItem>
                  <SelectItem value="custom">My own edges</SelectItem>
                </SelectContent>
              </Select>
              {derive.mode !== "custom" ? (
                <div>
                  <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                    Ranges: {derive.count}
                  </label>
                  <input
                    type="range"
                    aria-label="Number of ranges"
                    min={2}
                    max={50}
                    step={1}
                    value={derive.count}
                    onChange={(e) =>
                      applyDerive({
                        kind: "bins",
                        mode: derive.mode,
                        count: Number(e.target.value),
                      })
                    }
                    className="w-full accent-[var(--color-accent)]"
                  />
                </div>
              ) : (
                <EdgesInput
                  edges={derive.edges}
                  onCommit={(edges) =>
                    applyDerive({ kind: "bins", mode: "custom", edges })
                  }
                />
              )}
            </div>
          )}
          {derive?.kind === "timePart" && (
            <Select
              value={derive.part}
              onValueChange={(v) =>
                applyDerive({ kind: "timePart", part: v as TimePart })
              }
            >
              <SelectTrigger
                className="mt-2 h-7 text-xs"
                aria-label="Calendar part"
              >
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {(Object.keys(TIME_PART_LABELS) as TimePart[]).map((p) => (
                  <SelectItem key={p} value={p}>
                    {TIME_PART_LABELS[p]} (UTC)
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          )}
          {derive && (
            <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
              {describeDerive(derive)} — now treated as ordered categories.
            </p>
          )}
        </fieldset>
      )}

      {/* Figure — every figure, lit when legal for (field, treat-as); a greyed
          one carries its reason as the tooltip rather than vanishing. */}
      <div data-rail-section="figure">
        <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
          Figure
        </label>
        <div
          role="radiogroup"
          aria-label="Figure"
          className="grid grid-cols-3 gap-1"
        >
          {galleryEntries(effScale, field).map(
            ({ chartType: c, legal, reason }) => {
              // A greyed figure that exactly one derivation would light applies
              // it on click and says so; two candidates would be a guess, so
              // the tile stays inert and its tooltip stays the reason.
              const fix = legal ? null : singleFixFor(c, scale, field);
              return (
                <Button
                  key={c}
                  variant="ghost"
                  size="sm"
                  role="radio"
                  aria-checked={chartType === c}
                  aria-disabled={!legal}
                  aria-label={CHART_META[c].label}
                  title={
                    fix && reason
                      ? `${reason} Click to ${describeDerive(fix)}.`
                      : (reason ?? CHART_META[c].question)
                  }
                  onClick={() => {
                    if (legal) {
                      updateConfig({ chartType: c });
                      setAutoNotice(null);
                      return;
                    }
                    if (!fix || !field) return;
                    applyDerive(fix, c);
                    setAutoNotice(
                      `${CHART_META[c].label} needs categories — ${
                        fix.kind === "timePart"
                          ? `took the ${TIME_PART_LABELS[fix.part]} (UTC) of ${fieldTokenLabel(field)}`
                          : describeDerive(fix).replace(
                              /^grouped/,
                              `grouped ${fieldTokenLabel(field)}`,
                            )
                      }.`,
                    );
                  }}
                  className={`flex h-auto flex-col items-center gap-0.5 rounded border px-1 py-1.5 ${
                    chartType === c
                      ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-fg-primary)]"
                      : legal
                        ? "border-[var(--color-border)] text-[var(--color-fg-secondary)] hover:border-[var(--color-accent)]"
                        : fix
                          ? "border-[var(--color-border)] text-[var(--color-fg-secondary)] opacity-40 hover:opacity-70"
                          : "cursor-not-allowed border-[var(--color-border)] text-[var(--color-fg-secondary)] opacity-40"
                  }`}
                >
                  <FigureThumbnail chartType={c} />
                  <span className="w-full truncate text-center text-xs leading-tight">
                    {shortLabel(c)}
                  </span>
                </Button>
              );
            },
          )}
        </div>
        <p className="mt-1 text-xs text-[var(--color-fg-secondary)]">
          {CHART_META[chartType].question}
        </p>
        <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
          {CHART_HOW_TO_READ[chartType]}
        </p>
      </div>

      {/* What the figure asks for — one block per declared input key. */}
      {"fields" in CHART_META[chartType].inputs && (
        <div data-rail-section="fields">
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Fields to correlate ({selectedFields.length}/8){" "}
            <ExplainerPopover id="correlationMatrix" />
          </label>
          <div className="mb-1 flex flex-wrap gap-1">
            {selectedFields.map((token) => (
              <Button
                key={token}
                variant="outline"
                size="sm"
                onClick={() =>
                  updateConfig({
                    fields: selectedFields.filter((f) => f !== token),
                  })
                }
                className="h-auto gap-1 px-2 py-0.5 text-xs"
                title="Remove from the matrix"
              >
                {fieldTokenLabel(token)} <X size={10} />
              </Button>
            ))}
            {selectedFields.length === 0 && (
              <span className="text-xs text-[var(--color-fg-muted)]">
                Pick at least two numeric fields.
              </span>
            )}
          </div>
          <div className="mb-1 flex gap-1">
            {(["pearson", "spearman"] as const).map((m) => (
              <Button
                key={m}
                variant="outline"
                size="sm"
                onClick={() => setCorrMethod(m)}
                className={`h-auto flex-1 px-2 py-1 text-xs ${
                  corrMethod === m
                    ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-fg-primary)]"
                    : ""
                }`}
              >
                {m === "pearson" ? "Pearson r" : "Spearman ρ"}
              </Button>
            ))}
            <ExplainerPopover
              id={corrMethod === "pearson" ? "pearson" : "spearman"}
            />
          </div>
          <FieldCombo
            aria-label="Add a field to correlate"
            placeholder="Add a field…"
            // The box stays empty: this picker adds to the chip list above
            // rather than holding a selection of its own — so close the set:
            // the matrix charts fields the inventory reported, and a typo
            // appended as a chip returns an empty matrix with nothing naming
            // the cause.
            allowFreeText={false}
            value=""
            options={fields
              .filter(
                (f) =>
                  !selectedFields.includes(f.token) && !isTimeField(f.token),
              )
              .map(fieldComboOption)}
            onChange={(v) =>
              v && updateConfig({ fields: [...selectedFields, v].slice(0, 8) })
            }
          />
        </div>
      )}

      {"secondField" in CHART_META[chartType].inputs && (
        <div data-rail-section="secondField">
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            {secondFieldLabel}
          </label>
          <FieldCombo
            aria-label={secondFieldLabel}
            placeholder={
              acceptsSecondField ? "No grouping" : "Choose a second field…"
            }
            options={[
              ...(acceptsSecondField
                ? [{ value: CLEAR_GROUP, label: "No grouping" }]
                : []),
              ...fields.filter((f) => f.token !== field).map(fieldComboOption),
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
              {requiresSecondField ? "X field" : "charted field"} — pick a
              different one.
            </p>
          )}
        </div>
      )}

      {"columns" in CHART_META[chartType].inputs && (
        <fieldset data-rail-section="columns" role="group" aria-label="Columns">
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Columns
          </label>
          <div className="space-y-1">
            {TABLE_COLUMN_CHOICES.map((c) => {
              const current =
                config.inputs.columns ??
                (fieldY
                  ? [...DEFAULT_TABLE_COLUMNS, "distinct_second"]
                  : DEFAULT_TABLE_COLUMNS);
              const checked = current.includes(c);
              const disabled = c === "distinct_second" && !fieldY;
              return (
                <label
                  key={c}
                  className={`flex items-center gap-2 text-xs ${
                    disabled
                      ? "text-[var(--color-fg-muted)]"
                      : "cursor-pointer text-[var(--color-fg-secondary)]"
                  }`}
                >
                  <input
                    type="checkbox"
                    aria-label={TABLE_COLUMN_LABELS[c]}
                    checked={checked && !disabled}
                    disabled={disabled}
                    onChange={(e) => {
                      const next = TABLE_COLUMN_CHOICES.filter((k) =>
                        k === c ? e.target.checked : current.includes(k),
                      );
                      updateConfig({
                        inputs: { ...config.inputs, columns: next },
                      });
                    }}
                    className="accent-[var(--color-accent)]"
                  />
                  {TABLE_COLUMN_LABELS[c]}
                  {disabled && (
                    <span className="text-[var(--color-fg-muted)]">
                      — needs a second field
                    </span>
                  )}
                </label>
              );
            })}
          </div>
        </fieldset>
      )}

      {/* Compare — time histogram, bar (grouped), numeric histogram (overlay).
          Always rendered; disabled (with the reason) for chart types without
          an honest two-layer encoding, instead of silently disappearing. */}
      <div data-rail-section="compare">
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
              onChange={(f) =>
                updateConfig({ compare: { mode: "custom", filters: f } })
              }
              fields={fields}
            />
          </div>
        )}
      </div>

      {/* Metric */}
      {dataKind === "time" && (
        <div data-rail-section="metric">
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

      {CHART_META[chartType].supportsMarks && (
        <div data-rail-section="marks">
          <label className="mb-1 block text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Marks
          </label>
          <MarksEditor
            caseId={caseId}
            timelineId={timelineId}
            marks={config.marks}
            onChange={(marks) => updateConfig({ marks })}
            fields={fields}
            resolved={resolvedMarks}
          />
        </div>
      )}

      {/* Per-chart options */}
      {(chartType === "bar" ||
        chartType === "histogram" ||
        chartType === "time" ||
        chartType === "line" ||
        chartType === "cumulative" ||
        chartType === "table") && (
        <details className="rounded border border-[var(--color-border)]">
          <summary className="cursor-pointer px-2 py-1.5 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
            Options
          </summary>
          <div className="space-y-3 px-2 pb-2 pt-1">
            {chartType === "cumulative" && (
              <div>
                <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                  Quantity
                </label>
                <Select
                  value={quantity}
                  onValueChange={(v) =>
                    updateConfig({
                      options: {
                        ...config.options,
                        quantity: v as "events" | "sum" | "distinct",
                      },
                    })
                  }
                >
                  <SelectTrigger className="h-7 text-xs" aria-label="Quantity">
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    <SelectItem value="events">Running event count</SelectItem>
                    <SelectItem
                      value="sum"
                      disabled={!field || scale !== "ratio"}
                    >
                      Running sum (measure)
                    </SelectItem>
                    <SelectItem
                      value="distinct"
                      disabled={
                        !field || (scale !== "nominal" && scale !== "ordinal")
                      }
                    >
                      Distinct values seen so far
                    </SelectItem>
                  </SelectContent>
                </Select>
                <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
                  Sum needs a field treated as Measure; distinct needs
                  Categories or Ordered categories.
                </p>
              </div>
            )}
            {chartType === "table" && (
              <>
                <div>
                  <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                    Sort by
                  </label>
                  <Select
                    value={config.options.tableSortBy ?? "count"}
                    onValueChange={(v) =>
                      updateConfig({
                        options: {
                          ...config.options,
                          tableSortBy: v as TableSortColumn,
                        },
                      })
                    }
                  >
                    <SelectTrigger className="h-7 text-xs" aria-label="Sort by">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {TABLE_SORT_CHOICES.map((c) => (
                        <SelectItem
                          key={c}
                          value={c}
                          disabled={c === "distinct_second" && !fieldY}
                        >
                          {TABLE_COLUMN_LABELS[c]}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                </div>
                <div>
                  <label className="mb-1 block text-xs text-[var(--color-fg-secondary)]">
                    Direction
                  </label>
                  <Select
                    value={config.options.tableSortDir ?? "desc"}
                    onValueChange={(v) =>
                      updateConfig({
                        options: {
                          ...config.options,
                          tableSortDir: v as "asc" | "desc",
                        },
                      })
                    }
                  >
                    <SelectTrigger
                      className="h-7 text-xs"
                      aria-label="Direction"
                    >
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="desc">Descending</SelectItem>
                      <SelectItem value="asc">Ascending</SelectItem>
                    </SelectContent>
                  </Select>
                </div>
                <HighlightInput
                  values={config.options.highlight ?? []}
                  onCommit={(values) => {
                    const { highlight: _dropped, ...rest } = config.options;
                    updateConfig({
                      options: values.length
                        ? { ...rest, highlight: values }
                        : rest,
                    });
                  }}
                />
              </>
            )}
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
                        options: {
                          ...config.options,
                          sort: v as "count" | "value",
                        },
                      })
                    }
                  >
                    <SelectTrigger className="h-7 text-xs">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      <SelectItem value="count">
                        By count (descending)
                      </SelectItem>
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
                      options: {
                        ...config.options,
                        logScale: e.target.checked,
                      },
                    })
                  }
                  className="accent-[var(--color-accent)]"
                />
                Log-scale count axis
              </label>
            )}
            {(chartType === "time" || chartType === "cumulative") && (
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
                      options: {
                        ...config.options,
                        buckets: Number(e.target.value),
                      },
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
                        options: {
                          ...config.options,
                          legend: e.target.checked,
                        },
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
                        options: {
                          ...config.options,
                          showPoints: e.target.checked,
                        },
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
            Bins: {bins ?? `auto (${autoBinCount ?? "…"})`}{" "}
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
                    bins: e.target.checked ? undefined : (autoBinCount ?? 30),
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
                      options: {
                        ...config.options,
                        showPoints: e.target.checked,
                      },
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
                        options: {
                          ...config.options,
                          groups: Number(e.target.value),
                        },
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
                    options: {
                      ...config.options,
                      showDensity: e.target.checked,
                    },
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
                updateConfig({
                  options: { ...config.options, bins: Number(e.target.value) },
                })
              }
              className="w-full accent-[var(--color-accent)]"
            />
          )}
        </div>
      )}
      {(dataKind === "terms" ||
        dataKind === "timeseries" ||
        dataKind === "table") && (
        <TopNControl
          chartType={chartType}
          dataKind={dataKind}
          value={topN}
          onCommit={(n) =>
            updateConfig({ options: { ...config.options, topN: n } })
          }
        />
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
                updateConfig({
                  options: {
                    ...config.options,
                    limitX: Number(e.target.value),
                  },
                })
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
                updateConfig({
                  options: {
                    ...config.options,
                    limitY: Number(e.target.value),
                  },
                })
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
                updateConfig({
                  options: { ...config.options, sampleLimit: Number(v) },
                })
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
                updateConfig({
                  options: { ...config.options, logScale: e.target.checked },
                })
              }
              className="accent-[var(--color-accent)]"
            />
            Log-scale axes (positive values only)
          </label>
        </div>
      )}

      <div className="mt-auto space-y-3 border-t border-[var(--color-border)] pt-3">
        <SavedChartsRail
          caseId={caseId}
          timelineId={timelineId}
          currentConfig={config}
          // The *resolved* filters, routine collapse included. Only this
          // page re-derives collapse from live dispositions; the story
          // card and the frozen export render a saved chart's stored
          // filters verbatim, so leaving it out here is what would make
          // those two show the uncollapsed superset of what was saved.
          currentFilters={currentFilters}
          onLoad={onLoadSavedChart}
        />
        <ExportControls
          svgRef={svgRef}
          filename={exportFilename}
          captionLines={captionLines}
          csv={csv}
        />
      </div>
    </div>
  );
}
