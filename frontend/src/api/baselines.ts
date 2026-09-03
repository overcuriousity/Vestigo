import { del, get, post, put } from "./client";
import type {
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

/**
 * CRUD for baseline definitions (baseline range + suspect windows) — the
 * time-based normality primitive. Value/event-level normality lives in the
 * disposition taxonomy (see `dispositionsApi`).
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

  /**
   * Delete a definition. Detectors framed on it are unconfigured with it —
   * a configured entry pointing at a baseline that no longer exists is a
   * question nobody can answer — and `unconfigured_detectors` names the
   * methods that went, so the caller can say so.
   */
  remove: (caseId: string, timelineId: string, baselineId: string) =>
    del<{ deleted: boolean; baseline_id: string; unconfigured_detectors: string[] }>(
      `/cases/${caseId}/timelines/${timelineId}/baselines/${baselineId}`,
    ),
};
