/**
 * Single source of truth for chart captions — the same lines render under
 * the chart (`ChartCaption`) and embed into SVG/PNG exports
 * (`ExportControls.captionLines`), so what the analyst reads on screen is
 * exactly what a report reader sees. Includes the truthfulness warnings
 * (top-N capping, undefined metric bins) forensic rigor demands.
 */
import type { EventFilters, ScatterStats } from "@/api/types";
import type { ChartConfig } from "./chartConfig";
import { METRIC_INFO } from "./transforms";

/** Data-derived facts the active query contributes to the caption. */
export interface CaptionFacts {
  /** kind=time: layer totals + resolved bucket width. */
  primaryTotal?: number;
  comparisonTotal?: number;
  intervalSeconds?: number;
  /** kind=terms/timeseries: top-N truthfulness. */
  distinct?: number;
  shownValues?: number;
  otherCount?: number;
  /** kind=numeric: bin count over the value range. */
  binCount?: number;
  valueMin?: number | null;
  valueMax?: number | null;
  /** kind=numeric: how the bin count was chosen ("fd" = Freedman–Diaconis). */
  binRule?: "fd" | "manual";
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
  groupsShown?: number;
  groupsOmitted?: number;
  groupOmittedCount?: number;
  /** box/violin raw-value strip overlay: sample truthfulness. */
  overlayShown?: number;
  overlayTotal?: number;
  /** Mark-choice caution (e.g. a pie with too many/near-equal slices). */
  readabilityWarning?: string;
  /** Facet grid: the splitting field and its top-N truthfulness. */
  facetField?: string;
  facetPanels?: number;
  facetOmittedValues?: number;
  facetOmittedCount?: number;
  /** kind=corr: which fields were correlated, and over how many events. */
  corrFields?: string[];
  corrPairs?: number;
  corrDropped?: string[];
  corrMinPairN?: number;
  corrMaxPairN?: number;
}

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
  const { caseId, timelineId, chartLabel, config, filters, facts, headerLabel } = args;
  const { field, scale, chartType, metric, compare } = config;
  const lines: (string | undefined)[] = [];

  lines.push(
    `Vestigo — ${headerLabel ?? "visualization"} — case ${caseId} / timeline ${timelineId ?? ""}`,
  );
  lines.push(
    facts.focusedValue != null && field
      ? `field: ${field} = ${facts.focusedValue}`
      : chartType === "time"
        ? `event count over time — ${chartLabel}`
        : chartType === "punchcard"
          ? `event count by day-of-week × hour-of-day, UTC — ${chartLabel}`
          : field && config.fieldY
            ? `fields: ${field} × ${config.fieldY} — ${chartLabel}`
            : field
              ? `field: ${field} (${scale}) — ${chartLabel}`
              : undefined,
  );

  // Layer summaries: what each series is, with its total.
  const primaryDesc = describeFilters(filters);
  if (compare.mode !== "off") {
    lines.push(
      `primary: ${primaryDesc}` +
        (facts.primaryTotal != null ? ` — ${fmtInt(facts.primaryTotal)} events` : ""),
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
    if (facts.primaryTotal != null) lines.push(`${fmtInt(facts.primaryTotal)} events`);
  }

  if (filters.start || filters.end) {
    lines.push(`range: ${filters.start ?? "…"} to ${filters.end ?? "…"}`);
  }

  // Grid facts.
  if (facts.intervalSeconds != null && facts.intervalSeconds > 0) {
    lines.push(`${describeInterval(facts.intervalSeconds)} buckets, UTC`);
  }
  if (facts.binCount != null && facts.valueMin != null && facts.valueMax != null) {
    const rule =
      facts.binRule === "fd"
        ? " (Freedman–Diaconis automatic width)"
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
  if (
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
  if (facts.xDistinct != null && facts.xShown != null && facts.xDistinct > facts.xShown) {
    lines.push(
      `x-axis: top ${fmtInt(facts.xShown)} of ${fmtInt(facts.xDistinct)} distinct values (rest in "Other")`,
    );
  }
  if (facts.yDistinct != null && facts.yShown != null && facts.yDistinct > facts.yShown) {
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
      `showing ${fmtInt(facts.sampledPoints)} of ${fmtInt(facts.totalPoints)} points (uniform random sample; axes span full data)`,
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
  }
  if (facts.overlayShown != null && facts.overlayTotal != null) {
    lines.push(
      facts.overlayShown < facts.overlayTotal
        ? `point overlay: showing ${fmtInt(facts.overlayShown)} of ${fmtInt(facts.overlayTotal)} values (uniform random sample)`
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
      `recommended coefficient: ${s.recommendation === "pearson" ? "Pearson r" : "Spearman ρ"} (Shapiro–Wilk normality check on the ${s.shapiro.n.toLocaleString("en-US")}-point sample)`,
    );
  }
  if (facts.facetField != null && facts.facetPanels != null) {
    lines.push(
      `split into ${fmtInt(facts.facetPanels)} panel${facts.facetPanels === 1 ? "" : "s"} by ${facts.facetField}` +
        (facts.facetOmittedValues
          ? `; ${fmtInt(facts.facetOmittedValues)} further value${facts.facetOmittedValues === 1 ? "" : "s"}` +
            (facts.facetOmittedCount
              ? ` (${fmtInt(facts.facetOmittedCount)} events)`
              : "") +
            ' omitted, not merged into an "Other" panel'
          : ""),
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
    lines.push("correlation is not causation; a coefficient near 0 rules out only the relationship it measures");
  }
  if (facts.readabilityWarning) lines.push(`readability: ${facts.readabilityWarning}`);
  if (metric === "delta") lines.push("first bin omitted (Δ undefined)");
  if (metric === "ratio") lines.push("bins with a zero-count comparison layer omitted (ratio undefined)");

  // Metric formula.
  if (metric !== "count") {
    lines.push(`metric: ${METRIC_INFO[metric].label} = ${METRIC_INFO[metric].formula}`);
  }

  return lines.filter((l): l is string => !!l);
}
