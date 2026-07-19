/**
 * Map user ids to human names for display. `created_by`-style columns carry
 * opaque user ids (`user_34d6292d`) — a collaboration tool must show who did
 * what by name. Prefers display_name, falls back to username; unknown values
 * (legacy rows that stored a raw username, external principals) pass through
 * unchanged, null becomes "anonymous".
 */

export interface DirectoryUser {
  id: string;
  username: string;
  display_name: string | null;
}

export function buildUserNameMap(users: DirectoryUser[]): Map<string, string> {
  // Key by id AND username: annotations store user ids, older columns
  // (proposal decided_by, legacy rows) store usernames — both resolve.
  const map = new Map<string, string>();
  for (const u of users) {
    const name = u.display_name || u.username;
    map.set(u.id, name);
    map.set(u.username, name);
  }
  return map;
}

export function resolveUserName(map: Map<string, string>, value: string | null): string {
  if (!value) return "anonymous";
  return map.get(value) ?? value;
}
