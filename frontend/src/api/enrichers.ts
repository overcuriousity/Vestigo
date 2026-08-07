import { get, post, postForm, put, type TransferOptions } from "./client";

export interface EnricherInfo {
  key: string;
  display_name: string;
  description: string;
  output_fields: string[];
  available: boolean;
  reason: string | null;
}

/** An enrichment run that died before its staged results were applied.
 *
 * The work is already computed and one partition rewrite away from landing;
 * resuming applies it without re-scanning. `completed_sources` below
 * `staged_sources` means some source's staging was cut short — its values
 * still apply, but it stays eligible for a later run. */
export interface UnfinishedEnrichmentRun {
  job_id: string;
  started_at: string;
  age_seconds: number;
  staged_rows: number;
  staged_sources: number;
  completed_sources: number;
}

export interface TimelineEnricherInfo {
  key: string;
  display_name: string;
  description: string;
  eligible: boolean;
  sample_checked: number;
  sample_matched: number;
  /** Set when the eligibility scan itself failed (e.g. ClickHouse down).
   * `eligible` is then false because it is unknown, not because it is no. */
  eligibility_error: string | null;
  mode: "automatic" | "manual";
  enabled: boolean;
  unfinished_run: UnfinishedEnrichmentRun | null;
}

export interface EnricherAssetInfo {
  name: string;
  description: string;
  accepted_extensions: string[];
  uploaded: boolean;
  size_bytes: number | null;
  detail: Record<string, string | number | null>;
}

export interface AdminEnricherConfig {
  key: string;
  display_name: string;
  description: string;
  available: boolean;
  reason: string | null;
  auto_run_default: boolean;
  // Present when the enricher declares an uploadable data asset (asset_spec).
  asset: EnricherAssetInfo | null;
}

export const enrichersApi = {
  list: () =>
    get<{ enrichers: EnricherInfo[] }>("/enrichers").then((r) => r.enrichers),

  listForTimeline: (caseId: string, timelineId: string) =>
    get<{ enrichers: TimelineEnricherInfo[] }>(
      `/cases/${caseId}/timelines/${timelineId}/enrichers`,
    ).then((r) => r.enrichers),

  setConfig: (
    caseId: string,
    timelineId: string,
    key: string,
    body: { mode: "automatic" | "manual"; enabled: boolean },
  ) =>
    put(`/cases/${caseId}/timelines/${timelineId}/enrichers/${key}`, body),

  run: (caseId: string, timelineId: string, key: string, force = false) =>
    post<{
      // null when every ready source is already enriched at the current config
      // (status "skipped") — no job is started. `force` bypasses that skip and
      // re-enriches every ready source (idempotent; recovery path when
      // provenance disagrees with the actual event data).
      job_id: string | null;
      status: string;
      source_ids: string[];
      skipped_source_ids: string[];
    }>(
      `/cases/${caseId}/timelines/${timelineId}/enrichers/${key}/run${force ? "?force=true" : ""}`,
      {},
    ),

  /** Apply an unfinished run's already-staged results and clear its marker.
   * No re-scan, no recomputation. `jobId` is the marker's id from
   * `unfinished_run`, echoed back so a stale dialog 404s instead of resuming
   * something the analyst never saw. Returns a *different* id to poll. */
  resume: (caseId: string, timelineId: string, key: string, jobId: string) =>
    post<{
      job_id: string;
      resumed_job_id: string;
      status: string;
      staged_rows: number;
      staged_sources: number;
    }>(`/cases/${caseId}/timelines/${timelineId}/enrichers/${key}/resume`, {
      job_id: jobId,
    }),

  adminConfigs: () =>
    get<{ enrichers: AdminEnricherConfig[] }>("/admin/enrichers/config").then(
      (r) => r.enrichers,
    ),

  setAdminConfig: (key: string, body: { auto_run_default: boolean }) =>
    put(`/admin/enrichers/${key}/config`, body),

  /** Install an enricher's data asset (GeoLite mmdb and friends — tens to
   * hundreds of MB, hence `opts`). Synchronous server-side: there is no job
   * to poll afterwards, so the upload's own progress is the only feedback. */
  uploadAsset: (key: string, file: File, opts?: TransferOptions) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<{ available: boolean; reason: string | null }>(
      `/admin/enrichers/${key}/asset`,
      form,
      opts,
    );
  },
};
