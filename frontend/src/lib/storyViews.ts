/**
 * Resolve live Explorer filters to a persisted View for a story push.
 *
 * A `view_ref` block references a saved View, so pushing live filter state
 * has to persist one first. Doing that unconditionally meant pushing the same
 * filters to the same story three times left three identically-named Views
 * cluttering the case — the analyst never asked for three artifacts, they
 * asked for one block three times.
 *
 * So: an existing View with the same query and the same filter payload is
 * reused, and a new one is only minted when the filters are genuinely new.
 * (The design round sketched a naming dialog here instead; reuse solves the
 * duplication the dialog was meant to prevent without interrupting a push.
 * See docs/STORIES.md.)
 */
import { viewsApi } from "@/api/views";
import type { EventFilters, View } from "@/api/types";
import { filtersToViewPayload } from "@/lib/queryParams";

/**
 * True for a value that means "this filter is not set".
 *
 * `filtersToViewPayload` writes every key explicitly, so an unset filter
 * arrives as `null`, `[]`, `{}` or `false` depending on its type — while a
 * View saved by an older build simply omits the key. Both mean the same
 * filter, so both have to compare equal or reuse never triggers.
 */
function isEmpty(value: unknown): boolean {
  if (value === null || value === undefined || value === false) return true;
  if (Array.isArray(value)) return value.length === 0;
  if (typeof value === "object") return Object.keys(value as object).length === 0;
  return false;
}

/** Stable JSON for comparing two filter payloads by value, not identity. */
function canonical(value: unknown): string {
  if (value === null || typeof value !== "object") return JSON.stringify(value ?? null);
  if (Array.isArray(value)) return `[${value.map(canonical).join(",")}]`;
  const entries = Object.entries(value as Record<string, unknown>)
    .filter(([, v]) => !isEmpty(v))
    .sort(([a], [b]) => a.localeCompare(b));
  return `{${entries.map(([k, v]) => `${JSON.stringify(k)}:${canonical(v)}`).join(",")}}`;
}

/** True when a saved View encodes exactly these filters. */
export function viewMatchesFilters(view: View, filters: EventFilters): boolean {
  return (
    (view.query ?? "") === (filters.q ?? "") &&
    canonical(view.filter) === canonical(filtersToViewPayload(filters))
  );
}

/** Reuse the View encoding `filters`, or create one named `name`. */
export async function findOrCreateView(
  caseId: string,
  name: string,
  filters: EventFilters,
): Promise<View> {
  const existing = (await viewsApi.list(caseId)).find((v) => viewMatchesFilters(v, filters));
  if (existing) return existing;
  return viewsApi.create(caseId, name, filters.q ?? "", filtersToViewPayload(filters));
}
