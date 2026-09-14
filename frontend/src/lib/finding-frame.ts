/**
 * finding-frame — which frame a finding was computed under, read off the
 * `details.method` the detectors stamp into every finding.
 *
 * A baseline never restricts a detector (ROADMAP D18): every method runs with
 * or without one, and the four that used to be baseline-only now carry a self
 * mode of their own. The `details` shape differs between the two frames — a
 * self-mode finding has no `baseline_*` key, only `rest_*` or whole-scope
 * numbers — so the surfaces that read details branch here rather than on the
 * scope the panel happens to be in: a finding is what its run says it is.
 */
import type { AnomalyFinding } from "@/api/types";

/** Modes learned from a baseline window and scored on suspect windows. */
export const TEMPORAL_MODES: ReadonlySet<string> = new Set([
  "temporal",
  "temporal-z-score",
  "temporal-range",
  "temporal-charset",
  "temporal-iqr",
  "temporal-bigram-iqr",
  "g-test",
  "cadence",
  "ngram",
  "drift",
]);

/** The self modes of the four formerly baseline-only methods. */
export const SELF_MODES: ReadonlySet<string> = new Set([
  "self-g-test",
  "self-drift",
  "self-cadence",
  "rare-ngram",
]);

/** The two self modes that compare leave-one-out time slices with the rest of the scope. */
export const SLICE_MODES: ReadonlySet<string> = new Set(["self-g-test", "self-drift"]);

export function isTemporalMode(method: string): boolean {
  return TEMPORAL_MODES.has(method);
}

/** The mode a finding's run recorded, or null when the payload predates the stamp. */
export function findingMode(finding: { details: Record<string, unknown> }): string | null {
  const m = finding.details["method"];
  return typeof m === "string" ? m : null;
}

export function isSelfModeFinding(finding: { details: Record<string, unknown> }): boolean {
  const m = findingMode(finding);
  return m !== null && SELF_MODES.has(m);
}

/**
 * End of the slice a slice-mode finding was attributed to, for the range
 * highlight — the same affordance a frequency finding's window gets. Temporal
 * windows already render as histogram bands, so they are not offered here.
 */
export function sliceWindowEnd(finding: AnomalyFinding): string | undefined {
  if (finding.type === "frequency") return finding.window_end;
  const m = findingMode(finding);
  if (m === null || !SLICE_MODES.has(m)) return undefined;
  const end = finding.details["window_end"];
  return typeof end === "string" ? end : undefined;
}

/** A number out of `details`, or null when absent or not numeric. */
export function detailNumber(details: Record<string, unknown>, key: string): number | null {
  const v = details[key];
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

/** A string out of `details`, or null when absent. */
export function detailString(details: Record<string, unknown>, key: string): string | null {
  const v = details[key];
  return typeof v === "string" ? v : null;
}
