import { del, get, post, put } from "./client";
import type {
  AllowlistEntry,
  AllowlistListResponse,
  BaselineListResponse,
  BaselineMutationResponse,
  SuspectWindow,
} from "./types";

export interface BaselineDefinitionInput {
  name: string;
  baseline_start: string;
  baseline_end: string;
  suspect_windows: Array<Pick<SuspectWindow, "label" | "start" | "end">>;
}

export interface AllowlistEntryInput {
  detector: string;
  field: string;
  value: string;
  note?: string | null;
}

/**
 * CRUD for baseline definitions (baseline range + suspect windows) and the
 * detector value-allowlist — the two persistent-normality primitives a
 * timeline's temporal anomaly detection runs against.
 */
export const baselinesApi = {
  list: (caseId: string, timelineId: string) =>
    get<BaselineListResponse>(`/cases/${caseId}/timelines/${timelineId}/baselines`),

  create: (caseId: string, timelineId: string, body: BaselineDefinitionInput) =>
    post<BaselineMutationResponse>(
      `/cases/${caseId}/timelines/${timelineId}/baselines`,
      body,
    ),

  update: (
    caseId: string,
    timelineId: string,
    baselineId: string,
    body: BaselineDefinitionInput,
  ) =>
    put<BaselineMutationResponse>(
      `/cases/${caseId}/timelines/${timelineId}/baselines/${baselineId}`,
      body,
    ),

  remove: (caseId: string, timelineId: string, baselineId: string) =>
    del<{ deleted: boolean; baseline_id: string }>(
      `/cases/${caseId}/timelines/${timelineId}/baselines/${baselineId}`,
    ),

  listAllowlist: (caseId: string, timelineId: string) =>
    get<AllowlistListResponse>(`/cases/${caseId}/timelines/${timelineId}/allowlist`),

  addAllowlist: (caseId: string, timelineId: string, body: AllowlistEntryInput) =>
    post<{ entry: AllowlistEntry }>(
      `/cases/${caseId}/timelines/${timelineId}/allowlist`,
      body,
    ),

  removeAllowlist: (caseId: string, timelineId: string, entryId: string) =>
    del<{ deleted: boolean; entry_id: string }>(
      `/cases/${caseId}/timelines/${timelineId}/allowlist/${entryId}`,
    ),
};
