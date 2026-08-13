/**
 * useMutedMethods — the analysis methods kept out of this timeline's sweep.
 *
 * Shared server state on the Timeline, not a browser preference: "this
 * source's clocks are a mess, stop surfacing it" is a finding about the data
 * that the next analyst on the case inherits, and every change is audited.
 *
 * A mute is a *reading* preference and never a gate. The analysis plan does
 * not consult it — a muted method still reports `applicable`, because a mute
 * is not a claim that the method cannot produce a finding — and
 * `/analysis/findings` still runs it when asked for by name, which is what
 * keeps the sheet's "run anyway" honest for a muted method too. All the mute
 * does is keep the method out of the unprompted sweep, and callers owe the
 * analyst a visible count of what is being held back.
 *
 * Reads through the same `["timeline", caseId, timelineId]` query ExplorerPage
 * already holds, so muting costs no extra request and the write lands in one
 * cache that every reader shares.
 */
import { useCallback, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { timelinesApi } from "@/api/timelines";
import { casesApi } from "@/api/cases";
import { canContributeToCase } from "@/lib/caseAccess";
import { METHODS, type MethodId } from "@/components/analysis/method-registry";
import type { Timeline } from "@/api/types";

const KNOWN = new Set<string>(METHODS.map((m) => m.id));

export interface MutedMethods {
  /** Muted ids, filtered to methods this client knows about. */
  muted: Set<MethodId>;
  isMuted: (id: MethodId) => boolean;
  /** Mute if unmuted and vice versa. No-op without contribute access. */
  toggle: (id: MethodId) => void;
  /** Clear every mute at once — the rail's escape hatch. */
  unmuteAll: () => void;
  /** Whether the caller may change the list; false for read-only members. */
  canEdit: boolean;
  isSaving: boolean;
}

export function useMutedMethods(caseId: string, timelineId: string): MutedMethods {
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

  // An id the server holds but this build does not know is dropped rather than
  // carried: it can never match a method here, and keeping it would let the
  // rail claim it is holding back something it cannot name.
  const muted = useMemo(
    () => new Set((timeline?.muted_methods ?? []).filter((id) => KNOWN.has(id)) as MethodId[]),
    [timeline?.muted_methods],
  );

  const mutation = useMutation({
    mutationFn: (next: MethodId[]) => timelinesApi.patchMutedMethods(caseId, timelineId, next),
    // Written straight into the timeline cache rather than invalidated: the
    // sweep keys off this list, so a refetch round-trip would leave every
    // toggle looking dead for the length of a request.
    onSuccess: (updated: Timeline) => {
      queryClient.setQueryData(timelineKey, updated);
      queryClient.invalidateQueries({ queryKey: ["timelines", caseId] });
    },
  });

  const canEdit = case_ ? canContributeToCase(case_) : false;

  const write = useCallback(
    (next: Set<MethodId>) => {
      if (!canEdit) return;
      mutation.mutate([...next].sort());
    },
    [canEdit, mutation],
  );

  const toggle = useCallback(
    (id: MethodId) => {
      const next = new Set(muted);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      write(next);
    },
    [muted, write],
  );

  const unmuteAll = useCallback(() => write(new Set()), [write]);

  return {
    muted,
    isMuted: useCallback((id: MethodId) => muted.has(id), [muted]),
    toggle,
    unmuteAll,
    canEdit,
    isSaving: mutation.isPending,
  };
}
