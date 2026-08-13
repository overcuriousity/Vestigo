/**
 * useSigmaFindings — the latest completed Sigma run's hits, as rail findings.
 *
 * The rail groups by evidence weight, and its strongest group — "Named
 * techniques — a rule author named this" — existed from the start with nothing
 * able to fill it: every method in the registry is `statistical` or
 * `exploration`, so the group was structurally always empty and the preset
 * meant to filter to it shipped as something else. This is what fills it.
 *
 * Deliberate shape decisions:
 *
 * - **A hit is a rule, not an event.** A run records `match_count` per rule,
 *   not the matching rows, so a row here is "this rule matched N events" and
 *   the drill hands the grid the rule's tag. Inventing per-event rows would
 *   mean issuing a query per rule on every rail open to manufacture a shape the
 *   run does not have.
 * - **Only a completed run counts.** A queued or failed run is not evidence of
 *   anything, and a rail that showed its rules would be asserting matches
 *   nobody has computed.
 * - **Only this timeline's runs.** Runs are stored per case; a hit found on a
 *   different timeline is not a finding about the one on screen.
 */
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { sigmaApi, type SigmaRunResult } from "@/api/sigma";
import { useCapabilities } from "@/api/health";

export interface SigmaFinding {
  ruleKey: string;
  title: string;
  level: string | null;
  matchCount: number;
  /** The grid filter tag SigmaPanel's own drill uses — kept identical. */
  tag: string;
}

export function useSigmaFindings(caseId: string, timelineId: string) {
  const { sigma } = useCapabilities();
  const { data: runs = [], isLoading } = useQuery({
    queryKey: ["sigma-runs", caseId],
    queryFn: () => sigmaApi.listRuns(caseId),
    enabled: Boolean(sigma && caseId),
  });

  const findings = useMemo<SigmaFinding[]>(() => {
    const completed = runs.filter(
      (r) => r.timeline_id === timelineId && r.status === "completed" && r.results,
    );
    if (completed.length === 0) return [];
    // Runs come back newest-first, but sort rather than trust it: showing the
    // second-newest run's hits would report a rule set the analyst has already
    // replaced, with no indication anywhere that it is stale.
    const latest = completed.reduce((a, b) =>
      (b.created_at ?? "") > (a.created_at ?? "") ? b : a,
    );
    return (latest.results ?? [])
      .filter((res: SigmaRunResult) => res.status === "matched" && res.match_count > 0)
      .sort((a, b) => b.match_count - a.match_count)
      .map((res) => ({
        ruleKey: res.rule_key,
        title: res.title,
        level: res.level,
        matchCount: res.match_count,
        tag: `sigma: ${res.title}`,
      }));
  }, [runs, timelineId]);

  return { findings, isLoading: Boolean(sigma) && isLoading, available: Boolean(sigma) };
}
