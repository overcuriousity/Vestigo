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
  //
  // Only `confirmed` counts. `normal`/`dismissed`/`routine` are standing
  // declarations about a value, effective under every frame (see
  // `postgres.py::create_disposition`), so they are never re-examined on a
  // scope change and quoting them would inflate a number the dialog exists to
  // state exactly.
  const affectedVerdicts = useMemo(() => {
    const rows = dispositions?.dispositions ?? [];
    return rows.filter((d) => {
      if (d.kind !== "confirmed") return false;
      const recorded = d.analysis_scope;
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
    // A baseline frame with no definition is not a scope change — the store
    // resets the frame to `self` when the id is cleared, so confirming it would
    // promise a re-run against a different comparison and then change nothing.
    // Refuse it here as well as in the UI: the dialog must never offer a
    // confirm that cannot take effect.
    request: (next: PendingScope) => {
      if (next.frame === "baseline" && !next.baselineId) return;
      setPending(next);
    },
    cancel: () => setPending(null),
    confirm: () => {
      if (!pending) return;
      if (pending.frame === "baseline") {
        // Setting the id implies the frame (see the baseline store).
        setActiveBaselineId(pending.baselineId ?? null);
      } else {
        // Only the frame. Clearing the id as well would discard the analyst's
        // chosen definition, so a baseline → self → baseline round-trip would
        // land on "Pick a baseline…" and the builder instead of switching back
        // to the comparison they were just looking at. `useScopeParams` reads
        // the frame first, so a retained id changes nothing while in `self`.
        setFrame("self");
      }
      setPending(null);
    },
    methodsToRerun,
    affectedVerdicts,
    currentScope: scope,
  };
}
