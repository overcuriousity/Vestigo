import { fetchBlob } from "./client";
import type { ExportRequest, EventFilters } from "./types";

export async function downloadExport(
  caseId: string,
  timelineId: string,
  format: "csv" | "jsonl",
  filters: EventFilters,
): Promise<void> {
  const body: ExportRequest = {
    format,
    filter: {
      q: filters.q,
      artifact: filters.artifact,
      artifacts: filters.artifacts && filters.artifacts.length > 0 ? filters.artifacts.join(",") : undefined,
      source_id: filters.sourceId,
      tag: filters.tag,
      exclude_tag: filters.excludeTag,
      tags_include: filters.tagsInclude && filters.tagsInclude.length > 0 ? filters.tagsInclude.join(",") : undefined,
      tags_exclude: filters.tagsExclude && filters.tagsExclude.length > 0 ? filters.tagsExclude.join(",") : undefined,
      ids: filters.ids && filters.ids.length > 0 ? filters.ids.join(",") : undefined,
      start: filters.start,
      end: filters.end,
      fields: filters.filters ?? {},
      exclude: filters.exclusions ?? {},
      annotated: filters.annotated && filters.annotated.length > 0 ? filters.annotated.join(",") : undefined,
      annotation_tag_value: filters.annotationTagValue,
      live_event_ids:
        filters.liveAnomalyEventIds && filters.liveAnomalyEventIds.length > 0
          ? filters.liveAnomalyEventIds.join(",")
          : undefined,
    },
  };

  const blob = await fetchBlob(
    `/cases/${caseId}/timelines/${timelineId}/export`,
    body,
  );

  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `${caseId}-${timelineId}-events.${format}`;
  a.click();
  URL.revokeObjectURL(url);
}
