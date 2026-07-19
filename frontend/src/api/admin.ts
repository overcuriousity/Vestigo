import { del, get, patch, post, put } from "./client";
import type { AuditEntry, Team, TeamMember, TeamRole, User } from "./types";

/** GET/PUT `/admin/agent-settings` response shape: the effective (env/db/default-merged)
 * AI agent config, the source each field was resolved from, and the env var name for any
 * field currently pinned by the environment (see `resolve_agent_config` in the backend). */
export interface AgentSettingsResponse {
  effective: Record<string, unknown> & { api_key_set: boolean };
  sources: Record<string, "env" | "db" | "default">;
  env_vars: Record<string, string>;
  /** A10: "env-only" means the backend refuses to store the API key in the DB. */
  secret_mode: "db" | "env-only";
}

export const adminApi = {
  // --- Users -----------------------------------------------------------
  listUsers: (unassigned?: boolean) =>
    get<{ users: User[] }>("/admin/users", { unassigned }).then((r) => r.users),

  createUser: (payload: {
    username: string;
    password: string;
    is_admin?: boolean;
    display_name?: string;
    email?: string;
  }) => post<{ user: User }>("/admin/users", payload).then((r) => r.user),

  updateUser: (
    userId: string,
    payload: {
      username?: string;
      display_name?: string;
      is_admin?: boolean;
      is_active?: boolean;
    },
  ) => patch<{ user: User }>(`/admin/users/${userId}`, payload).then((r) => r.user),

  rotatePassword: (userId: string, newPassword: string, forceChange = true) =>
    post<{ rotated: boolean }>(`/admin/users/${userId}/password`, {
      new_password: newPassword,
      force_change: forceChange,
    }),

  deleteUser: (userId: string, reassignTo?: string) =>
    del<{ deleted: boolean }>(`/admin/users/${userId}`, { reassign_to: reassignTo }),

  // --- Teams -------------------------------------------------------------
  listTeams: () => get<{ teams: Team[] }>("/admin/teams").then((r) => r.teams),

  createTeam: (name: string, description?: string) =>
    post<{ team: Team }>("/admin/teams", { name, description }).then((r) => r.team),

  deleteTeam: (teamId: string) => del<{ deleted: boolean }>(`/admin/teams/${teamId}`),

  // --- Memberships ---------------------------------------------------------
  listMembers: (teamId: string) =>
    get<{ members: TeamMember[] }>(`/admin/teams/${teamId}/members`).then((r) => r.members),

  addMember: (teamId: string, userId: string, role: TeamRole = "member") =>
    post<{ membership: unknown }>(`/admin/teams/${teamId}/members`, {
      user_id: userId,
      role,
    }),

  setMemberRole: (teamId: string, userId: string, role: TeamRole) =>
    patch<{ updated: boolean }>(`/admin/teams/${teamId}/members/${userId}`, { role }),

  removeMember: (teamId: string, userId: string) =>
    del<{ removed: boolean }>(`/admin/teams/${teamId}/members/${userId}`),

  // --- Audit ---------------------------------------------------------------
  queryAudit: (filters?: { user_id?: string; case_id?: string; action?: string; limit?: number }) =>
    get<{ audit: AuditEntry[] }>("/admin/audit", filters).then((r) => r.audit),

  // --- AI agent settings (A7) ----------------------------------------------
  getAgentSettings: () => get<AgentSettingsResponse>("/admin/agent-settings"),

  putAgentSettings: (patch: Record<string, unknown>) =>
    put<AgentSettingsResponse>("/admin/agent-settings", patch),
};
