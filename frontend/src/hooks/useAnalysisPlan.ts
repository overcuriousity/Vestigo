/**
 * useAnalysisPlan — the gate's verdicts, with a deliberate fail-open.
 *
 * The plan is an optimisation: it decides what to run first and explains what
 * it did not run. If it fails, every method must still be reachable, so the
 * fallback marks all of them applicable and the panel behaves like the old
 * unconditional sweep. A broken gate may cost time; it may never quietly show
 * an analyst fewer methods than exist.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { analysisApi, type AnalysisScope, type MethodPlanEntry, type ScopeParams } from "@/api/analysis";
import { METHODS, type MethodId } from "@/components/analysis/method-registry";
import { useBaselineStore } from "@/stores/baseline";

const FAIL_OPEN: Record<MethodId, MethodPlanEntry> = Object.fromEntries(
  METHODS.map((m) => [
    m.id,
    {
      method: m.id,
      status: "applicable" as const,
      reason: "",
      reason_facts: {},
      cost_class: m.costClass,
    },
  ]),
) as Record<MethodId, MethodPlanEntry>;

/** Resolve the scope params every analysis request carries, from the store. */
export function useScopeParams(): ScopeParams {
  const frame = useBaselineStore((s) => s.frame);
  const activeBaselineId = useBaselineStore((s) => s.activeBaselineId);
  return frame === "baseline" && activeBaselineId
    ? { frame: "baseline", baseline_id: activeBaselineId }
    : { frame: "self" };
}

export function useAnalysisPlan(caseId: string, timelineId: string) {
  const scopeParams = useScopeParams();

  const query = useQuery({
    queryKey: [
      "analysis-plan",
      caseId,
      timelineId,
      scopeParams.frame,
      scopeParams.baseline_id ?? "none",
    ],
    queryFn: () => analysisApi.plan(caseId, timelineId, scopeParams),
    // The plan reads cached stats plus one cheap probe; a minute of staleness
    // is invisible next to how long a scan takes.
    staleTime: 60_000,
    retry: 1,
  });

  // Memoized on the query's own data, which react-query keeps referentially
  // stable between fetches. A fresh object spread per render would make every
  // hook downstream (`useStreamingSweep`'s `runnable` and `byMethod`, and the
  // rail's markers built from them) churn identity on every render too — which
  // is a re-render loop the moment any of it feeds a parent's setState.
  const planById = useMemo(
    () =>
      query.data
        ? ({
            ...FAIL_OPEN,
            ...Object.fromEntries(query.data.methods.map((m) => [m.method, m])),
          } as Record<MethodId, MethodPlanEntry>)
        : FAIL_OPEN,
    [query.data],
  );

  const scope: AnalysisScope = useMemo(
    () =>
      query.data?.scope ?? {
        frame: scopeParams.frame,
        baseline_id: scopeParams.baseline_id ?? null,
        baseline_name: null,
      },
    [query.data, scopeParams.frame, scopeParams.baseline_id],
  );

  return {
    plan: query.data,
    planById,
    scope,
    isLoading: query.isLoading,
    failedOpen: query.isError,
    scopeParams,
    eventsTotal: query.data?.events_total ?? 0,
  };
}
