/**
 * Derivations — a change of scale applied to the charted field before
 * aggregation. Two kinds, both yielding ordered categories: `bins` (number →
 * ranges) and `timePart` (a timestamp-valued field → hour/weekday/…). The
 * event time's own parts are the `time:` fields, which need no derivation.
 * Nothing here knows what an IP or a URL is. `docs/VISUALIZE.md` §"Derivations".
 */
import type { DeriveEcho } from "@/api/types";
import type { ChartType, DeriveSpec, Scale, TimePart } from "./chartConfig";
import { CHART_META, chartTypesFor } from "./chartMeta";
import { chartTypesForField, defaultChartTypeForScale } from "./chartOptions";
import { isTimeField } from "./timeFields";

export type { DeriveEcho };

export type DeriveKind = "bins" | "timePart";

/** Bin-count bounds, mirroring `db/derive.py`'s `BINS_MIN, BINS_MAX`. A custom
 * binning states the interior edges, so it takes one fewer than the ranges
 * they cut — the server refuses more with "at most 49 custom edges", which is
 * a 422 and a blank chart if the rail let the analyst paste them. */
export const BINS_MIN = 2;
export const BINS_MAX = 50;
export const EDGES_MAX = BINS_MAX - 1;

export const TIME_PART_LABELS: Record<TimePart, string> = {
  hour: "hour of day",
  weekday: "day of week",
  day: "day of month",
  week: "ISO week",
  month: "month",
};

/** Which derivations a field treated as *scale* offers — none for a `time:`
 * field (already a calendar part) or where the scale is already categories. */
export function deriveOptionsFor(scale: Scale, field: string | null): DeriveKind[] {
  if (!field || isTimeField(field)) return [];
  if (scale === "ratio") return ["bins"];
  if (scale === "interval") return ["bins", "timePart"];
  return [];
}

/** The figure a derivation would leave selected: *target* when it is still
 * legal at the derived scale, else the default figure for that scale. */
export function resolveDeriveTarget(
  target: ChartType,
  scale: Scale,
  field: string | null,
  next: DeriveSpec | null,
): ChartType {
  const eff = effectiveScale(scale, next);
  return chartTypesForField(eff, field).includes(target)
    ? target
    : defaultChartTypeForScale(eff, field);
}

/** The derivations the rail may offer on *chartType* — each one the figure it
 * would leave selected actually sends.
 *
 * Not simply `CHART_META[chartType].derives`. Applying a derivation is a
 * change of scale, and the rail re-picks the figure when the current one is
 * illegal at the derived one: a histogram admits no derivation of its own, yet
 * "Group into ranges" on a histogram is legal and lands on a bar, which does.
 * The figures that admit none *and* are legal at every scale (cumulative,
 * calendar, punchcard, time, corr) stay selected instead — they would carry a
 * `derive` they never put on the wire, while the caption, the export and a
 * Story snapshot all named it. Those are the ones withheld. */
export function deriveOptionsForChart(
  chartType: ChartType,
  scale: Scale,
  field: string | null,
): DeriveKind[] {
  return deriveOptionsFor(scale, field).filter((kind) =>
    CHART_META[
      resolveDeriveTarget(chartType, scale, field, defaultDerive(kind, scale))
    ].derives.includes(kind),
  );
}

/** A derived field is ordered categories, whatever it was treated as. */
export function effectiveScale(scale: Scale, derive: DeriveSpec | null): Scale {
  return derive ? "ordinal" : scale;
}

/** The treat-as values each derivation is computed from; the first is what an
 * unstated one resolves to. `deriveOptionsFor` read the other way round — the
 * Python twin is `chart_meta.DERIVE_SOURCE_SCALES`. */
const DERIVE_SOURCE_SCALES: Record<DeriveKind, Scale[]> = {
  bins: ["ratio", "interval"],
  timePart: ["interval"],
};

/** The `scale` a derived chart should carry on the page: *scale* if the
 * derivation admits it, else the derivation's natural one. "ordinal" is the
 * *effective* scale of every derived field (and all the agent contract took
 * for a while); as the treat-as it would hide the Derive control — the rail
 * offers no derivation on categories — so it resolves like an unstated one. */
export function deriveSourceScale(kind: DeriveKind, scale: Scale | null | undefined): Scale {
  const admitted = DERIVE_SOURCE_SCALES[kind];
  return scale && admitted.includes(scale) ? scale : admitted[0];
}

export function defaultDerive(kind: DeriveKind, scale: Scale): DeriveSpec {
  if (kind === "timePart") return { kind: "timePart", part: "hour" };
  return { kind: "bins", mode: scale === "ratio" ? "log" : "width", count: 8 };
}

/** The API's snake_case JSON for a derivation; `undefined` when there is none. */
export function deriveToParam(d: DeriveSpec | null): string | undefined {
  if (!d) return undefined;
  if (d.kind === "timePart") return JSON.stringify({ kind: "time_part", part: d.part });
  return JSON.stringify(
    d.mode === "custom"
      ? { kind: "bins", mode: "custom", edges: d.edges }
      : { kind: "bins", mode: d.mode, count: d.count },
  );
}

const fmt = (n: number) =>
  Number.isInteger(n)
    ? n.toLocaleString("en-US")
    : n.toLocaleString("en-US", { maximumSignificantDigits: 3 });

/** The caption's sentence for a derivation, with the resolved edges when the
 * response echoed them (width/log bins are computed from the data's range). */
export function describeDerive(d: DeriveSpec, echo?: DeriveEcho | null): string {
  if (d.kind === "timePart") return `calendar part: ${TIME_PART_LABELS[d.part]} (UTC)`;
  // The server formats the edges (`db/derive.py::_fmt_edges`) to the precision
  // that names each one rather than a number near it, and cuts the bin labels
  // at the same place. Rounding the floats here instead is how a caption comes
  // to say `4,000 – 4,001` under a bin that starts at 4000.125.
  const texts = echo?.edge_labels ?? echo?.edges?.map(fmt) ?? [];
  if (d.mode === "custom")
    return `grouped by your edges: ${(texts.length ? texts : d.edges.map(fmt)).join(" · ")}`;
  const tail = texts.length ? ` (edges: ${texts.join(" · ")})` : "";
  const neg = echo?.negative_bin ? "; values ≤ 0 in their own range" : "";
  // The edges the server could actually place, which over a range narrow
  // relative to its magnitude is fewer than asked for — float64 cannot
  // separate them (`db/derive.py::bin_edges`). Naming the requested count
  // under an axis with fewer ranges on it is the one thing this sentence
  // must not do.
  const actual = echo?.edges ? echo.edges.length + 1 : d.count;
  const short =
    actual < d.count
      ? ` — ${d.count} asked for; the values in this slice do not separate more`
      : "";
  return `grouped into ${actual} ${d.mode === "log" ? "log-spaced" : "equal-width"} range${actual === 1 ? "" : "s"}${tail}${neg}${short}`;
}

/** The one derivation that would make *chartType* legal for this field, or
 * null — when it is already legal, when no derivation lights it, or when two
 * could (the rail never guesses between them). */
export function singleFixFor(chartType: ChartType, scale: Scale, field: string | null): DeriveSpec | null {
  if (chartTypesForField(scale, field).includes(chartType)) return null;
  if (!chartTypesFor("ordinal").includes(chartType)) return null;
  const admitted = CHART_META[chartType].derives;
  const offered = deriveOptionsFor(scale, field).filter((k) => admitted.includes(k));
  if (offered.length !== 1) return null;
  return defaultDerive(offered[0], scale);
}
