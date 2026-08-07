/**
 * Client for the two Investigate endpoints: the gate's plan, and one method's
 * findings.
 *
 * `plan` costs no event scan, so it is cheap to call on every scope change.
 * `findings` is served from a server-side fingerprint cache whenever the data,
 * scope and params are unchanged — the response says which (`cache`), so the
 * UI can be honest about whether it recomputed.
 */
import { get } from "./client";
import type { MethodId } from "@/components/analysis/method-registry";
import type { AnomalyFinding } from "./types";

export type MethodStatus = "applicable" | "not_applicable" | "needs_setup";

/** The comparison a plan or finding set was produced under. */
export interface AnalysisScope {
  frame: "self" | "baseline";
  baseline_id: string | null;
  baseline_name: string | null;
  dispositions_hash?: string;
}

export interface MethodPlanEntry {
  method: MethodId;
  status: MethodStatus;
  /** Empty when applicable; otherwise why this method cannot find anything here. */
  reason: string;
  /** The arithmetic behind `reason`, so the UI shows numbers rather than a verdict. */
  reason_facts: Record<string, number | string | boolean>;
  cost_class: "cheap" | "heavy";
}

export interface AnalysisPlan {
  methods: MethodPlanEntry[];
  scope: AnalysisScope;
  events_total: number;
}

export interface MethodFindings {
  method: MethodId;
  status: string;
  results: AnomalyFinding[];
  total_findings: number;
  dismissed_count?: number;
  warnings: string[];
  scope: AnalysisScope;
  cache: "hit" | "miss";
  computed_at: string;
}

export interface ScopeParams {
  frame: "self" | "baseline";
  baseline_id?: string;
}

export const analysisApi = {
  /** Gate verdicts for every method under *scope*. No events are scanned. */
  plan: (caseId: string, timelineId: string, scope: ScopeParams) =>
    get<AnalysisPlan>(`/cases/${caseId}/timelines/${timelineId}/analysis/plan`, { ...scope }),

  /**
   * Run one method. A method the plan reported as not applicable runs here
   * exactly as any other — the gate never withholds.
   */
  findings: (
    caseId: string,
    timelineId: string,
    opts: ScopeParams & {
      method: MethodId;
      params?: Record<string, unknown>;
      limit?: number;
      include_dismissed?: boolean;
    },
  ) => {
    const { params, ...rest } = opts;
    return get<MethodFindings>(`/cases/${caseId}/timelines/${timelineId}/analysis/findings`, {
      ...rest,
      // Omitted entirely when empty so the cache key matches the server's
      // "no params" case rather than an empty-object variant of it.
      ...(params && Object.keys(params).length ? { params: JSON.stringify(params) } : {}),
    });
  },
};
