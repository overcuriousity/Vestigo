/**
 * useScopeChange — hold a requested scope change until the analyst confirms it.
 *
 * The request comes from the Tools sheet; nothing touches the baseline store
 * until confirmation, so cancelling genuinely leaves the findings on screen
 * untouched rather than reverting a change that already ran.
 *
 * The counts the dialog quotes are computed here: how many methods will re-run
 * (every applicable one — the fingerprint moves for all of them at once) and
 * how many verdicts were recorded under the scope being left behind.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { dispositionsApi } from "@/api/dispositions";
import { useBaselineStore } from "@/stores/baseline";
import { METHODS } from "@/components/analysis/method-registry";
import { useAnalysisPlan } from "./useAnalysisPlan";

export interface PendingScope {
  frame: "self" | "baseline";
  baselineId?: string;
  baselineName?: string | null;
}

export function useScopeChange(caseId: string, timelineId: string) {
  const [pending, setPending] = useState<PendingScope | null>(null);
  const { planById, scope } = useAnalysisPlan(caseId, timelineId);
  const setFrame = useBaselineStore((s) => s.setFrame);
  const setActiveBaselineId = useBaselineStore((s) => s.setActiveBaselineId);

  const { data: dispositions } = useQuery({
    queryKey: ["dispositions", caseId, timelineId, "all"],
    queryFn: () => dispositionsApi.list(caseId, timelineId),
  });

  // Verdicts reached under the scope being left. Rows written before scope
  // provenance existed carry none and are not counted — claiming them would be
  // asserting something the database does not record.
  const affectedVerdicts = useMemo(() => {
    const rows = dispositions?.dispositions ?? [];
    return rows.filter((d) => {
      const recorded = (d as { analysis_scope?: { frame?: string; baseline_id?: string | null } })
        .analysis_scope;
      if (!recorded) return false;
      return (
        recorded.frame === scope.frame &&
        (recorded.baseline_id ?? null) === (scope.baseline_id ?? null)
      );
    }).length;
  }, [dispositions, scope]);

  const methodsToRerun = METHODS.filter((m) => planById[m.id]?.status === "applicable").length;

  return {
    pending,
    request: (next: PendingScope) => setPending(next),
    cancel: () => setPending(null),
    confirm: () => {
      if (!pending) return;
      setFrame(pending.frame);
      setActiveBaselineId(pending.frame === "baseline" ? (pending.baselineId ?? null) : null);
      setPending(null);
    },
    methodsToRerun,
    affectedVerdicts,
    currentScope: scope,
  };
}
