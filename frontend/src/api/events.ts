import { get } from "./client";
import type { EmbeddingFieldsResponse, Event, EventCursor, EventFilters, EventPage, FieldsResponse, HistogramResponse } from "./types";

export const eventsApi = {
  list: (
    caseId: string,
    timelineId: string,
    filters: EventFilters = {},
    signal?: AbortSignal,
    cursor?: EventCursor,
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
        after: cursor?.after,
        before: cursor?.before,
      };
    if (filters.artifacts && filters.artifacts.length > 0) {
      params.artifacts = filters.artifacts.join(",");
    }
    if (filters.tagsInclude && filters.tagsInclude.length > 0) {
      params.tags_include = filters.tagsInclude.join(",");
    }
    if (filters.tagsExclude && filters.tagsExclude.length > 0) {
      params.tags_exclude = filters.tagsExclude.join(",");
    }
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
    if (filters.ids && filters.ids.length > 0) {
      params.ids = filters.ids.join(",");
    }
    return get<EventPage>(
      `/cases/${caseId}/timelines/${timelineId}/events`,
      params,
      signal,
    );
  },

  /** Fetch a single full event by id — e.g. to hydrate a partial finding
   * object (analysis detectors return lightweight event stubs) before
   * displaying it in the Event Detail panel. */
  getById: async (
    caseId: string,
    timelineId: string,
    eventId: string,
  ): Promise<Event | null> => {
    const page = await get<EventPage>(
      `/cases/${caseId}/timelines/${timelineId}/events`,
      { event_id: eventId, limit: 1 },
    );
    return page.events[0] ?? null;
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
    if (filters.artifacts && filters.artifacts.length > 0) {
      params.artifacts = filters.artifacts.join(",");
    }
    if (filters.tagsInclude && filters.tagsInclude.length > 0) {
      params.tags_include = filters.tagsInclude.join(",");
    }
    if (filters.tagsExclude && filters.tagsExclude.length > 0) {
      params.tags_exclude = filters.tagsExclude.join(",");
    }
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
    if (filters.ids && filters.ids.length > 0) {
      params.ids = filters.ids.join(",");
    }
    return get<HistogramResponse>(
      `/cases/${caseId}/timelines/${timelineId}/histogram`,
      params,
    );
  },

  artifacts: (caseId: string, timelineId: string): Promise<string[]> =>
    get<{ artifacts: string[] }>(
      `/cases/${caseId}/timelines/${timelineId}/artifacts`,
    ).then((r) => r.artifacts),

  mergedTags: (caseId: string, timelineId: string): Promise<string[]> =>
    get<{ tags: string[] }>(
      `/cases/${caseId}/timelines/${timelineId}/tags/merged`,
    ).then((r) => r.tags),
};
