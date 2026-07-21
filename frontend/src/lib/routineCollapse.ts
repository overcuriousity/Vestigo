/**
 * Routine-collapse resolution for the Explorer (issue #147).
 *
 * A `kind="routine"` disposition — minted by Templates → Mute or Patterns →
 * Mark routine — *is* a filter, and the UI copy promises its events leave the
 * grid immediately. Collapse is therefore derived from the disposition set
 * rather than opted into: any routine disposition means collapse is on, and
 * the top-bar toggle is a temporary reveal override.
 *
 * The override is stamped with the disposition-set signature it was made
 * against so it expires by itself. Without that stamp, an analyst who revealed
 * routine events once would silently defeat every later mute — exactly the
 * "muted it, still there" the issue reports, one step removed.
 *
 * Extracted from ExplorerPage so the precedence between derived state, the
 * reveal toggle and an agent-applied `collapseRoutine` stays unit tested; the
 * agent's applied filter set must reproduce what it ran (`agent/tools.py`).
 */
import type { Disposition } from "@/api/types";

/** An explicit collapse/reveal choice, stamped with the disposition set it
 * was made against. `null` = no choice made, use the derived default. */
export type RoutineOverride = { value: boolean; signature: string } | null;

/**
 * Identity of the active routine-disposition set. Empty string = nothing to
 * collapse. Order-independent, since the dispositions query gives no ordering
 * guarantee and a reshuffle must not read as a new filter.
 */
export function routineSignature(dispositions: readonly Disposition[]): string {
  return dispositions
    .filter((d) => d.kind === "routine")
    .map((d) => d.id)
    .sort()
    .join(",");
}

/**
 * Whether the grid should collapse routine events right now.
 *
 * On whenever routine dispositions exist, unless an override made against this
 * exact set says otherwise. An empty scope is always `false`: with nothing to
 * hide, a `true` here would render a collapse stat claiming zero collapsed
 * events, which reads as a broken filter.
 */
export function resolveCollapseRoutine(
  signature: string,
  override: RoutineOverride,
): boolean {
  if (signature === "") return false;
  if (override && override.signature === signature) return override.value;
  return true;
}
