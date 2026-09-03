/**
 * useTimelineDetectors — the detectors configured on this timeline.
 *
 * Shared server state on the Timeline, not a browser preference: which
 * detectors an investigation runs is a decision the next analyst inherits,
 * and every change is audited. This list is the *only* thing the Investigate
 * rail runs — nothing runs unprompted.
 *
 * Reads through the same `["timeline", caseId, timelineId]` query ExplorerPage
 * already holds; writes land in that cache from the response so a just-added
 * detector starts fetching without a round trip.
 */
import { useCallback, useMemo } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { timelinesApi } from "@/api/timelines";
import { casesApi } from "@/api/cases";
import { canContributeToCase } from "@/lib/caseAccess";
import { METHODS, type MethodId } from "@/components/analysis/method-registry";
import type { ScopeParams } from "@/api/analysis";
import type { DetectorEntry, Timeline } from "@/api/types";

const KNOWN = new Set<string>(METHODS.map((m) => m.id));

export type DetectorBody = Pick<DetectorEntry, "params" | "frame" | "baseline_id">;

/** A stored entry whose method this build knows. */
export interface KnownDetectorEntry extends DetectorEntry {
  method: MethodId;
}

export interface TimelineDetectors {
  /** Stored order, filtered to methods this client knows about. */
  entries: KnownDetectorEntry[];
  byMethod: Map<MethodId, KnownDetectorEntry>;
  set: (method: MethodId, body: DetectorBody) => Promise<Timeline>;
  remove: (method: MethodId) => Promise<Timeline>;
  canEdit: boolean;
  isSaving: boolean;
  saveError: string | null;
}

/** The scope params a configured entry's findings request carries. */
export function scopeOf(entry: DetectorEntry): ScopeParams {
  return entry.frame === "baseline" && entry.baseline_id
    ? { frame: "baseline", baseline_id: entry.baseline_id }
    : { frame: "self" };
}

export function useTimelineDetectors(caseId: string, timelineId: string): TimelineDetectors {
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

  // An entry for a method this build does not know is dropped rather than
  // carried: the rail could neither run nor name it.
  const entries = useMemo(
    () =>
      (timeline?.detectors ?? []).filter((e): e is KnownDetectorEntry => KNOWN.has(e.method)),
    [timeline?.detectors],
  );
  const byMethod = useMemo(
    () => new Map<MethodId, KnownDetectorEntry>(entries.map((e) => [e.method, e])),
    [entries],
  );

  const onSuccess = (updated: Timeline) => {
    queryClient.setQueryData(timelineKey, updated);
    queryClient.invalidateQueries({ queryKey: ["timelines", caseId] });
  };
  const setMutation = useMutation({
    mutationFn: ({ method, body }: { method: MethodId; body: DetectorBody }) =>
      timelinesApi.putDetector(caseId, timelineId, method, body),
    onSuccess,
  });
  const removeMutation = useMutation({
    mutationFn: (method: MethodId) => timelinesApi.deleteDetector(caseId, timelineId, method),
    onSuccess,
  });

  const canEdit = case_ ? canContributeToCase(case_) : false;
  const error = setMutation.error ?? removeMutation.error;

  const set = useCallback(
    (method: MethodId, body: DetectorBody) => setMutation.mutateAsync({ method, body }),
    [setMutation],
  );
  const remove = useCallback(
    (method: MethodId) => removeMutation.mutateAsync(method),
    [removeMutation],
  );

  return {
    entries,
    byMethod,
    set,
    remove,
    canEdit,
    isSaving: setMutation.isPending || removeMutation.isPending,
    saveError: error ? (error as Error).message : null,
  };
}
