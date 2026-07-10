import { del, get, post } from "./client";
import type { Disposition, DispositionKind, DispositionListResponse } from "./types";

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

  create: (caseId: string, timelineId: string, body: DispositionInput) =>
    post<{ disposition: Disposition }>(
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
