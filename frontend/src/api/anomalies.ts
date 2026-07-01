import { get, post } from "./client";
import type { AnomaliesResponse, Annotation, NoveltyFieldsResponse, TagAnomaliesResponse } from "./types";

export interface AnomalyParams {
  detector?: "value_novelty" | "frequency";
  /** Comma-separated field tokens for value_novelty, e.g. "artifact,display_name,attr:user_agent" */
  fields?: string;
  /** Field to group frequency series by */
  series_field?: string;
  /** |z| cutoff for the frequency detector. Omit to use the server default. */
  z_threshold?: number;
  /** Explicit temporal baseline end timestamp */
  baseline_start?: string;
  /** Enable temporal mode (backend uses timeline midpoint when baseline_start is absent) */
  temporal?: boolean;
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

  /** Return candidate fields (with cardinality metadata) for the field picker. */
  fields: (caseId: string, timelineId: string) =>
    get<NoveltyFieldsResponse>(
      `/cases/${caseId}/timelines/${timelineId}/anomalies/fields`,
    ),

  /**
   * Persist a single live (not-yet-tagged) finding as a system annotation,
   * without re-running the detector or touching any other tagged finding —
   * the per-event "Persist" action in the event detail panel.
   */
  persistFinding: (
    caseId: string,
    sourceId: string,
    eventId: string,
    body: { detector: "value_novelty" | "frequency"; content: string; details: Record<string, unknown> },
  ) =>
    post<{ annotation: Annotation }>(
      `/cases/${caseId}/sources/${sourceId}/events/${eventId}/anomalies/persist`,
      body,
    ),
};
