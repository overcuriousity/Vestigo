import { del, get, post } from "./client";
import type {
  Disposition,
  DispositionKind,
  DispositionListResponse,
  DispositionStatsResponse,
} from "./types";

/**
 * One disposition declaration. Exactly one scope: value (`field` + `value`)
 * or event (`source_id` + `event_id`). `detector` defaults to `"*"` (all
 * detectors) server-side; `confirmed` requires event scope and a concrete
 * detector.
 */
export interface DispositionInput {
  kind: DispositionKind;
  detector?: string;
  field?: string;
  value?: string;
  source_id?: string;
  event_id?: string;
  note?: string | null;
  details?: Record<string, unknown> | null;
  /**
   * The analysis comparison this verdict was reached under. A verdict is an
   * assertion about a comparison, so without it "confirmed on 4 March" cannot
   * say what the finding was compared against. Optional: a caller that does
   * not know its scope records none rather than a guess.
   */
  analysis_scope?: Record<string, unknown> | null;
}

/**
 * CRUD for the unified disposition taxonomy — the analyst verdicts on anomaly
 * findings (normal = baseline extension, dismissed = presentation-only noise,
 * confirmed = durable escalation). Every mutation is audited server-side.
 */
export const dispositionsApi = {
  list: (caseId: string, timelineId: string, params?: { kind?: DispositionKind; detector?: string }) =>
    get<DispositionListResponse>(
      `/cases/${caseId}/timelines/${timelineId}/dispositions`,
      params,
    ),

  stats: (caseId: string, timelineId: string) =>
    get<DispositionStatsResponse>(
      `/cases/${caseId}/timelines/${timelineId}/dispositions/stats`,
    ),

  create: (caseId: string, timelineId: string, body: DispositionInput) =>
    post<{ disposition: Disposition; materialization_job_id?: string }>(
      `/cases/${caseId}/timelines/${timelineId}/dispositions`,
      body,
    ),

  bulkCreate: (caseId: string, timelineId: string, items: DispositionInput[]) =>
    post<{ dispositions: Disposition[] }>(
      `/cases/${caseId}/timelines/${timelineId}/dispositions/bulk`,
      { items },
    ),

  remove: (caseId: string, timelineId: string, dispositionId: string) =>
    del<{ deleted: boolean; disposition_id: string }>(
      `/cases/${caseId}/timelines/${timelineId}/dispositions/${dispositionId}`,
    ),
};
