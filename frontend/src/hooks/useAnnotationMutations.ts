import { useMutation, useQueryClient } from "@tanstack/react-query";
import { annotationsApi } from "@/api/annotations";
import type { AnnotationType } from "@/api/types";
import { shouldInvalidate } from "@/hooks/useCaseStream";

/**
 * Shared hook for creating and deleting annotations on events within a source.
 * Invalidates every annotation/tag-sensitive panel query on success — the
 * same prefix list `useCaseStream` uses for SSE-driven invalidation, so a
 * teammate's live update and the analyst's own edit refresh the same set of
 * panels (EventGrid chips, EventDetailPanel, TriageMeter, ValueNoveltyView,
 * FrequencyView, histogram, viz modals).
 */
export function useAnnotationMutations(caseId: string, sourceId: string) {
  const qc = useQueryClient();

  const invalidate = () => {
    qc.invalidateQueries({ predicate: (query) => shouldInvalidate(query.queryKey, caseId) });
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
