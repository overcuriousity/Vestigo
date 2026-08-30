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
import { chartTypesForField } from "./chartOptions";
import { isTimeField } from "./timeFields";

export type { DeriveEcho };

export type DeriveKind = "bins" | "timePart";

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
  if (d.mode === "custom") return `grouped by your edges: ${d.edges.map(fmt).join(" · ")}`;
  const edges = echo?.edges ?? [];
  const tail = edges.length ? ` (edges: ${edges.map(fmt).join(" · ")})` : "";
  const neg = echo?.negative_bin ? "; values ≤ 0 in their own range" : "";
  return `grouped into ${d.count} ${d.mode === "log" ? "log-spaced" : "equal-width"} ranges${tail}${neg}`;
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
