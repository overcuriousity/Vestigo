/**
 * Starting a column recommendation, and the consent that gates its AI half
 * (issue #213).
 *
 * Two callers need all of this and must not drift: the Columns picker, where
 * an analyst asks for a suggestion by hand, and the Explorer's one-time offer
 * on a timeline nobody has answered for yet. Both have to record the same
 * per-timeline answer, run the same job, and clear the same local override —
 * a second copy of any of that is a second set of rules for when evidence
 * leaves the machine.
 *
 * **An explicit request adopts its own result.** A per-user column choice
 * normally outranks the timeline's suggestion, so an automatic recompute — a
 * colleague finishing an ingest — never moves anyone's columns. But pressing
 * "Re-suggest columns" *is* asking for the new answer, and leaving the stored
 * override in place made the button look broken: the job ran, the suggestion
 * changed, and the grid kept showing the columns it had. So every run started
 * from here drops this browser's override (`lib/columns.ts` precedence 1) and
 * lets the fresh answer through.
 */
import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { timelinesApi } from "@/api/timelines";
import { COLUMN_ADVISOR_OPTIN } from "@/lib/columns";
import { useAuthStore } from "@/stores/auth";
import { useJobsStore } from "@/stores/jobs";
import { useUiStore } from "@/stores/ui";

/**
 * Where a failure of the shared run mutation gets reported.
 *
 * `recommendMutation` is fired from three places but only the run behind the
 * disclosure dialog has a dialog to report into — and it says more there than
 * a one-line footer can ("your choice was saved", "nothing was sent"). Every
 * other path has no open surface of its own and would otherwise fail silently.
 */
type ErrorSurface = "footer" | "dialog";

export function useColumnRecommendation(caseId: string, timelineId: string) {
  const queryClient = useQueryClient();
  const addJob = useJobsStore((s) => s.addJob);
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const setVisibleColumnsStore = useUiStore((s) => s.setVisibleColumns);

  const [errorSurface, setErrorSurface] = useState<ErrorSurface>("footer");
  // Which half of the confirm this attempt reached. Tracked by the confirm
  // itself rather than read off `isError`, which is sticky: a plain
  // "Re-suggest columns" that failed earlier would still be flagged here, and
  // a *save* failure would then be reported as "your choice was saved" — the
  // one wrong answer, since the analyst would never be asked again for a
  // consent that was never recorded.
  const [optInStage, setOptInStage] = useState<"save" | "run">("save");

  /** Record this analyst's answer for this timeline: `true` yes, `false` no. */
  const saveAnswer = async (answer: boolean) => {
    const updated = await authApi.updatePreferences({
      [COLUMN_ADVISOR_OPTIN]: { [timelineId]: answer },
    });
    setUser(updated);
    queryClient.setQueryData(["auth", "me"], updated);
    return updated;
  };

  const recommendMutation = useMutation({
    mutationFn: (useAi: boolean) => timelinesApi.recommendColumns(caseId, timelineId, useAi),
    onSuccess: (result) => {
      // The analyst asked for this answer, so it is theirs to receive: drop
      // the local override that would otherwise hide it. Done even when
      // `job_id` is null (a job was already in flight) — that job's answer is
      // the one they are waiting for.
      setVisibleColumnsStore(`${caseId}/${timelineId}`, undefined);
      if (result.job_id) {
        addJob(result.job_id, "Suggesting columns", [
          ["timeline", caseId, timelineId],
          ["fields", caseId, timelineId],
        ]);
      }
      queryClient.invalidateQueries({ queryKey: ["timeline", caseId, timelineId] });
    },
  });

  // The opt-in is persisted *before* the run, and the run only happens if that
  // write succeeded: a request that sends evidence must never be one the user
  // will be asked to authorize again because the record of it was lost.
  const optInAndRecommend = useMutation({
    mutationFn: async () => {
      setErrorSurface("dialog");
      setOptInStage("save");
      await saveAnswer(true);
      setOptInStage("run");
      return recommendMutation.mutateAsync(true);
    },
  });

  /**
   * "No thanks", recorded so the offer does not come back.
   *
   * The same key as the opt-in, holding `false` instead of `true` — the
   * question is per timeline either way, and a declined timeline has to be
   * distinguishable from one nobody has been asked about yet. Nothing is sent.
   */
  const decline = useMutation({ mutationFn: () => saveAnswer(false) });

  /** Start a run from a surface that has no dialog to report a failure into. */
  const recommend = (useAi: boolean) => {
    setErrorSurface("footer");
    recommendMutation.mutate(useAi);
  };

  return {
    recommend,
    optInAndRecommend,
    decline,
    /** True while a run this hook started is in flight. */
    pending: recommendMutation.isPending,
    optInPending: optInAndRecommend.isPending,
    /** A failure no dialog is already reporting. */
    footerError: recommendMutation.isError && errorSurface === "footer",
    /** Which half of the disclosure's confirm failed, for the dialog itself. */
    optInError: optInAndRecommend.isError ? optInStage : null,
    preferences: user?.preferences,
  };
}
