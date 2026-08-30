/**
 * Single source of truth for chart captions — the same lines render under
 * the chart (`ChartCaption`) and embed into SVG/PNG exports
 * (`ExportControls.captionLines`), so what the analyst reads on screen is
 * exactly what a report reader sees. Includes the truthfulness warnings
 * (top-N capping, undefined metric bins) forensic rigor demands.
 */
import type {
  DeriveEcho,
  EventFilters,
  ResolvedMarksResponse,
  ScatterStats,
} from "@/api/types";
import type { ChartConfig } from "./chartConfig";
import { describeDerive } from "./derive";
import { markCaptionLines, type MarksDomain } from "./marks";
import { TABLE_COLUMN_LABELS } from "./tableRows";
import { METRIC_INFO } from "./transforms";

/** Data-derived facts the active query contributes to the caption. */
export interface CaptionFacts {
  /** A derived field's resolved derivation (the server's echo) — the edges
   * width/log bins landed on, and whether `≤ 0` got its own range. */
  derive?: DeriveEcho | null;
  /** kind=time: layer totals + resolved bucket width. */
  primaryTotal?: number;
  comparisonTotal?: number;
  intervalSeconds?: number;
  /** kind=cumulative: what accumulated and what could not be. */
  cumulative?: {
    quantity: "events" | "sum" | "distinct";
    field: string | null;
    total: number;
    events: number;
    unparsed: number;
  };
  /** kind=calendar: the shown span and the week cap. */
  calendar?: {
    field: string | null;
    start: string | null;
    end: string | null;
    weeks: number;
    weeksTotal: number;
    truncated: boolean;
    dropped: number;
    total: number;
  };
  /** kind=change: the ranking's inputs and what the union cap dropped. */
  change?: {
    topN: number;
    unionSize: number;
    rowsShown: number;
    truncated: boolean;
    omitted: number;
    newCount: number;
    vanishedCount: number;
  };
  /** kind=lanes: the pairing rule's inputs and every cap. */
  lanes?: {
    pairing: "first_last" | "next_end";
    lanesShown: number;
    lanesTotal: number;
    laneCapHit: boolean;
    otherLanes: number;
    starts: number;
    ends: number;
    unpairedStarts: number;
    orphanEnds: number;
    rowsTruncated: boolean;
    rowsPaired: number;
    rowsCap: number;
    undated: number;
    sliceEnd: string | null;
  };
  /** kind=terms/timeseries: top-N truthfulness. */
  distinct?: number;
  shownValues?: number;
  otherCount?: number;
  /** kind=numeric: bin count over the value range. */
  binCount?: number;
  valueMin?: number | null;
  valueMax?: number | null;
  /** kind=numeric: how the bin count was chosen ("fd" = Freedman–Diaconis,
   * "fd_fallback" = the rule was undefined and a fixed default was used). */
  binRule?: "fd" | "fd_fallback" | "manual";
  /** kind=numeric: an "fd" count that hit the allowed bin-count clamp. */
  binCountClamped?: boolean;
  /** kind=numeric: population skewness g₁ (null when degenerate). */
  skewness?: number | null;
  /** Single focused value (e.g. the field-histogram modal's `field = value`
   * drill-down) — takes over the kind=time field line instead of the
   * generic "event count over time" phrasing. */
  focusedValue?: string;
  /** kind=pivot: per-axis top-N truthfulness (Other rollup). */
  xDistinct?: number;
  xShown?: number;
  yDistinct?: number;
  yShown?: number;
  /** kind=scatter: sample-size truthfulness. */
  sampledPoints?: number;
  totalPoints?: number;
  /** kind=scatter: server-computed correlation/regression block. */
  scatterStats?: ScatterStats | null;
  /** Grouped box/violin: grouping field and top-N truthfulness. */
  groupField?: string;
  /** Grouped box/violin: distinct values of the grouping field, for the
   * "this looks like an identifier" caution. */
  groupDistinct?: number;
  /** Grouped box/violin drawn as violins — widths need their reading spelled
   * out, since they are normalized per group. */
  groupedViolin?: boolean;
  groupsShown?: number;
  groupsOmitted?: number;
  /** kind=table: the share denominator (events with a non-empty value). */
  tableTotal?: number;
  /** kind=table: the row order the server applied. */
  tableSort?: { by: string; dir: string };
  /** kind=table: values whose rows are highlighted — presentation only. */
  tableHighlight?: string[];
  /** kind=table: what the top-N cut, reported as the remainder row. */
  tableRemainder?: { count: number; distinctValues: number };
  groupOmittedCount?: number;
  /** box/violin raw-value strip overlay: sample truthfulness. */
  overlayShown?: number;
  overlayTotal?: number;
  /** Mark-choice caution (e.g. a pie with too many/near-equal slices). */
  readabilityWarning?: string;
  /** kind=corr: which fields were correlated, and over how many events. */
  corrFields?: string[];
  corrPairs?: number;
  corrDropped?: string[];
  corrMinPairN?: number;
  corrMaxPairN?: number;
  /** Time-axis figures: the server's resolution of `config.marks`. */
  marks?: ResolvedMarksResponse;
  /** The time interval the figure actually draws (`lib/timeDomain.ts`) — what
   * lets the mark lines say how many marks fall outside it and are not
   * drawn. Omitted while the figure's own query has not answered. */
  marksDomain?: MarksDomain | null;
}

/** Distinct grouping values past which the grouping field reads as an
 * identifier. Mirrors the agent's VIZ_GROUP_CARDINALITY_CAUTION. */
const IDENTIFIER_LIKE_GROUP_COUNT = 50;

/** Shapiro–Wilk sample size past which "normality rejected" says more about
 * the sample size than about the data — the test's power grows with n, so it
 * starts flagging departures too small to change which coefficient to quote.
 * Matches the `shapiroWilk` explainer's "distrust" section. */
const SHAPIRO_LARGE_SAMPLE = 1000;

const fmtInt = (n: number) => n.toLocaleString("en-US");

function describeInterval(seconds: number): string {
  if (seconds % 86400 === 0 && seconds >= 86400) return `${seconds / 86400} d`;
  if (seconds % 3600 === 0 && seconds >= 3600) return `${seconds / 3600} h`;
  if (seconds % 60 === 0 && seconds >= 60) return `${seconds / 60} min`;
  return `${seconds} s`;
}

/** Compact, human-readable one-liner for a filter set (comparison layers,
 * primary-layer summaries) — never raw JSON in a forensic caption. */
export function describeFilters(filters: EventFilters): string {
  const parts: string[] = [];
  if (filters.q) parts.push(`search "${filters.q}"`);
  if (filters.artifact) parts.push(`artifact=${filters.artifact}`);
  for (const a of filters.artifacts ?? []) parts.push(`artifact=${a}`);
  if (filters.sourceId) parts.push(`source=${filters.sourceId}`);
  if (filters.tag) parts.push(`tag=${filters.tag}`);
  for (const t of filters.tagsInclude ?? []) parts.push(`tag=${t}`);
  for (const t of filters.tagsExclude ?? []) parts.push(`not tag=${t}`);
  for (const [k, vs] of Object.entries(filters.filters ?? {})) {
    for (const v of vs) parts.push(`${k}=${v}`);
  }
  for (const [k, vs] of Object.entries(filters.exclusions ?? {})) {
    for (const v of vs) parts.push(`${k}≠${v}`);
  }
  for (const t of filters.annotated ?? []) parts.push(`flagged:${t}`);
  return parts.length > 0 ? parts.join(" · ") : "no filters";
}

export function buildCaptionLines(args: {
  caseId: string | undefined;
  timelineId: string | undefined;
  chartLabel: string;
  config: ChartConfig;
  filters: EventFilters;
  facts: CaptionFacts;
  /** Overrides the "visualization" word in the header line (e.g. "field
   * histogram" for the per-value drill-down modal). */
  headerLabel?: string;
}): string[] {
  const {
    caseId,
    timelineId,
    chartLabel,
    config,
    filters,
    facts,
    headerLabel,
  } = args;
  const { field, scale, chartType, metric, compare } = config;
  const lines: (string | undefined)[] = [];

  lines.push(
    `Vestigo — ${headerLabel ?? "visualization"} — case ${caseId} / timeline ${timelineId ?? ""}`,
  );
  lines.push(
    facts.focusedValue != null && field
      ? `field: ${field} = ${facts.focusedValue}`
      : chartType === "lanes" && field
        ? `intervals of ${field} over time — ${chartLabel}`
        : chartType === "change" && field
          ? `share-of-window change of ${field}${
            config.derive ? ` (${scale} → ordered categories)` : ""
          } between two windows — ${chartLabel}`
        : chartType === "time"
        ? `event count over time — ${chartLabel}`
        : chartType === "punchcard"
          ? `event count by day-of-week × hour-of-day, UTC — ${chartLabel}`
          : chartType === "cumulative"
            ? `${
                facts.cumulative?.quantity === "sum"
                  ? `cumulative sum of ${field} (measure) over time`
                  : facts.cumulative?.quantity === "distinct"
                    ? `distinct values of ${field} seen so far`
                    : "cumulative event count over time"
              } — ${chartLabel}`
            : chartType === "calendar"
              ? `${field ? `events with a ${field} value` : "event count"} per day, UTC — ${chartLabel}`
              : field && config.fieldY
                ? `fields: ${field} × ${config.fieldY} — ${chartLabel}`
                : field && config.derive
                  ? `field: ${field} (${scale} → ordered categories) — ${chartLabel}`
                  : field
                    ? `field: ${field} (${scale}) — ${chartLabel}`
                    : undefined,
  );
  // A derived chart says what it did to the values and what it could not
  // count — a chart of ranges that did not name its edges is uncheckable.
  if (config.derive) {
    lines.push(
      `derived: ${describeDerive(config.derive, facts.derive)} — ${
        config.derive.kind === "bins"
          ? "values that do not parse as numbers are not counted"
          : "values that do not parse as timestamps are not counted; parts are UTC"
      }`,
    );
  }

  // Layer summaries: what each series is, with its total.
  const primaryDesc = describeFilters(filters);
  if (compare.mode !== "off") {
    lines.push(
      `primary: ${primaryDesc}` +
        (facts.primaryTotal != null
          ? ` — ${fmtInt(facts.primaryTotal)} events`
          : ""),
    );
    lines.push(
      compare.mode === "baseline"
        ? `comparison: all timeline events (same time range)` +
            (facts.comparisonTotal != null
              ? ` — ${fmtInt(facts.comparisonTotal)} events`
              : "")
        : `comparison: ${describeFilters(compare.filters)} (time range pinned to primary)` +
            (facts.comparisonTotal != null
              ? ` — ${fmtInt(facts.comparisonTotal)} events`
              : ""),
    );
  } else {
    if (filters.q) lines.push(`search: ${filters.q}`);
    if (facts.primaryTotal != null)
      lines.push(`${fmtInt(facts.primaryTotal)} events`);
  }

  // The ranked change encodes share, never count — say so before any
  // number, and say what the per-window top-N and the union cap hid.
  if (facts.change) {
    const c = facts.change;
    const nv =
      c.newCount || c.vanishedCount
        ? ` — ${fmtInt(c.newCount)} new, ${fmtInt(c.vanishedCount)} vanished`
        : "";
    lines.push(
      `ranked by |Δ share of window|, not by count; top ${fmtInt(c.topN)} values per window, ${fmtInt(c.unionSize)} in the union${nv}`,
    );
    if (c.truncated) {
      lines.push(
        `union capped at ${fmtInt(c.rowsShown)} of ${fmtInt(c.unionSize)} values; the ${fmtInt(c.omitted)} with the smallest change not drawn`,
      );
    }
  }

  // Interval lanes: the pairing rule in one sentence, then every cap — the
  // reader must be able to say what a bar means and what is not on the canvas.
  if (facts.lanes) {
    const l = facts.lanes;
    lines.push(
      l.pairing === "next_end"
        ? "pairing: start → next end — an end closes the most recent open start in its lane; an open start runs to the slice end; an end with no open start before it is an orphan, counted and not drawn"
        : "pairing: first to last — one bar per lane, from its first event to its last",
    );
    lines.push(
      l.laneCapHit
        ? `lanes: ${fmtInt(l.lanesShown)} shown of ${fmtInt(l.lanesTotal)} (top by event count); ${fmtInt(l.otherLanes)} more not drawn`
        : `lanes: ${fmtInt(l.lanesShown)}`,
    );
    if (l.pairing === "next_end") {
      // Two sentences because they are two scopes, and one sentence carrying
      // both is arithmetic nobody can reconcile: `starts`/`ends` are counted
      // over the whole union, before the lane cap, while the pairing runs only
      // over the lanes that survived it.
      lines.push(
        `starts: ${fmtInt(l.starts)} · ends: ${fmtInt(l.ends)} — matched across all ${fmtInt(l.lanesTotal)} lane${l.lanesTotal === 1 ? "" : "s"}, before the caps`,
      );
      lines.push(
        `paired over the ${fmtInt(l.lanesShown)} lane${l.lanesShown === 1 ? "" : "s"} drawn: ${fmtInt(l.unpairedStarts)} open-ended (no end seen, drawn to ${l.sliceEnd ?? "the slice end"}), ${fmtInt(l.orphanEnds)} orphan end${l.orphanEnds === 1 ? "" : "s"} not drawn`,
      );
      if (l.rowsTruncated) {
        lines.push(
          `first ${fmtInt(l.rowsPaired)} start/end events (by time) paired — the row cap; later ones not drawn`,
        );
      }
    }
    if (l.undated > 0) {
      lines.push(`${fmtInt(l.undated)} undated event${l.undated === 1 ? "" : "s"} not drawn`);
    }
  }

  if (filters.start || filters.end) {
    lines.push(`range: ${filters.start ?? "…"} to ${filters.end ?? "…"}`);
  }

  // Grid facts.
  if (facts.intervalSeconds != null && facts.intervalSeconds > 0) {
    lines.push(`${describeInterval(facts.intervalSeconds)} buckets, UTC`);
  }
  // A running total says what it reached and what it could not add — a sum
  // that silently skipped "n/a" is a smaller number than the reader assumes.
  if (facts.cumulative) {
    const c = facts.cumulative;
    const num = (n: number) =>
      Number.isInteger(n)
        ? fmtInt(n)
        : n.toLocaleString("en-US", { maximumFractionDigits: 2 });
    let line =
      c.quantity === "distinct"
        ? `${fmtInt(c.total)} distinct values over ${fmtInt(c.events)} events`
        : `final value ${num(c.total)} over ${fmtInt(c.events)} events`;
    if (c.unparsed > 0) {
      const n = `${fmtInt(c.unparsed)} event${c.unparsed === 1 ? "" : "s"}`;
      line +=
        c.quantity === "sum"
          ? `; ${n} with no numeric ${c.field} value not summed`
          : `; ${n} with an empty ${c.field} not counted`;
    }
    lines.push(line);
  }
  // The calendar names its day boundary and its cap: a figure that quietly
  // dropped the first eight weeks would read as "nothing happened then".
  if (facts.calendar) {
    const c = facts.calendar;
    if (c.start && c.end)
      lines.push(`${c.weeks} weeks, ${c.start} → ${c.end}, day boundaries UTC`);
    if (c.truncated) {
      lines.push(
        `latest ${c.weeks} of ${c.weeksTotal} weeks drawn; ${fmtInt(c.dropped)} earlier events not drawn`,
      );
    }
  }
  if (
    facts.binCount != null &&
    facts.valueMin != null &&
    facts.valueMax != null
  ) {
    // Each rule names itself exactly. Crediting Freedman–Diaconis for a fixed
    // fallback, or for a count the clamp overrode, would put a decision in the
    // caption that the data never made.
    const rule =
      facts.binRule === "fd"
        ? facts.binCountClamped
          ? " (Freedman–Diaconis, clamped to the allowed bin range)"
          : " (Freedman–Diaconis automatic width)"
        : facts.binRule === "fd_fallback"
          ? " (no interquartile spread — the automatic rule is undefined; fixed default)"
          : facts.binRule === "manual"
            ? " (manual)"
            : "";
    lines.push(
      `${facts.binCount} fixed-width bins over [${facts.valueMin.toLocaleString()}, ${facts.valueMax.toLocaleString()}]${rule}`,
    );
  }
  if (facts.skewness != null) {
    const g1 = facts.skewness;
    const reading =
      Math.abs(g1) < 0.5
        ? "approximately symmetric"
        : g1 > 0
          ? "right-skewed (long upper tail; mode < median < mean)"
          : "left-skewed (long lower tail; mean < median < mode)";
    lines.push(`skewness g₁ = ${g1.toFixed(2)} — ${reading}`);
  }

  // Truthfulness warnings.
  if (chartType === "table") {
    if (facts.tableSort) {
      const label =
        TABLE_COLUMN_LABELS[
          facts.tableSort.by as keyof typeof TABLE_COLUMN_LABELS
        ] ?? facts.tableSort.by;
      lines.push(
        `sorted by ${label} (${facts.tableSort.dir === "asc" ? "ascending" : "descending"})`,
      );
    }
    if (facts.tableTotal != null && field) {
      lines.push(
        `share = count / ${fmtInt(facts.tableTotal)} events with a non-empty ${field}`,
      );
    }
    if (
      facts.tableRemainder &&
      facts.distinct != null &&
      facts.shownValues != null
    ) {
      const more = facts.tableRemainder.distinctValues;
      lines.push(
        `showing top ${fmtInt(facts.shownValues)} of ${fmtInt(facts.distinct)} distinct values; ${fmtInt(facts.tableRemainder.count)} events across ${fmtInt(more)} more value${more === 1 ? "" : "s"} in the remainder row`,
      );
    }
    if (facts.tableHighlight?.length) {
      lines.push(
        `highlighted rows: ${facts.tableHighlight.join(" · ")} — presentation only`,
      );
    }
  }
  if (facts.marks && facts.marks.sources.length > 0) {
    lines.push(...markCaptionLines(facts.marks, facts.marksDomain));
  }
  if (
    chartType !== "table" &&
    facts.distinct != null &&
    facts.shownValues != null &&
    facts.distinct > facts.shownValues
  ) {
    lines.push(
      `showing top ${fmtInt(facts.shownValues)} of ${fmtInt(facts.distinct)} distinct values (capped` +
        (facts.otherCount != null && facts.otherCount > 0
          ? `; ${fmtInt(facts.otherCount)} events in "Other")`
          : ")"),
    );
  }
  if (
    facts.xDistinct != null &&
    facts.xShown != null &&
    facts.xDistinct > facts.xShown
  ) {
    lines.push(
      `x-axis: top ${fmtInt(facts.xShown)} of ${fmtInt(facts.xDistinct)} distinct values (rest in "Other")`,
    );
  }
  if (
    facts.yDistinct != null &&
    facts.yShown != null &&
    facts.yDistinct > facts.yShown
  ) {
    lines.push(
      `y-axis: top ${fmtInt(facts.yShown)} of ${fmtInt(facts.yDistinct)} distinct values (rest in "Other")`,
    );
  }
  if (
    facts.sampledPoints != null &&
    facts.totalPoints != null &&
    facts.totalPoints > facts.sampledPoints
  ) {
    lines.push(
      `showing ${fmtInt(facts.sampledPoints)} of ${fmtInt(facts.totalPoints)} points (uniform sample, stable across reruns; axes span full data)`,
    );
  }
  if (facts.groupField != null && facts.groupsShown != null) {
    lines.push(
      `grouped by ${facts.groupField}: ${fmtInt(facts.groupsShown)} group${facts.groupsShown === 1 ? "" : "s"} shown` +
        (facts.groupsOmitted
          ? `; ${fmtInt(facts.groupsOmitted)} smaller group${facts.groupsOmitted === 1 ? "" : "s"} omitted (${fmtInt(facts.groupOmittedCount ?? 0)} events), not merged into an "Other" group`
          : "") +
        " — all groups binned over the same value range",
    );
    if (
      facts.groupDistinct != null &&
      facts.groupDistinct > IDENTIFIER_LIKE_GROUP_COUNT
    ) {
      lines.push(
        `${facts.groupField} has ${fmtInt(facts.groupDistinct)} distinct values — that is usually an identifier rather than a grouping variable, and only the largest groups are drawn`,
      );
    }
    if (facts.groupedViolin) {
      // Widths are normalized per group, so they compare shapes, not sizes.
      // Without this line a narrow violin reads as "fewer events", which is
      // not what the mark encodes.
      lines.push(
        "violin widths show each group's own distribution shape (relative frequency), not its size — group sizes differ and are stated per group",
      );
    }
  }
  if (facts.overlayShown != null && facts.overlayTotal != null) {
    lines.push(
      facts.overlayShown < facts.overlayTotal
        ? `point overlay: showing ${fmtInt(facts.overlayShown)} of ${fmtInt(facts.overlayTotal)} values (uniform sample, stable across reruns)`
        : `point overlay: all ${fmtInt(facts.overlayShown)} values shown`,
    );
  }
  if (facts.scatterStats) {
    const s = facts.scatterStats;
    const fmtC = (v: number | null) => (v == null ? "n/a" : v.toFixed(3));
    lines.push(
      `Pearson r = ${fmtC(s.pearson.r)}, Spearman ρ = ${fmtC(s.spearman.rho)} over all ${s.n.toLocaleString("en-US")} pairs (ClickHouse)` +
        (s.regression?.slope != null && s.regression.intercept != null
          ? `; regression y ≈ ${s.regression.slope.toPrecision(4)}·x + ${s.regression.intercept.toPrecision(4)}, R² = ${fmtC(s.regression.r_squared)}`
          : ""),
    );
    lines.push(
      s.recommendation_basis === "shapiro"
        ? `recommended coefficient: ${s.recommendation === "pearson" ? "Pearson r" : "Spearman ρ"} (Shapiro–Wilk normality check on the ${s.shapiro.n.toLocaleString("en-US")}-point sample)` +
            // The test's power grows with n, so at these sample sizes it
            // rejects deviations too small to affect which coefficient to
            // quote. The explainer says so on screen; the export has to say
            // it too, or the caption reads as a finding about the data.
            (s.shapiro.n >= SHAPIRO_LARGE_SAMPLE
              ? ` — at this sample size Shapiro–Wilk flags even slight departures from normality, so read the verdict alongside the scatter's shape`
              : "")
        : // No normality verdict exists — say the coefficient is a fallback
          // rather than dressing an untested default as a recommendation.
          `normality could not be tested here; Spearman ρ shown as the conservative default`,
    );
  }
  if (facts.corrFields?.length) {
    lines.push(
      `${facts.corrPairs ?? 0} field pairs over ${facts.corrFields.length} fields: ${facts.corrFields.join(", ")}`,
    );
    if (facts.corrMinPairN != null && facts.corrMaxPairN != null) {
      lines.push(
        facts.corrMinPairN === facts.corrMaxPairN
          ? `each pair computed over ${fmtInt(facts.corrMinPairN)} events with both values (pairwise-complete)`
          : `pairs computed over ${fmtInt(facts.corrMinPairN)}–${fmtInt(facts.corrMaxPairN)} events with both values (pairwise-complete)`,
      );
    }
    if (facts.corrDropped?.length) {
      lines.push(
        `no numeric values under these filters: ${facts.corrDropped.join(", ")} — their cells are empty`,
      );
    }
    lines.push(
      "correlation is not causation; a coefficient near 0 rules out only the relationship it measures",
    );
  }
  if (facts.readabilityWarning)
    lines.push(`readability: ${facts.readabilityWarning}`);
  if (metric === "delta") lines.push("first bin omitted (Δ undefined)");
  if (metric === "ratio")
    lines.push(
      "bins with a zero-count comparison layer omitted (ratio undefined)",
    );

  // Metric formula.
  if (metric !== "count") {
    lines.push(
      `metric: ${METRIC_INFO[metric].label} = ${METRIC_INFO[metric].formula}`,
    );
  }

  return lines.filter((l): l is string => !!l);
}
