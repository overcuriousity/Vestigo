/**
 * useMethodFindings / useStreamingSweep — one query per method.
 *
 * The old sweep issued eleven requests inside a single query, so the panel
 * could not render until the slowest returned. One query per method means each
 * result paints the moment it lands, and the rail's progress line is simply how
 * many have settled.
 *
 * Cheap methods are enabled immediately; heavy ones wait until the cheap set
 * settles, so the first paint is never queued behind a HEAVY_SCAN_GATE slot on
 * the server.
 *
 * The sweep uses `useQueries` rather than a loop of `useQuery` calls: a loop
 * would be a rules-of-hooks violation to suppress, and this is the API that
 * exists for a list of queries whose length the component does not control.
 *
 * Query keys deliberately live under the ["anomalies", case, timeline] prefix
 * with the method id at index 3. That is the shape `useDisposition`
 * optimistically rewrites when an analyst records a verdict; keying these
 * elsewhere would leave the row visibly unchanged after a click, which reads as
 * a dead button. The "dismissed-hidden" segment is part of the same contract.
 */
import { useMemo } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import {
  analysisApi,
  type MethodFindings,
  type MethodPlanEntry,
  type ScopeParams,
} from "@/api/analysis";
import { METHODS, type MethodId, type MethodMeta } from "@/components/analysis/method-registry";
import { HIDE_DISMISSED_KEY } from "@/components/analysis/detector-hooks";
import { useAnalysisPlan, useScopeParams } from "./useAnalysisPlan";

/** Per-method fetch cap, mirrored in the rail's coverage copy. */
export const METHOD_LIMIT = 50;

function findingsQueryOptions(
  caseId: string,
  timelineId: string,
  method: MethodId,
  scopeParams: ScopeParams,
  params: Record<string, unknown>,
  enabled: boolean,
) {
  return {
    queryKey: [
      "anomalies",
      caseId,
      timelineId,
      method,
      "analysis",
      scopeParams.frame,
      scopeParams.baseline_id ?? "none",
      JSON.stringify(params),
      METHOD_LIMIT,
      HIDE_DISMISSED_KEY,
    ],
    queryFn: () =>
      analysisApi.findings(caseId, timelineId, {
        ...scopeParams,
        method,
        params,
        limit: METHOD_LIMIT,
      }),
    enabled,
    // The server cache makes a repeat cheap and provably current, so the
    // client only needs to avoid refetch storms, not to hoard.
    staleTime: 5 * 60_000,
  };
}

/** One method on its own — the "run anyway" path, and the sheet's rerun. */
export function useMethodFindings(
  caseId: string,
  timelineId: string,
  method: MethodId,
  opts: { enabled: boolean; params?: Record<string, unknown> },
) {
  const scopeParams = useScopeParams();
  return useQuery<MethodFindings>(
    findingsQueryOptions(caseId, timelineId, method, scopeParams, opts.params ?? {}, opts.enabled),
  );
}

export interface MethodState {
  meta: MethodMeta;
  plan: MethodPlanEntry | undefined;
  status: MethodPlanEntry["status"];
  findings: MethodFindings["results"];
  total: number;
  isLoading: boolean;
  error: boolean;
  cache?: MethodFindings["cache"];
}

const CHEAP_IDS = METHODS.filter((m) => m.costClass === "cheap").map((m) => m.id);
const HEAVY_IDS = METHODS.filter((m) => m.costClass === "heavy").map((m) => m.id);

/** Run every applicable method, cheapest-first, reporting them as they settle. */
export function useStreamingSweep(caseId: string, timelineId: string) {
  const { planById, scope, isLoading: planLoading } = useAnalysisPlan(caseId, timelineId);
  const scopeParams = useScopeParams();

  const runnable = (id: MethodId) => !planLoading && planById[id]?.status === "applicable";

  const cheapResults = useQueries({
    queries: CHEAP_IDS.map((id) =>
      findingsQueryOptions(caseId, timelineId, id, scopeParams, {}, runnable(id)),
    ),
  });
  const cheapSettled = cheapResults.every((q) => !q.isLoading);

  const heavyResults = useQueries({
    queries: HEAVY_IDS.map((id) =>
      findingsQueryOptions(caseId, timelineId, id, scopeParams, {}, runnable(id) && cheapSettled),
    ),
  });

  const byMethod = useMemo(() => {
    const results = new Map<MethodId, (typeof cheapResults)[number]>();
    CHEAP_IDS.forEach((id, i) => results.set(id, cheapResults[i]));
    HEAVY_IDS.forEach((id, i) => results.set(id, heavyResults[i]));

    const out = {} as Record<MethodId, MethodState>;
    for (const meta of METHODS) {
      const query = results.get(meta.id);
      const data = query?.data as MethodFindings | undefined;
      const plan = planById[meta.id];
      out[meta.id] = {
        meta,
        plan,
        status: plan?.status ?? "applicable",
        findings: data?.results ?? [],
        total: data?.total_findings ?? 0,
        // A disabled query sits at `fetchStatus === "idle"` with
        // `isLoading === true`; without this guard every gated-off method
        // would render as loading forever.
        isLoading: Boolean(query?.isLoading) && query?.fetchStatus !== "idle",
        error: Boolean(query?.isError),
        cache: data?.cache,
      };
    }
    return out;
  }, [cheapResults, heavyResults, planById]);

  const expected = METHODS.filter((m) => runnable(m.id));
  const settled = expected.filter((m) => !byMethod[m.id].isLoading).length;

  return { byMethod, scope, done: settled, total: expected.length, planLoading };
}
