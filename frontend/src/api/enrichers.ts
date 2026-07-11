import { get, post, postForm, put } from "./client";

export interface EnricherInfo {
  key: string;
  display_name: string;
  description: string;
  output_fields: string[];
  available: boolean;
  reason: string | null;
}

export interface TimelineEnricherInfo {
  key: string;
  display_name: string;
  description: string;
  eligible: boolean;
  sample_checked: number;
  sample_matched: number;
  mode: "automatic" | "manual";
  enabled: boolean;
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

  adminConfigs: () =>
    get<{ enrichers: AdminEnricherConfig[] }>("/admin/enrichers/config").then(
      (r) => r.enrichers,
    ),

  setAdminConfig: (key: string, body: { auto_run_default: boolean }) =>
    put(`/admin/enrichers/${key}/config`, body),

  uploadAsset: (key: string, file: File) => {
    const form = new FormData();
    form.append("file", file);
    return postForm<{ available: boolean; reason: string | null }>(
      `/admin/enrichers/${key}/asset`,
      form,
    );
  },
};
