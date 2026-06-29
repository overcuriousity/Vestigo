import { useMutation, useQueryClient } from "@tanstack/react-query";
import { annotationsApi } from "@/api/annotations";
import type { AnnotationType } from "@/api/types";

/**
 * Shared hook for creating and deleting annotations on events within a timeline.
 * Invalidates the timeline annotations query on success so all consumers
 * (EventGrid chips, EventDetailPanel, TriageMeter) refresh automatically.
 */
export function useAnnotationMutations(caseId: string, timelineId: string) {
  const qc = useQueryClient();
  const invalidate = () =>
    qc.invalidateQueries({ queryKey: ["annotations", caseId, timelineId] });

  const add = useMutation({
    mutationFn: (v: { eventId: string; type: AnnotationType; content: string }) =>
      annotationsApi.create(caseId, timelineId, v.eventId, v.type, v.content.trim()),
    onSuccess: invalidate,
    onError: invalidate, // refresh to reflect any partial success
  });

  const remove = useMutation({
    mutationFn: (v: { eventId: string; annotationId: string }) =>
      annotationsApi.delete(caseId, timelineId, v.eventId, v.annotationId),
    onSuccess: invalidate,
  });

  return { add, remove };
}
