import { fetchBlob, type TransferOptions } from "./client";
import type { ExportRequest, EventFilters } from "./types";
import { serializeEventFilterFields } from "@/lib/queryParams";
import { triggerDownload } from "@/lib/download";

/**
 * Download the filtered event set as CSV/JSONL.
 *
 * The server streams this and applies no row cap, so the response can be
 * arbitrarily large — but the browser still has to hold the whole Blob before
 * it can be saved. `opts` therefore matters here: the response is chunked with
 * no `Content-Length`, so progress reports bytes received with a null total
 * (an indeterminate bar), and the signal is the only way out of a download
 * that turns out to be bigger than the analyst expected.
 */
export async function downloadExport(
  caseId: string,
  timelineId: string,
  format: "csv" | "jsonl",
  filters: EventFilters,
  opts?: TransferOptions,
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
    opts,
  );

  triggerDownload(blob, `${caseId}-${timelineId}-events.${format}`);
}
