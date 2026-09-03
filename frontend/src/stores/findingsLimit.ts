/**
 * How many findings each method's page holds on the Investigate surface.
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

interface FindingsLimitState {
  byMethod: Partial<Record<MethodId, number>>;
  raise: (method: MethodId) => void;
}

export const useFindingsLimitStore = create<FindingsLimitState>()((set) => ({
  byMethod: {},
  raise: (method) =>
    set((s) => {
      const current = s.byMethod[method] ?? DEFAULT_FINDINGS_LIMIT;
      const next = FINDINGS_LIMIT_STEPS.find((step) => step > current);
      if (next === undefined) return s;
      return { byMethod: { ...s.byMethod, [method]: next } };
    }),
}));

export function limitOf(byMethod: Partial<Record<MethodId, number>>, method: MethodId): number {
  return byMethod[method] ?? DEFAULT_FINDINGS_LIMIT;
}

export function canRaise(limit: number): boolean {
  return FINDINGS_LIMIT_STEPS.some((step) => step > limit);
}
