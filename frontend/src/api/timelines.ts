import { del, get, patch, post } from "./client";
import type {
  EmbeddingFieldsResponse,
  EmbeddingFieldConfig,
  FieldCoverageResponse,
  Source,
  Timeline,
} from "./types";

export const timelinesApi = {
  list: (caseId: string) =>
    get<{ timelines: Timeline[] }>(`/cases/${caseId}/timelines`).then(
      (r) => r.timelines,
    ),

  get: (caseId: string, timelineId: string) =>
    get<{ timeline: Timeline }>(
      `/cases/${caseId}/timelines/${timelineId}`,
    ).then((r) => r.timeline),

  create: (
    caseId: string,
    name: string,
    description?: string,
    sourceIds?: string[],
    fieldMappings?: Record<string, string[]> | null,
  ) =>
    post<{ timeline: Timeline }>(`/cases/${caseId}/timelines`, {
      name,
      description,
      source_ids: sourceIds ?? [],
      field_mappings:
        fieldMappings && Object.keys(fieldMappings).length > 0 ? fieldMappings : null,
    }).then((r) => r.timeline),

  /** Replace a timeline's field mappings (null/{} clears them). */
  patchFieldMappings: (
    caseId: string,
    timelineId: string,
    fieldMappings: Record<string, string[]> | null,
  ) =>
    patch<{ timeline: Timeline }>(
      `/cases/${caseId}/timelines/${timelineId}/field-mappings`,
      {
        field_mappings:
          fieldMappings && Object.keys(fieldMappings).length > 0 ? fieldMappings : null,
      },
    ).then((r) => r.timeline),

  /** Per-raw-field coverage across sources, for the wizard's aggregation step. */
  fieldCoverage: (caseId: string, sourceIds: string[]) =>
    get<FieldCoverageResponse>(
      `/cases/${caseId}/fields/coverage?source_ids=${encodeURIComponent(sourceIds.join(","))}`,
    ),

  delete: (caseId: string, timelineId: string) =>
    del<{ deleted: boolean }>(`/cases/${caseId}/timelines/${timelineId}`),

  listSources: (caseId: string, timelineId: string) =>
    get<{ sources: Source[] }>(
      `/cases/${caseId}/timelines/${timelineId}/sources`,
    ).then((r) => r.sources),

  addSource: (caseId: string, timelineId: string, sourceId: string) =>
    post<{ added: boolean }>(
      `/cases/${caseId}/timelines/${timelineId}/sources/${sourceId}`,
    ),

  removeSource: (caseId: string, timelineId: string, sourceId: string) =>
    del<{ removed: boolean }>(
      `/cases/${caseId}/timelines/${timelineId}/sources/${sourceId}`,
    ),

  /** Fetch per-artifact field recommendations for the timeline's embedding wizard. */
  embeddingFields: (caseId: string, timelineId: string) =>
    get<EmbeddingFieldsResponse>(
      `/cases/${caseId}/timelines/${timelineId}/embedding-fields`,
    ),

  /** Start a background job to embed all sources of a timeline. */
  embed: (caseId: string, timelineId: string, config: EmbeddingFieldConfig) =>
    post<{ job_id: string; status: string; source_ids: string[] }>(
      `/cases/${caseId}/timelines/${timelineId}/embed`,
      { embedding_config: config },
    ),
};
