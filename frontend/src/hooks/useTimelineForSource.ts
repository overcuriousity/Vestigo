import { useQuery } from "@tanstack/react-query";

/**
 * A timeline that covers `sourceId`, preferring the case's default.
 *
 * An event is addressed by (source, id) but `eventsApi.getById` is
 * timeline-scoped, so the timeline has to be one that actually contains the
 * source. Defaulting to the case's default timeline made a card report
 * "deleted" for any event whose source wasn't in it — while the server-side
 * export resolver, which queries by `source_id`, resolved the same block
 * fine. Editor and export must not disagree about whether a block resolves.
 */
export function useTimelineForSource(caseId: string, sourceId: string) {
  const query = useQuery({
    queryKey: ["timelines", caseId],
    queryFn: () => import("@/api/timelines").then((m) => m.timelinesApi.list(caseId)),
  });
  const timelines = query.data;
  const timeline =
    timelines?.find((t) => t.is_default && t.source_ids.includes(sourceId)) ??
    timelines?.find((t) => t.source_ids.includes(sourceId));
  return { ...query, timelineId: timeline?.id };
}
