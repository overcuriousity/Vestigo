import type { Case, User } from "@/api/types";

/** Mirrors the backend's `resolve_case_access` (api/deps.py) access levels,
 * computed client-side purely to decide what UI to show/hide — the backend
 * remains the source of truth and re-checks on every request.
 *
 * Follow-up (PR #7 review, cleanup): returning a computed `access_level`
 * field from the case list/detail API would collapse this duplication to a
 * field read. Not done here — `list_cases_for_user` would need a bulk
 * access-resolution path to avoid an N+1 (one `resolve_case_access` call
 * per case), which is a bigger API-shape change than this cleanup pass. */
export type CaseAccessLevel = "none" | "read" | "contribute" | "manage";

export function resolveCaseAccess(case_: Case, user: User | null): CaseAccessLevel {
  if (!user) return "none";
  if (user.is_admin) return "manage";
  if (case_.team_id) {
    const membership = user.teams?.find((t) => t.id === case_.team_id);
    if (!membership) return "none";
    return membership.role === "manager" ? "manage" : "contribute";
  }
  if (case_.owner_id === user.id) return "manage";
  return "none";
}

export function canManageCase(case_: Case, user: User | null): boolean {
  return resolveCaseAccess(case_, user) === "manage";
}

/** Teams the user may create a *team* case for, based on their own memberships
 * (must be a manager of the team). Admins are not necessarily a member of any
 * team, so callers should combine this with the full team list (fetched via
 * `adminApi.listTeams`) for admin users — see CreateCaseDialog. */
export function manageableTeams(user: User | null): { id: string; name: string }[] {
  return (user?.teams ?? []).filter((t) => t.role === "manager").map((t) => ({ id: t.id, name: t.name }));
}
