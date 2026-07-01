import { get } from "./client";
import type { EmbeddingFieldsResponse, EventFilters, EventPage, FieldsResponse, HistogramResponse } from "./types";

export const eventsApi = {
  list: (
    caseId: string,
    timelineId: string,
    filters: EventFilters = {},
    signal?: AbortSignal,
  ): Promise<EventPage> => {
    const params: Record<string, string | number | boolean | undefined | null> =
      {
        q: filters.q,
        artifact: filters.artifact,
        source_id: filters.sourceId,
        tag: filters.tag,
        exclude_tag: filters.excludeTag,
        start: filters.start,
        end: filters.end,
        limit: filters.limit ?? 100,
        offset: filters.offset ?? 0,
        order: filters.order ?? "desc",
      };
    if (filters.filters && Object.keys(filters.filters).length > 0) {
      params.filters = JSON.stringify(filters.filters);
    }
    if (filters.exclusions && Object.keys(filters.exclusions).length > 0) {
      params.exclusions = JSON.stringify(filters.exclusions);
    }
    if (filters.annotated && filters.annotated.length > 0) {
      params.annotated = filters.annotated.join(",");
    }
    if (filters.annotationTagValue) {
      params.annotation_tag_value = filters.annotationTagValue;
    }
    if (filters.liveAnomalyEventIds && filters.liveAnomalyEventIds.length > 0) {
      params.live_event_ids = filters.liveAnomalyEventIds.join(",");
    }
    return get<EventPage>(
      `/cases/${caseId}/timelines/${timelineId}/events`,
      params,
      signal,
    );
  },

  fields: (caseId: string, timelineId: string): Promise<FieldsResponse> =>
    get<FieldsResponse>(`/cases/${caseId}/timelines/${timelineId}/fields`),

  embeddingFields: (
    caseId: string,
    timelineId: string,
  ): Promise<EmbeddingFieldsResponse> =>
    get<EmbeddingFieldsResponse>(
      `/cases/${caseId}/timelines/${timelineId}/embedding-fields`,
    ),

  histogram: (
    caseId: string,
    timelineId: string,
    filters: EventFilters = {},
    buckets = 60,
  ): Promise<HistogramResponse> => {
    const params: Record<string, string | number | undefined | null> = {
      q: filters.q,
      artifact: filters.artifact,
      source_id: filters.sourceId,
      tag: filters.tag,
      exclude_tag: filters.excludeTag,
      start: filters.start,
      end: filters.end,
      buckets,
    };
    if (filters.filters && Object.keys(filters.filters).length > 0) {
      params.filters = JSON.stringify(filters.filters);
    }
    if (filters.exclusions && Object.keys(filters.exclusions).length > 0) {
      params.exclusions = JSON.stringify(filters.exclusions);
    }
    if (filters.annotated && filters.annotated.length > 0) {
      params.annotated = filters.annotated.join(",");
    }
    if (filters.annotationTagValue) {
      params.annotation_tag_value = filters.annotationTagValue;
    }
    if (filters.liveAnomalyEventIds && filters.liveAnomalyEventIds.length > 0) {
      params.live_event_ids = filters.liveAnomalyEventIds.join(",");
    }
    return get<HistogramResponse>(
      `/cases/${caseId}/timelines/${timelineId}/histogram`,
      params,
    );
  },
};
