/**
 * Which columns an event grid actually shows, and where that answer comes from.
 *
 * Three sources, in strict precedence order:
 *
 * 1. **The analyst's own choice** for this timeline, in the `vestigo-ui`
 *    zustand store (per browser). Always wins — a suggestion recomputed
 *    because a colleague uploaded a source must never move someone's columns
 *    out from under them mid-investigation.
 * 2. **The timeline's suggestion** (`Timeline.recommended_columns`, issue
 *    #213), derived from the timeline's own field statistics and shared by
 *    everyone with access.
 * 3. **`DEFAULT_COLUMNS`** — timestamp/artifact/message, the original
 *    behaviour, used whenever there is no suggestion to apply.
 *
 * Kept in one place because `ExplorerPage` reads it to render the grid and
 * `ColumnPicker` reads it to render the checkboxes; the two disagreeing would
 * show ticks next to columns that are not on screen.
 */
import type { RecommendedColumns } from "@/api/types";
import { DEFAULT_COLUMNS, sanitizeColumns } from "@/stores/ui";

/**
 * How long a `running` recommendation is believed before the explorer stops
 * waiting on it. Background jobs are in-memory server-side, so a process that
 * died mid-job leaves a payload claiming to be in flight forever; the server
 * settles those on its next boot, and this is the client's own floor so a
 * long-lived tab cannot poll indefinitely against one that has not restarted.
 */
export const STALE_SUGGESTION_MS = 10 * 60_000;

/**
 * Whether a recommendation has columns ready to apply.
 *
 * `running` counts when it carries columns: a recompute keeps the previous
 * answer in the payload precisely so the grid does not fall back to the
 * built-in defaults and re-lay out twice while it works.
 */
export function hasSuggestion(
  recommended: RecommendedColumns | null | undefined,
): recommended is RecommendedColumns {
  if (!recommended || recommended.columns.length === 0) return false;
  return recommended.status === "ok" || recommended.status === "running";
}

/**
 * Whether a recommendation job is currently in flight for this timeline.
 *
 * False once the claim is older than {@link STALE_SUGGESTION_MS} — that job is
 * not coming back, and the caller polls on this answer.
 */
export function isSuggesting(
  recommended: RecommendedColumns | null | undefined,
): boolean {
  if (recommended?.status !== "running") return false;
  const startedAt = Date.parse(recommended.generated_at);
  if (Number.isNaN(startedAt)) return false;
  return Date.now() - startedAt < STALE_SUGGESTION_MS;
}

/**
 * The suggested columns, run through the same sanitizer the stored selections
 * go through — a suggestion is server data and gets no more trust than a
 * persisted one. Null when nothing survives sanitization, so the caller falls
 * back rather than rendering an empty grid.
 */
export function suggestedColumns(
  recommended: RecommendedColumns | null | undefined,
): string[] | null {
  if (!hasSuggestion(recommended)) return null;
  const sanitized = sanitizeColumns(recommended.columns);
  return sanitized.length > 0 ? sanitized : null;
}

/**
 * Preference key holding this user's answer per timeline for AI column
 * suggestions (issue #213): `true` opted in, `false` declined, key absent
 * never asked.
 *
 * Per timeline, because that is the granularity at which evidence is actually
 * sent: the request carries sample values from *this* timeline's events, so
 * consenting to it says nothing about the next one.
 *
 * A declined answer is stored rather than inferred from the absence of an
 * opt-in, because the Explorer offers the suggestion once per timeline and
 * has to tell "said no" apart from "not asked yet" — otherwise the offer
 * comes back on every visit, which is how people learn to dismiss a consent
 * dialog unread. Server-side for the same reason the opt-in is: the answer
 * has to hold on the analyst's other machine too.
 */
export const COLUMN_ADVISOR_OPTIN = "column_advisor_optin";

function advisorAnswer(
  preferences: Record<string, unknown> | null | undefined,
  timelineId: string,
): unknown {
  const answers = preferences?.[COLUMN_ADVISOR_OPTIN];
  if (!answers || typeof answers !== "object") return undefined;
  return (answers as Record<string, unknown>)[timelineId];
}

/** Whether *timelineId* has already been opted in by this user. */
export function hasColumnAdvisorOptIn(
  preferences: Record<string, unknown> | null | undefined,
  timelineId: string,
): boolean {
  return advisorAnswer(preferences, timelineId) === true;
}

/**
 * Whether this user has answered the question for *timelineId* at all — yes
 * or no. False only for a timeline nobody has been offered the suggestion on.
 */
export function hasAnsweredColumnAdvisor(
  preferences: Record<string, unknown> | null | undefined,
  timelineId: string,
): boolean {
  return typeof advisorAnswer(preferences, timelineId) === "boolean";
}

/** Resolve the columns to render, applying the precedence above. */
export function resolveVisibleColumns(
  stored: string[] | undefined,
  recommended: RecommendedColumns | null | undefined,
): string[] {
  if (stored) return stored;
  return suggestedColumns(recommended) ?? DEFAULT_COLUMNS;
}
