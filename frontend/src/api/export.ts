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
      source: filters.source,
      tag: filters.tag,
      exclude_tag: filters.excludeTag,
      start: filters.start,
      end: filters.end,
      fields: filters.filters ?? {},
      exclude: filters.exclusions ?? {},
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
