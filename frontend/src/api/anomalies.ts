import { get, post } from "./client";
import type { AnomaliesResponse, TagAnomaliesResponse } from "./types";

export interface AnomalyParams {
  detector?: "value_novelty" | "frequency";
  /** Comma-separated field tokens for value_novelty, e.g. "artifact,display_name,attr:user_agent" */
  fields?: string;
  /** Field to group frequency series by */
  series_field?: string;
  /** Temporal baseline end: values absent before this time and present after are flagged */
  baseline_start?: string;
  limit?: number;
  [key: string]: string | number | boolean | null | undefined;
}

export interface TagAnomalyParams extends AnomalyParams {
  // same shape as AnomalyParams — POST body
}

export const anomaliesApi = {
  list: (caseId: string, timelineId: string, params: AnomalyParams = {}) =>
    get<AnomaliesResponse>(
      `/cases/${caseId}/timelines/${timelineId}/anomalies`,
      params,
    ),

  tag: (caseId: string, timelineId: string, params: TagAnomalyParams = {}) =>
    post<TagAnomaliesResponse>(
      `/cases/${caseId}/timelines/${timelineId}/anomalies/tag`,
      params,
    ),
};
