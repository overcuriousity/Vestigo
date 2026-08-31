import { del, get, patch, post } from "./client";
import type { AuditEntry, Team, TeamMember, TeamRole, User } from "./types";

/** One tool in the agent's catalog, for the admin toggle UI. */
export interface AdminAgentTool {
  name: string;
  description: string;
  embeddings_gated: boolean;
  requires_conversation: boolean;
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
  ) =>
    patch<{ user: User }>(`/admin/users/${userId}`, payload).then(
      (r) => r.user,
    ),

  rotatePassword: (userId: string, newPassword: string, forceChange = true) =>
    post<{ rotated: boolean }>(`/admin/users/${userId}/password`, {
      new_password: newPassword,
      force_change: forceChange,
    }),

  deleteUser: (userId: string, reassignTo?: string) =>
    del<{ deleted: boolean }>(`/admin/users/${userId}`, {
      reassign_to: reassignTo,
    }),

  // --- Teams -------------------------------------------------------------
  listTeams: () => get<{ teams: Team[] }>("/admin/teams").then((r) => r.teams),

  createTeam: (name: string, description?: string) =>
    post<{ team: Team }>("/admin/teams", { name, description }).then(
      (r) => r.team,
    ),

  deleteTeam: (teamId: string) =>
    del<{ deleted: boolean }>(`/admin/teams/${teamId}`),

  // --- Memberships ---------------------------------------------------------
  listMembers: (teamId: string) =>
    get<{ members: TeamMember[] }>(`/admin/teams/${teamId}/members`).then(
      (r) => r.members,
    ),

  addMember: (teamId: string, userId: string, role: TeamRole = "member") =>
    post<{ membership: unknown }>(`/admin/teams/${teamId}/members`, {
      user_id: userId,
      role,
    }),

  setMemberRole: (teamId: string, userId: string, role: TeamRole) =>
    patch<{ updated: boolean }>(`/admin/teams/${teamId}/members/${userId}`, {
      role,
    }),

  removeMember: (teamId: string, userId: string) =>
    del<{ removed: boolean }>(`/admin/teams/${teamId}/members/${userId}`),

  // --- Audit ---------------------------------------------------------------
  queryAudit: (filters?: {
    user_id?: string;
    case_id?: string;
    action?: string;
    limit?: number;
  }) =>
    get<{ audit: AuditEntry[] }>("/admin/audit", filters).then((r) => r.audit),

  // --- AI agent ------------------------------------------------------------
  /** Model ids the configured LLM endpoint advertises, for the model picker.
   * Takes the *unsaved* credentials so an admin sees an endpoint's models
   * before committing them; omitted fields fall back to what is already
   * configured (the key is never sent back to the browser, so it usually is).
   * Never errors on an unreachable endpoint — an empty list means "fall back
   * to free-text entry". */
  listAgentModels: (creds: {
    api_base_url?: string;
    api_key?: string;
    provider?: string;
  }) => post<{ models: string[] }>("/admin/agent/models", creds),

  /** Re-probe the configured endpoint now, ignoring the cached availability
   * result — "Test connection". Persists nothing. */
  probeAgent: () => post<{ available: boolean }>("/admin/agent/probe", {}),
};
