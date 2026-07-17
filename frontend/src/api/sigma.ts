/**
 * Sigma rule runner API — rules (global directory + case uploads), runs.
 */
import { del, get, patch, post } from "./client";

export interface SigmaLogsource {
  product?: string;
  category?: string;
  service?: string;
}

/** One rule as listed for the run picker (global-directory or case-uploaded). */
export interface SigmaRuleInfo {
  origin: "global" | "case";
  /** Relative file path (global) or Postgres row id (case) — the run selection handle. */
  ref: string;
  rule_key: string;
  title: string;
  rule_uuid: string | null;
  level: string | null;
  logsource: SigmaLogsource | null;
  content_hash: string;
  enabled: boolean;
  error?: string | null;
  /** Case rules only. */
  id?: string;
  yaml_content?: string;
  created_at?: string | null;
}

export interface SigmaRulesResponse {
  rules_path_configured: boolean;
  global_rules: SigmaRuleInfo[];
  case_rules: SigmaRuleInfo[];
}

export type SigmaRuleStatus = "matched" | "empty" | "not_applicable" | "error";

/** Per-rule outcome inside a persisted run record. */
export interface SigmaRunResult {
  rule_key: string;
  origin: "global" | "case";
  ref: string;
  title: string;
  level: string | null;
  logsource: SigmaLogsource | null;
  content_hash: string;
  sql: string | null;
  match_count: number;
  status: SigmaRuleStatus;
  error: string | null;
  fallback_fields: string[];
}

export interface SigmaRun {
  id: string;
  case_id: string;
  timeline_id: string;
  status: "queued" | "running" | "completed" | "failed";
  params: { source_ids?: string[]; selection?: { origin: string; ref: string }[] | null };
  results: SigmaRunResult[] | null;
  error: string | null;
  created_by: string | null;
  created_at: string | null;
  completed_at: string | null;
}

export const sigmaApi = {
  listRules: (caseId: string): Promise<SigmaRulesResponse> =>
    get<SigmaRulesResponse>(`/cases/${caseId}/sigma/rules`),

  uploadRule: (caseId: string, yamlContent: string): Promise<SigmaRuleInfo> =>
    post<{ rule: SigmaRuleInfo }>(`/cases/${caseId}/sigma/rules`, {
      yaml_content: yamlContent,
    }).then((r) => r.rule),

  setRuleEnabled: (caseId: string, ruleId: string, enabled: boolean): Promise<void> =>
    patch(`/cases/${caseId}/sigma/rules/${ruleId}`, { enabled }),

  deleteRule: (caseId: string, ruleId: string): Promise<void> =>
    del(`/cases/${caseId}/sigma/rules/${ruleId}`),

  run: (
    caseId: string,
    timelineId: string,
    rules?: { origin: string; ref: string }[] | null,
  ): Promise<{ job_id: string; run_id: string; status: string }> =>
    post(`/cases/${caseId}/timelines/${timelineId}/sigma/run`, { rules: rules ?? null }),

  listRuns: (caseId: string): Promise<SigmaRun[]> =>
    get<{ runs: SigmaRun[] }>(`/cases/${caseId}/sigma/runs`).then((r) => r.runs),

  getRun: (caseId: string, runId: string): Promise<SigmaRun> =>
    get<{ run: SigmaRun }>(`/cases/${caseId}/sigma/runs/${runId}`).then((r) => r.run),
};
