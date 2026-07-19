/** Scoped MCP access tokens for the external agent endpoint (docs/AGENT.md). */
import { get, post, del } from "./client";

export interface AgentToken {
  id: string;
  case_id: string;
  timeline_id: string;
  user_id: string;
  name: string;
  created_at: string | null;
  expires_at: string | null;
  revoked_at: string | null;
}

export const agentTokensApi = {
  list: (caseId: string, timelineId: string) =>
    get<{ tokens: AgentToken[] }>(`/cases/${caseId}/timelines/${timelineId}/agent-tokens`),
  create: (caseId: string, timelineId: string, body: { name: string; expires_in_days?: number }) =>
    post<AgentToken & { token: string }>(
      `/cases/${caseId}/timelines/${timelineId}/agent-tokens`,
      body,
    ),
  revoke: (caseId: string, timelineId: string, tokenId: string) =>
    del<{ revoked: boolean }>(`/cases/${caseId}/timelines/${timelineId}/agent-tokens/${tokenId}`),
};
