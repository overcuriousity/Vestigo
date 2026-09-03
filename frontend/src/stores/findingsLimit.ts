/**
 * How many findings each findings *query* holds on the Investigate surface.
 *
 * Keyed by the query's own identity — method, scope and parameters — not by
 * method alone. The rail's configured detector and the sheet's method mode ask
 * different questions of the same method, and a page shared between them means
 * "Show more" on one silently re-runs the other: a heavy scan nobody asked for,
 * answering something the analyst was not reading.
 *
 * Session state, deliberately not persisted (the UI store persists everything
 * it holds): a page raised last week and still raised today would be the same
 * undisclosed state the rail exists to avoid, and the server answer at a
 * larger limit is a recompute the analyst should have asked for.
 *
 * The steps are the whole policy. `total_findings` is exact whatever the page
 * (docs/ANOMALY_DETECTION.md §"Totals and truncation"), so the page is a
 * reading choice, not a bound on what the analysis covered.
 */
import { create } from "zustand";
import type { MethodId } from "@/components/analysis/method-registry";

export const DEFAULT_FINDINGS_LIMIT = 50;
export const FINDINGS_LIMIT_STEPS: readonly number[] = [50, 80];

/** A findings query's page identity. Mirrors `findingsQueryOptions`' key. */
export type FindingsPageKey = string;

export function pageKeyOf(
  method: MethodId,
  scope: { frame: string; baseline_id?: string | null },
  params: Record<string, unknown>,
): FindingsPageKey {
  return [
    method,
    scope.frame,
    scope.baseline_id ?? "none",
    JSON.stringify(params),
  ].join("|");
}

interface FindingsLimitState {
  byKey: Partial<Record<FindingsPageKey, number>>;
  raise: (key: FindingsPageKey) => void;
}

export const useFindingsLimitStore = create<FindingsLimitState>()((set) => ({
  byKey: {},
  raise: (key) =>
    set((s) => {
      const current = s.byKey[key] ?? DEFAULT_FINDINGS_LIMIT;
      const next = FINDINGS_LIMIT_STEPS.find((step) => step > current);
      if (next === undefined) return s;
      return { byKey: { ...s.byKey, [key]: next } };
    }),
}));

export function limitOf(
  byKey: Partial<Record<FindingsPageKey, number>>,
  key: FindingsPageKey,
): number {
  return byKey[key] ?? DEFAULT_FINDINGS_LIMIT;
}

export function canRaise(limit: number): boolean {
  return FINDINGS_LIMIT_STEPS.some((step) => step > limit);
}
