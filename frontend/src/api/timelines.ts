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

  /**
   * Replace the analysis methods muted for this timeline (empty clears them).
   *
   * Shared state, not a browser preference: the next analyst on the case
   * inherits the mute, and every change lands in the audit trail.
   */
  patchMutedMethods: (caseId: string, timelineId: string, mutedMethods: string[]) =>
    patch<{ timeline: Timeline }>(
      `/cases/${caseId}/timelines/${timelineId}/muted-methods`,
      { muted_methods: mutedMethods },
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

  /**
   * Re-derive the timeline's suggested grid columns (issue #213).
   *
   * `useAi` is the analyst's per-timeline opt-in to the LLM reranker, which
   * sends candidate field names and sample values to the configured model
   * endpoint; without it the scoring is local and nothing leaves the machine.
   * `job_id` is null when a recommendation is already running for this
   * timeline.
   */
  recommendColumns: (caseId: string, timelineId: string, useAi = false) =>
    post<{ job_id: string | null; use_ai: boolean }>(
      `/cases/${caseId}/timelines/${timelineId}/recommend-columns`,
      { use_ai: useAi },
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
