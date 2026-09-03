/**
 * useFieldOverrides — which fields a detector reads on this timeline, declared.
 *
 * The recommenders behind every method's "auto" mode type fields
 * *syntactically* and say so: an HTTP status code parses as a number, so the
 * numeric-range detector offers it, learns a band over {200, 404, 500} and
 * reports the 500s as outliers forever. No amount of probing discovers that it
 * is a categorical field wearing digits — only the analyst knows. This is where
 * they say it, per method: the same field is meaningless to `numeric_range` and
 * excellent for `value_novelty`, so a declaration is never global.
 *
 * Shared server state on the Timeline for the same reason `detectors` is —
 * "status codes are not a range field" is a finding about the data that the
 * next analyst inherits, and every change is audited.
 *
 * Advice to the recommenders, never a gate: it steers only a detector's
 * *automatic* field selection. Naming the field explicitly still scans it, the
 * analysis plan does not consult it, and a run that held a field back discloses
 * it in its warnings.
 *
 * Reads through the same `["timeline", caseId, timelineId]` query the mute
 * strip and ExplorerPage already hold, so it costs no extra request and every
 * reader shares one cache.
 */
import { useCallback, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { timelinesApi } from "@/api/timelines";
import { casesApi } from "@/api/cases";
import { canContributeToCase } from "@/lib/caseAccess";
import type { MethodId } from "@/components/analysis/method-registry";
import type { Timeline } from "@/api/types";

/** A field's declared state for one method. `null` = undeclared. */
export type FieldDeclaration = boolean | null;

export type FieldOverrides = Record<string, Record<string, boolean>>;

export interface FieldOverridesHandle {
  /** The whole timeline's declarations, method id -> field token -> on/off. */
  overrides: FieldOverrides;
  /** One method's slice; `{}` when it declares nothing. */
  forMethod: (id: MethodId) => Record<string, boolean>;
  /** Declare a field on/off for a method, or `null` to hand it back to the recommender. */
  declare: (id: MethodId, token: string, state: FieldDeclaration) => void;
  /** Drop every declaration for one method. */
  clearMethod: (id: MethodId) => void;
  /** Whether the caller may change them; false for read-only members. */
  canEdit: boolean;
  isSaving: boolean;
  /** Set when the last write failed — this is shared state, so silence lies. */
  saveError: string | null;
}

const EMPTY: Record<string, boolean> = {};

/**
 * In-flight write state per timeline, module-level rather than per-hook.
 *
 * Two hook instances are mounted at once — the method sheet's field picker and
 * the Tools sheet's summary — and they write the same object with a
 * full-replace PATCH. Per-instance refs would serialize each against itself
 * only: declare a field in the picker, switch to Tools before it lands, hit
 * Reset, and Tools would build its payload from the still-stale query cache,
 * dropping the in-flight declaration from the timeline *and* from the audit
 * row's previous/new pair. Keyed by timeline because that is the scope of the
 * object being replaced.
 */
interface WriteChain {
  pending: FieldOverrides | null;
  chain: Promise<unknown>;
}
const writeChains = new Map<string, WriteChain>();

function chainFor(key: string): WriteChain {
  let entry = writeChains.get(key);
  if (!entry) {
    entry = { pending: null, chain: Promise.resolve() };
    writeChains.set(key, entry);
  }
  return entry;
}

export function useFieldOverrides(caseId: string, timelineId: string): FieldOverridesHandle {
  const queryClient = useQueryClient();
  const timelineKey = ["timeline", caseId, timelineId];

  const { data: timeline } = useQuery({
    queryKey: timelineKey,
    queryFn: () => timelinesApi.get(caseId, timelineId),
    enabled: Boolean(caseId && timelineId),
  });
  const { data: case_ } = useQuery({
    queryKey: ["case", caseId],
    queryFn: () => casesApi.get(caseId),
    enabled: Boolean(caseId),
  });

  const overrides = useMemo<FieldOverrides>(
    () => timeline?.field_overrides ?? {},
    [timeline?.field_overrides],
  );

  const mutation = useMutation({
    mutationFn: (next: FieldOverrides) =>
      timelinesApi.patchFieldOverrides(caseId, timelineId, next),
    // Written into the timeline cache rather than invalidated, like the mute
    // list: what runs keys off this, so a refetch round-trip would leave every
    // toggle looking dead for the length of a request.
    onSuccess: (updated: Timeline) => {
      queryClient.setQueryData(timelineKey, updated);
      queryClient.invalidateQueries({ queryKey: ["timelines", caseId] });
      // The declaration changes which fields a detector picks for itself, so
      // every finding held from before it is an answer to a different question.
      // The server's own cache key covers it; these drop the client's — both
      // the per-method findings and the sweep that feeds the rail.
      queryClient.invalidateQueries({ queryKey: ["anomalies", caseId, timelineId] });
      queryClient.invalidateQueries({ queryKey: ["detector-sweep-v2", caseId, timelineId] });
    },
  });

  const canEdit = case_ ? canContributeToCase(case_) : false;

  // The chip row invites rapid multi-declare — ban one field, then the next.
  // `overrides` only catches up on the mutation's onSuccess, so both edits
  // would otherwise build on the same pre-mutation snapshot and the second
  // PATCH, a full replace, would drop the first from the timeline *and* from
  // the audit row's previous/new pair. Each edit builds on what is in flight,
  // and the requests are chained so they cannot land out of order. Shared
  // across hook instances (see `writeChains`) because the sheet mounts two.
  const chainKey = `${caseId}/${timelineId}`;

  const write = useCallback(
    (next: FieldOverrides) => {
      if (!canEdit) return;
      const entry = chainFor(chainKey);
      entry.pending = next;
      entry.chain = entry.chain
        .catch(() => {})
        .then(() => mutation.mutateAsync(next))
        .catch(() => {
          // Reported through `saveError` — a rejection here would break the
          // chain for every later write, and this state is shared and audited,
          // so it must not fail silently either.
        })
        .finally(() => {
          // Only the last write in flight hands control back to server state;
          // an earlier one settling must not strand a newer edit's base.
          if (entry.pending === next) entry.pending = null;
        });
    },
    [canEdit, chainKey, mutation],
  );

  const declare = useCallback(
    (id: MethodId, token: string, state: FieldDeclaration) => {
      const base = chainFor(chainKey).pending ?? overrides;
      const forMethod = { ...(base[id] ?? {}) };
      if (state === null) delete forMethod[token];
      else forMethod[token] = state;
      const next = { ...base };
      // A method with nothing left declared is dropped rather than sent as an
      // empty object, so "undeclared" has one representation in the audit trail.
      if (Object.keys(forMethod).length === 0) delete next[id];
      else next[id] = forMethod;
      write(next);
    },
    [chainKey, overrides, write],
  );

  const clearMethod = useCallback(
    (id: MethodId) => {
      const base = chainFor(chainKey).pending ?? overrides;
      if (!(id in base)) return;
      const next = { ...base };
      delete next[id];
      write(next);
    },
    [chainKey, overrides, write],
  );

  return {
    overrides,
    forMethod: useCallback((id: MethodId) => overrides[id] ?? EMPTY, [overrides]),
    declare,
    clearMethod,
    canEdit,
    isSaving: mutation.isPending,
    // The chip returns to the server's answer on the next render either way,
    // which on its own reads as "nothing happened" rather than "this was not
    // saved" — for state the rest of the case inherits, say which.
    saveError: mutation.error ? ((mutation.error as Error).message ?? "Save failed") : null,
  };
}
