import { useCallback, useMemo } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { authApi } from "@/api/auth";
import { useAuthStore } from "@/stores/auth";
import type { MethodId } from "@/components/analysis/method-registry";

/** Preference key holding every timeline's focus for this analyst. */
export const ANALYSIS_METHOD_FOCUS = "analysis_method_focus";

/** One timeline's focus: the fields each focused method is narrowed to. */
export type MethodFocus = Record<string, string[]>;

type AllFocus = Record<string, MethodFocus>;

/** One shared empty object, so "no focus" keeps a stable identity — see below. */
const NO_FOCUS: MethodFocus = Object.freeze({});

function readAll(preferences: Record<string, unknown> | null | undefined): AllFocus {
  const raw = preferences?.[ANALYSIS_METHOD_FOCUS];
  return raw && typeof raw === "object" ? (raw as AllFocus) : {};
}

/**
 * An analyst's own narrowing of which fields one method scans (#341).
 *
 * Deliberately *not* `Timeline.field_overrides`: that is the case team's
 * shared, audited declaration of which fields a method should consider, and
 * one analyst tidying their own feed must not rewrite what a colleague sees.
 * A focus is applied instead by sending an explicit `fields` to
 * `/analysis/findings`, which bypasses the overrides layer by contract rather
 * than competing with it — so the two mechanisms stay legible side by side.
 *
 * This narrows what is actually *scanned*, not just what is displayed: a
 * focused method produces no findings for the fields it no longer looks at.
 * That is what the analyst asked for, and it is why every caller must keep
 * the focus disclosed and one click from gone.
 */
export function useMethodFocus(timelineId: string) {
  const user = useAuthStore((s) => s.user);
  const setUser = useAuthStore((s) => s.setUser);
  const queryClient = useQueryClient();

  // Memoized, and falling back to one shared empty object rather than a fresh
  // `{}`: `useStreamingSweep` derives its per-method params from `fieldsFor`,
  // and the rail publishes markers derived from that sweep back into
  // ExplorerPage state. A focus that changed identity every render would make
  // that loop — the same hazard useMethodFindings documents for `byMethod`.
  const focus = useMemo(
    () => readAll(user?.preferences)[timelineId] ?? NO_FOCUS,
    [user?.preferences, timelineId],
  );

  const write = useCallback(
    async (next: MethodFocus) => {
      // The whole timeline object goes in one request: the server merges
      // dict-valued preferences one level down (by timeline), so sending a
      // partial object here would drop this timeline's other focused methods.
      const updated = await authApi.updatePreferences({
        [ANALYSIS_METHOD_FOCUS]: { [timelineId]: next },
      });
      setUser(updated);
      queryClient.setQueryData(["auth", "me"], updated);
    },
    [timelineId, setUser, queryClient],
  );

  /** The fields *method* is narrowed to, or undefined when it is not focused. */
  const fieldsFor = useCallback(
    (method: MethodId): string[] | undefined => {
      const fields = focus[method];
      // An empty list is not a focus on nothing — it would scan no field at
      // all and silently empty the feed. Treat it as absent.
      return Array.isArray(fields) && fields.length > 0 ? fields : undefined;
    },
    [focus],
  );

  const setFocus = useCallback(
    (method: MethodId, fields: string[]) => write({ ...focus, [method]: fields }),
    [focus, write],
  );

  const clearFocus = useCallback(
    (method: MethodId) => {
      const next = { ...focus };
      delete next[method];
      return write(next);
    },
    [focus, write],
  );

  return { focus, fieldsFor, setFocus, clearFocus };
}
