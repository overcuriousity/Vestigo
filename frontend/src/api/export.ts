import { fetchBlob, type TransferOptions } from "./client";
import type {
  EventFilters,
  ExportFilterPayload,
  ExportRequest,
  FieldInventoryColumn,
  FieldInventoryOrder,
  FieldInventoryRequest,
  FieldInventorySeparator,
} from "./types";
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
  const body: ExportRequest = { format, filter: exportFilterPayload(filters) };

  const blob = await fetchBlob(
    `/cases/${caseId}/timelines/${timelineId}/export`,
    body,
    opts,
  );

  triggerDownload(blob, `${caseId}-${timelineId}-events.${format}`);
}

/**
 * The filter half of an export body.
 *
 * Both export surfaces send it, so an inventory and an events export taken from
 * the same view describe the same scope — an inventory computed over a wider one
 * would be a forensic footgun.
 */
function exportFilterPayload(filters: EventFilters): ExportFilterPayload {
  return {
    ...serializeEventFilterFields(filters),
    // Sent as raw objects, not JSON strings — this is already a
    // structured JSON POST body, unlike the query-param-shaped requests
    // (list/histogram/bulk-annotate) that stringify these.
    fields: filters.filters ?? {},
    exclude: filters.exclusions ?? {},
    field_modes: filters.filterModes ?? {},
    exclude_modes: filters.exclusionModes ?? {},
  };
}

export interface FieldInventoryOptions {
  field: string;
  columns: FieldInventoryColumn[];
  separator: FieldInventorySeparator;
  orderBy: FieldInventoryOrder;
}

/**
 * Download a value inventory of one field: one row per distinct value with its
 * count and first/last seen, within the current filters (#295).
 *
 * Streamed and uncapped like the events export — a high-cardinality field can have
 * millions of distinct values — so `opts` carries the same indeterminate progress
 * and the same abort signal.
 */
export async function downloadFieldInventory(
  caseId: string,
  timelineId: string,
  filters: EventFilters,
  inventory: FieldInventoryOptions,
  opts?: TransferOptions,
): Promise<void> {
  const body: FieldInventoryRequest = {
    field: inventory.field,
    columns: inventory.columns,
    separator: inventory.separator,
    order_by: inventory.orderBy,
    filter: exportFilterPayload(filters),
  };

  const blob = await fetchBlob(
    `/cases/${caseId}/timelines/${timelineId}/export/field-inventory`,
    body,
    opts,
  );

  const ext = inventory.separator === "tab" ? "tsv" : "csv";
  const slug = inventory.field.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "") || "field";
  // Must match the server's Content-Disposition name exactly. The separator is
  // part of it because comma, semicolon and pipe otherwise share one filename,
  // so a second export of the same field saves as `…(1).csv` and the analyst
  // reopens the first — indistinguishable from the picker doing nothing.
  triggerDownload(blob, `${caseId}-${timelineId}-${slug}-inventory-${inventory.separator}.${ext}`);
}
