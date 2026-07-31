import { BASE, fetchBlobGet, get, patch, post, put } from "./client";
import type { User } from "./types";

export const authApi = {
  login: (username: string, password: string) =>
    post<{ user: User }>("/auth/login", { username, password }).then((r) => r.user),

  logout: () => post<{ logged_out: boolean }>("/auth/logout"),

  me: () => get<{ user: User }>("/auth/me").then((r) => r.user),

  listUsers: () =>
    get<{ users: import("../lib/userNames").DirectoryUser[] }>("/auth/users").then(
      (r) => r.users,
    ),

  updateProfile: (payload: {
    username?: string;
    display_name?: string;
    onboarding_completed?: boolean;
  }) =>
    patch<{ user: User }>("/auth/me", payload).then((r) => r.user),

  /**
   * Merge whitelisted keys into the user's own preferences blob. The backend
   * rejects any key it does not know (`_ALLOWED_PREFERENCE_KEYS`).
   */
  updatePreferences: (preferences: Record<string, unknown>) =>
    put<{ user: User }>("/auth/me/preferences", { preferences }).then((r) => r.user),

  changePassword: (newPassword: string, currentPassword?: string) =>
    post<{ user: User }>("/auth/me/password", {
      new_password: newPassword,
      current_password: currentPassword,
    }).then((r) => r.user),

  downloadMyAudit: (format: "csv" | "json" = "csv") =>
    fetchBlobGet("/auth/me/audit", { format }),

  oidcLoginUrl: () => `${BASE}/auth/oidc/login`,
};
