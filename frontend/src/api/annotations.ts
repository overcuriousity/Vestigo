import { del, get, post } from "./client";
import type { Annotation, AnnotationType, EventFilters } from "./types";

export const annotationsApi = {
  listDistinctTags: (caseId: string, timelineId: string) =>
    get<{ tags: string[] }>(
      `/cases/${caseId}/timelines/${timelineId}/tags`,
    ).then((r) => r.tags),

  listForTimeline: (caseId: string, timelineId: string) =>
    get<{ annotations: Annotation[] }>(
      `/cases/${caseId}/timelines/${timelineId}/annotations`,
    ).then((r) => r.annotations),

  listForEvent: (caseId: string, sourceId: string, eventId: string) =>
    get<{ annotations: Annotation[] }>(
      `/cases/${caseId}/sources/${sourceId}/events/${eventId}/annotations`,
    ).then((r) => r.annotations),

  create: (
    caseId: string,
    sourceId: string,
    eventId: string,
    annotation_type: AnnotationType,
    content: string,
  ) =>
    post<{ annotation: Annotation }>(
      `/cases/${caseId}/sources/${sourceId}/events/${eventId}/annotations`,
      { annotation_type, content },
    ).then((r) => r.annotation),

  delete: (
    caseId: string,
    sourceId: string,
    eventId: string,
    annotationId: string,
  ) =>
    del<{ deleted: boolean }>(
      `/cases/${caseId}/sources/${sourceId}/events/${eventId}/annotations/${annotationId}`,
    ),

  bulkByFilter: (
    caseId: string,
    timelineId: string,
    params: {
      annotation_type: AnnotationType;
      content: string;
      filters: EventFilters;
    },
  ) => {
    const { filters } = params;
    return post<{ tagged: number }>(
      `/cases/${caseId}/timelines/${timelineId}/events/annotations/bulk`,
      {
        annotation_type: params.annotation_type,
        content: params.content,
        q: filters.q ?? null,
        artifact: filters.artifact ?? null,
        artifacts: filters.artifacts && filters.artifacts.length > 0 ? filters.artifacts.join(",") : null,
        source_id: filters.sourceId ?? null,
        tag: filters.tag ?? null,
        exclude_tag: filters.excludeTag ?? null,
        tags_include: filters.tagsInclude && filters.tagsInclude.length > 0 ? filters.tagsInclude.join(",") : null,
        tags_exclude: filters.tagsExclude && filters.tagsExclude.length > 0 ? filters.tagsExclude.join(",") : null,
        ids: filters.ids && filters.ids.length > 0 ? filters.ids.join(",") : null,
        start: filters.start ?? null,
        end: filters.end ?? null,
        filters: filters.filters ? JSON.stringify(filters.filters) : null,
        exclusions: filters.exclusions ? JSON.stringify(filters.exclusions) : null,
        annotated: filters.annotated && filters.annotated.length > 0 ? filters.annotated.join(",") : null,
        annotation_tag_value: filters.annotationTagValue ?? null,
        live_event_ids:
          filters.liveAnomalyEventIds && filters.liveAnomalyEventIds.length > 0
            ? filters.liveAnomalyEventIds.join(",")
            : null,
      },
    );
  },
};
