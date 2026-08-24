/**
 * useTimelineReadiness — is there anything on this timeline to analyse yet?
 *
 * Only the Investigate surface can answer this. A single method sees its own
 * empty response and cannot tell "nothing was ingested" from "this method
 * found nothing", which is why thirteen detector views used to each claim "No
 * events ingested yet" in wording that drifted. Said once, here, and read by
 * both the rail and the Tools sheet so the two never disagree.
 *
 * The distinction matters most for the negative claim. A Sigma scan that
 * matches nothing on an empty timeline reads as "these rules cleared you"; it
 * did not clear anything, there was nothing to match against.
 *
 * `status: "ingesting"` sources are excluded from timeline queries until they
 * are ready, so a mid-ingest timeline legitimately scans zero events — and
 * needs a different sentence from a case nobody has uploaded to, because one
 * resolves itself and the other needs an action.
 */
import { useQuery } from "@tanstack/react-query";
import { timelinesApi } from "@/api/timelines";

export interface TimelineReadiness {
  /** Some source is mid-ingest: this resolves itself, given time. */
  stillIngesting: boolean;
  /** No ready source carries a single event. Undefined until sources load. */
  nothingToAnalyse: boolean;
}

export function useTimelineReadiness(caseId: string, timelineId: string): TimelineReadiness {
  const { data: sources } = useQuery({
    queryKey: ["timeline-sources", caseId, timelineId],
    queryFn: () => timelinesApi.listSources(caseId, timelineId),
    // The "still ingesting" state promises events appear as they land, so it
    // has to notice when they do.
    refetchInterval: (query) => (query.state.data?.some((s) => s.status !== "ready") ? 4000 : false),
  });

  const readyEventCount = (sources ?? [])
    .filter((s) => s.status === "ready")
    .reduce((n, s) => n + s.event_count, 0);

  return {
    stillIngesting: (sources ?? []).some((s) => s.status === "ingesting"),
    // `sources !== undefined`: before the list loads, "nothing to analyse" is
    // not yet a claim anyone can make, and asserting it would flash an empty
    // state over a timeline that has plenty.
    nothingToAnalyse: sources !== undefined && readyEventCount === 0,
  };
}
