/**
 * The plain-language sentence the wizard's confirm step shows, kept pure so
 * it can be tested without rendering: it is the one place the analyst reads
 * back what is about to be stored, so it has to say every part of it.
 */
import type { MethodId, MethodMeta } from "./method-registry";

/** Methods that compare a baseline window against suspect windows — no self frame. */
export const NEEDS_BASELINE: ReadonlySet<MethodId> = new Set<MethodId>([
  "proportion_shift",
  "value_distribution_drift",
  "interval_periodicity",
  "sequence_novelty",
]);

function fieldsClause(params: Record<string, unknown>): string | null {
  const raw = params.fields;
  if (raw === undefined || raw === null) return null;
  const list = Array.isArray(raw) ? raw.map(String) : String(raw).split(",");
  if (list.length === 0) return null;
  if (list.length === 1) return list[0];
  return `${list.slice(0, -1).join(", ")} and ${list[list.length - 1]}`;
}

export function summarize(
  meta: MethodMeta,
  params: Record<string, unknown>,
  frame: "self" | "baseline",
  baselineName: string | null,
): string {
  const parts: string[] = [];
  const hasFieldsKnob = meta.knobs.some((k) => k.kind === "fields");
  if (hasFieldsKnob) {
    parts.push(`over ${fieldsClause(params) ?? "fields Vestigo picks"}`);
  }
  if (typeof params.series_field === "string" && params.series_field) {
    parts.push(`per ${params.series_field}`);
  }
  for (const knob of meta.knobs) {
    if (knob.kind !== "number") continue;
    const v = params[knob.param];
    if (v === undefined || v === null || v === "") continue;
    parts.push(`${knob.label.toLowerCase()} ${v}`);
  }
  const scope =
    frame === "baseline"
      ? `comparing to baseline “${baselineName ?? "(unnamed)"}”`
      : "across the whole timeline";
  const cost = meta.costClass === "heavy" ? "Full scan." : "Cheap scan.";
  const head = parts.length ? `${meta.label} ${parts.join(", ")}` : meta.label;
  return `${head}, ${scope}. ${cost}`;
}
