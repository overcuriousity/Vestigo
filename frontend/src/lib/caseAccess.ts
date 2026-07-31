import type { Case, User } from "@/api/types";

/** Access levels as resolved by the backend (`resolve_case_access`,
 * api/deps.py) and returned on every case as `access_level` — the client
 * only reads the field, it never re-implements the rules. */
export type CaseAccessLevel = Case["access_level"];

export function canManageCase(case_: Case): boolean {
  return case_.access_level === "manage";
}

/** Whether the caller may change shared case content (annotations, timeline
 * metadata, the column suggestion) rather than only read it. */
export function canContributeToCase(case_: Case): boolean {
  return case_.access_level === "contribute" || case_.access_level === "manage";
}

/** Teams the user may create a *team* case for, based on their own memberships
 * (must be a manager of the team). Admins are not necessarily a member of any
 * team, so callers should combine this with the full team list (fetched via
 * `adminApi.listTeams`) for admin users — see CreateCaseDialog. */
export function manageableTeams(user: User | null): { id: string; name: string }[] {
  return (user?.teams ?? []).filter((t) => t.role === "manager").map((t) => ({ id: t.id, name: t.name }));
}
