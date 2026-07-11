/**
 * Single source of truth for chart captions — the same lines render under
 * the chart (`ChartCaption`) and embed into SVG/PNG exports
 * (`ExportControls.captionLines`), so what the analyst reads on screen is
 * exactly what a report reader sees. Includes the truthfulness warnings
 * (top-N capping, undefined metric bins) forensic rigor demands.
 */
import type { EventFilters } from "@/api/types";
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
    `TraceSignal — ${headerLabel ?? "visualization"} — case ${caseId} / timeline ${timelineId ?? ""}`,
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
    lines.push(
      `${facts.binCount} fixed-width bins over [${facts.valueMin.toLocaleString()}, ${facts.valueMax.toLocaleString()}]`,
    );
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
  if (metric === "delta") lines.push("first bin omitted (Δ undefined)");
  if (metric === "ratio") lines.push("bins with a zero-count comparison layer omitted (ratio undefined)");

  // Metric formula.
  if (metric !== "count") {
    lines.push(`metric: ${METRIC_INFO[metric].label} = ${METRIC_INFO[metric].formula}`);
  }

  return lines.filter((l): l is string => !!l);
}
