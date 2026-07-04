import { fetchBlob } from "./client";
import type { ExportRequest, EventFilters } from "./types";
import { serializeEventFilterFields } from "@/lib/queryParams";
import { triggerDownload } from "@/lib/download";

export async function downloadExport(
  caseId: string,
  timelineId: string,
  format: "csv" | "jsonl",
  filters: EventFilters,
): Promise<void> {
  const body: ExportRequest = {
    format,
    filter: {
      ...serializeEventFilterFields(filters),
      // Sent as raw objects, not JSON strings — this is already a
      // structured JSON POST body, unlike the query-param-shaped requests
      // (list/histogram/bulk-annotate) that stringify these.
      fields: filters.filters ?? {},
      exclude: filters.exclusions ?? {},
      field_modes: filters.filterModes ?? {},
      exclude_modes: filters.exclusionModes ?? {},
    },
  };

  const blob = await fetchBlob(
    `/cases/${caseId}/timelines/${timelineId}/export`,
    body,
  );

  triggerDownload(blob, `${caseId}-${timelineId}-events.${format}`);
}
