/**
 * useMethodFindings / useStreamingSweep — one query per configured detector.
 *
 * Nothing runs unprompted. The sweep runs exactly the entries on
 * `Timeline.detectors` (see `useTimelineDetectors`), each with its own stored
 * params and its own scope, through the same findings endpoint and server
 * cache as any ad hoc run. The analysis plan is fetched only to advise — the
 * wizard's cards and the Tools accounting read it; it never decides what runs.
 *
 * One query per entry means each result paints the moment it lands, and the
 * rail's progress line is simply how many have settled.
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
import { useCallback, useMemo } from "react";
import { useQueries, useQuery } from "@tanstack/react-query";
import {
  analysisApi,
  type MethodFindings,
  type MethodPlanEntry,
  type ScopeParams,
} from "@/api/analysis";
import { METHODS, type MethodId, type MethodMeta } from "@/components/analysis/method-registry";
import {
  HIDE_DISMISSED_KEY,
  SHOW_DISMISSED_KEY,
} from "@/components/analysis/detector-hooks";
import { useUiStore } from "@/stores/ui";
import { useAnalysisPlan, useScopeParams } from "./useAnalysisPlan";
import { scopeOf, useTimelineDetectors, type KnownDetectorEntry } from "./useTimelineDetectors";

/** Per-method fetch cap, mirrored in the rail's coverage copy. */
export const METHOD_LIMIT = 50;

/**
 * The reveal toggle, as one store flag rather than per-view state.
 *
 * The rail and the sheet render the same finding set from the same query keys,
 * so a per-component toggle would show a finding as dismissed in one and gone
 * in the other. The named key segment (not the raw boolean) is what
 * `useDisposition` scans to pick its optimistic-update branch.
 */
export function useIncludeDismissed() {
  const includeDismissed = useUiStore((s) => s.includeDismissedFindings);
  const setIncludeDismissed = useUiStore((s) => s.setIncludeDismissedFindings);
  return { includeDismissed, setIncludeDismissed };
}

function findingsQueryOptions(
  caseId: string,
  timelineId: string,
  method: MethodId,
  scopeParams: ScopeParams,
  params: Record<string, unknown>,
  enabled: boolean,
  includeDismissed: boolean,
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
      includeDismissed ? SHOW_DISMISSED_KEY : HIDE_DISMISSED_KEY,
    ],
    queryFn: () =>
      analysisApi.findings(caseId, timelineId, {
        ...scopeParams,
        method,
        params,
        limit: METHOD_LIMIT,
        ...(includeDismissed ? { include_dismissed: true } : {}),
      }),
    enabled,
    // The server cache makes a repeat cheap and provably current, so the
    // client only needs to avoid refetch storms, not to hoard.
    staleTime: 5 * 60_000,
  };
}

/**
 * One method on its own — the sheet's ad hoc run, or a finding addressed by
 * the rail. `scope` overrides the panel scope: a finding the rail fetched under
 * a configured entry must be opened under that entry's scope, or the sheet
 * reads a different list than the row that was clicked.
 */
export function useMethodFindings(
  caseId: string,
  timelineId: string,
  method: MethodId,
  opts: { enabled: boolean; params?: Record<string, unknown>; scope?: ScopeParams },
) {
  const globalScope = useScopeParams();
  const scopeParams = opts.scope ?? globalScope;
  const { includeDismissed } = useIncludeDismissed();
  return useQuery<MethodFindings>(
    findingsQueryOptions(
      caseId,
      timelineId,
      method,
      scopeParams,
      opts.params ?? {},
      opts.enabled,
      includeDismissed,
    ),
  );
}

export interface MethodState {
  meta: MethodMeta;
  plan: MethodPlanEntry | undefined;
  status: MethodPlanEntry["status"];
  findings: MethodFindings["results"];
  total: number;
  isLoading: boolean;
  /**
   * Expected to produce an answer, and hasn't yet. Distinct from `isLoading`:
   * a heavy method waiting behind the cheap set is not fetching but is very
   * much not settled, and counting it as done is what made the progress bar
   * finish before half the sweep had started.
   */
  pending: boolean;
  error: boolean;
  cache?: MethodFindings["cache"];
  /**
   * What the *run* concluded, as distinct from what the plan predicted.
   * `insufficient_data` / `no_data` mean the method scanned and could not
   * score — which is not the same claim as a clean zero, and rendering the two
   * identically is the "checked, clear" misread the rail is built to avoid.
   */
  dataStatus?: string;
  /** Caveats the runner attached to an answer it did produce. */
  warnings: string[];
  /**
   * Re-run *this* query — the configured entry's params under its own scope.
   * The Tools sheet's Retry needs it: routing that button through an ad hoc
   * run would ask a different question (empty params, the panel scope) than
   * the one that failed, and could report success while the rail's chip still
   * showed the failure.
   */
  refetch: () => void;
  /** The stored entry this method runs under; absent when not configured. */
  entry?: KnownDetectorEntry;
  configured: boolean;
  /**
   * Whether `plan`/`status` are about *this* detector's question.
   *
   * The gate is fetched once, under the panel scope. A configured entry runs
   * under its own frame, so when the two disagree the plan's verdict answers a
   * run nobody asked for — a baseline-framed detector reads as "needs a
   * baseline" while the rail is showing the findings it produced from the
   * baseline it already has. Advice about a different question is withheld
   * rather than restated.
   */
  planApplies: boolean;
}

const CHEAP_IDS = METHODS.filter((m) => m.costClass === "cheap").map((m) => m.id);
const HEAVY_IDS = METHODS.filter((m) => m.costClass === "heavy").map((m) => m.id);

/** Run every configured detector, cheapest-first, reporting them as they settle. */
export function useStreamingSweep(caseId: string, timelineId: string) {
  const { planById, scope, isLoading: planLoading } = useAnalysisPlan(caseId, timelineId);
  const scopeParams = useScopeParams();
  const { includeDismissed } = useIncludeDismissed();

  // A configured entry is the whole decision: not the plan (advice, never a
  // lock — a not_applicable method an analyst configured anyway still runs),
  // not a mute, not a per-user focus. One list, shared, audited.
  const { entries, byMethod: entryByMethod } = useTimelineDetectors(caseId, timelineId);
  const runnable = useCallback((id: MethodId) => entryByMethod.has(id), [entryByMethod]);

  const optionsFor = (id: MethodId, enabled: boolean) => {
    const entry = entryByMethod.get(id);
    return findingsQueryOptions(
      caseId,
      timelineId,
      id,
      entry ? scopeOf(entry) : scopeParams,
      entry?.params ?? {},
      enabled && entry !== undefined,
      includeDismissed,
    );
  };

  const cheapResults = useQueries({ queries: CHEAP_IDS.map((id) => optionsFor(id, true)) });
  // Settled, not "not loading": a disabled query is neither, and asking
  // `isFetched` says what this actually means without depending on how
  // react-query happens to derive `isLoading` from pending/fetching.
  const cheapSettled = cheapResults.every(
    (q, i) => !runnable(CHEAP_IDS[i]) || q.isFetched || q.isError,
  );

  const heavyResults = useQueries({
    queries: HEAVY_IDS.map((id) => optionsFor(id, cheapSettled)),
  });

  // `useQueries` returns a fresh array (and fresh result objects) on every
  // render, so memoizing on it memoizes nothing. Depend instead on the four
  // fields actually read: `data` is referentially stable between fetches and
  // the rest are primitives. The dependency list has a constant length —
  // METHODS is a module constant — which is what makes spreading it legal.
  //
  // This stability is load-bearing: InvestigateRail derives histogram markers
  // from `byMethod` and publishes them into ExplorerPage state. A `byMethod`
  // that changed identity every render would make that an infinite loop.
  const queryDeps = [...cheapResults, ...heavyResults].flatMap((q) => [
    q.data,
    q.isFetching,
    q.isFetched,
    q.isError,
  ]);

  // Primitives, not the object: `useScopeParams` builds a fresh one per render,
  // so depending on it would churn `byMethod`'s identity every render — which
  // the comment above `queryDeps` explains is a render loop, not a slowdown.
  const panelFrame = scopeParams.frame;
  const panelBaseline = scopeParams.baseline_id ?? null;

  const byMethod = useMemo(() => {
    const results = new Map<MethodId, (typeof cheapResults)[number]>();
    CHEAP_IDS.forEach((id, i) => results.set(id, cheapResults[i]));
    HEAVY_IDS.forEach((id, i) => results.set(id, heavyResults[i]));

    const out = {} as Record<MethodId, MethodState>;
    for (const meta of METHODS) {
      const query = results.get(meta.id);
      const data = query?.data as MethodFindings | undefined;
      const plan = planById[meta.id];
      const entry = entryByMethod.get(meta.id);
      const entryScope = entry ? scopeOf(entry) : null;
      const planApplies =
        !entryScope ||
        (entryScope.frame === panelFrame &&
          (entryScope.baseline_id ?? null) === panelBaseline);
      out[meta.id] = {
        meta,
        plan,
        status: plan?.status ?? "applicable",
        findings: data?.results ?? [],
        total: data?.total_findings ?? 0,
        isLoading: Boolean(query?.isFetching),
        // A method the plan says to run is pending until its query has
        // actually produced something — including while it is still disabled
        // behind `cheapSettled`, which is exactly the window a naive
        // "not loading" check reported as finished.
        pending: runnable(meta.id) && !query?.isFetched && !query?.isError,
        error: Boolean(query?.isError),
        cache: data?.cache,
        dataStatus: data?.status,
        warnings: data?.warnings ?? [],
        refetch: () => void query?.refetch(),
        entry,
        configured: entry !== undefined,
        planApplies,
      };
    }
    return out;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- see queryDeps above
  }, [...queryDeps, planById, runnable, entryByMethod, panelFrame, panelBaseline]);

  const expected = METHODS.filter((m) => runnable(m.id));
  const settled = expected.filter((m) => !byMethod[m.id].pending).length;

  return {
    byMethod,
    scope,
    done: settled,
    total: expected.length,
    planLoading,
    planById,
    configured: entries,
  };
}
