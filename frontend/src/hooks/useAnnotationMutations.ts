import { useMutation, useQueryClient } from "@tanstack/react-query";
import { annotationsApi } from "@/api/annotations";
import type { AnnotationType } from "@/api/types";

/**
 * Shared hook for creating and deleting annotations on events within a source.
 * Invalidates both the timeline annotations query and the anomalies query on
 * success so all consumers (EventGrid chips, EventDetailPanel, TriageMeter,
 * AnomaliesList) refresh automatically.  The anomaly refresh is important
 * because marking/unmarking Normal events changes the active algorithm mode.
 */
export function useAnnotationMutations(caseId: string, sourceId: string) {
  const qc = useQueryClient();

  const invalidate = () => {
    // Invalidate by prefix so all timeline- and source-scoped annotation
    // queries for this case are refreshed regardless of how consumers key them.
    qc.invalidateQueries({ queryKey: ["annotations", caseId] });
    // Marking an event Normal changes which anomaly algorithm is active.
    qc.invalidateQueries({ queryKey: ["anomalies", caseId] });
    // Refresh tag autocomplete suggestions when a new tag is created.
    qc.invalidateQueries({ queryKey: ["tags", caseId] });
  };

  const add = useMutation({
    mutationFn: (v: { eventId: string; type: AnnotationType; content: string }) =>
      annotationsApi.create(caseId, sourceId, v.eventId, v.type, v.content.trim()),
    onSuccess: invalidate,
    onError: invalidate, // refresh to reflect any partial success
  });

  const remove = useMutation({
    mutationFn: (v: { eventId: string; annotationId: string }) =>
      annotationsApi.delete(caseId, sourceId, v.eventId, v.annotationId),
    onSuccess: invalidate,
  });

  return { add, remove };
}
