import { get } from "./client";
import type { SimilarityResponse } from "./types";

export const similarityApi = {
  findSimilar: (
    caseId: string,
    eventId: string,
    limit = 10,
    timelineId?: string,
  ) =>
    get<SimilarityResponse>(`/cases/${caseId}/events/${eventId}/similar`, {
      limit,
      timeline_id: timelineId,
    }),

  semanticSearch: (
    caseId: string,
    query: string,
    limit = 10,
    timelineId?: string,
  ) =>
    get<SimilarityResponse>(`/cases/${caseId}/events/semantic-search`, {
      q: query,
      limit,
      timeline_id: timelineId,
    }),
};
