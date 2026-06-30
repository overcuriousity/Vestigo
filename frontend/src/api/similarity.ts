import { get } from "./client";
import type { SimilarityResponse } from "./types";

export const similarityApi = {
  findSimilar: (
    caseId: string,
    timelineId: string,
    eventId: string,
    limit = 10,
  ) =>
    get<SimilarityResponse>(
      `/cases/${caseId}/timelines/${timelineId}/events/${eventId}/similar`,
      { limit },
    ),
};
