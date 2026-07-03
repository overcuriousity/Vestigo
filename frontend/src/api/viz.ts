import { del, get, patch, post } from "./client";
import { serializeEventFilterParams } from "@/lib/queryParams";
import type {
  CompareNumericResponse,
  CompareTermsResponse,
  CompareTimeResponse,
  EventFilters,
  FieldNumericResponse,
  FieldTermsResponse,
  FieldTimeseriesResponse,
  SavedChart,
  VizFieldsResponse,
} from "./types";

export type CompareMode =
  | { mode: "baseline" }
  | { mode: "custom"; filters: EventFilters };

/**
 * Field-value aggregations for the per-value histogram modal and the
 * Visualization page. Every call accepts the same `EventFilters` shape as
 * `eventsApi.list`/`eventsApi.histogram` so a chart always matches the
 * currently-filtered Explorer view.
 */
export const vizApi = {
  /** Every chartable field with distinct/coverage counts — unlike
   * `anomaliesApi.fields`, no novelty-detection heuristics are applied. */
  fields: (caseId: string, timelineId: string): Promise<VizFieldsResponse> =>
    get<VizFieldsResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/fields`),

  /** Top-N value/count terms aggregation for a field. */
  fieldTerms: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    limit = 50,
  ): Promise<FieldTermsResponse> =>
    get<FieldTermsResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-terms`, {
      ...serializeEventFilterParams(filters),
      field,
      limit,
    }),

  /** Summary statistics + fixed-width histogram for a numeric field. */
  fieldNumeric: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    bins = 30,
  ): Promise<FieldNumericResponse> =>
    get<FieldNumericResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-numeric`, {
      ...serializeEventFilterParams(filters),
      field,
      bins,
    }),

  /** Per-value event counts bucketed over time (top values only). */
  fieldTimeseries: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    buckets = 60,
    seriesLimit = 12,
  ): Promise<FieldTimeseriesResponse> =>
    get<FieldTimeseriesResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-timeseries`, {
      ...serializeEventFilterParams(filters),
      field,
      buckets,
      series_limit: seriesLimit,
    }),

  /**
   * Two-layer comparison against one server-computed shared grid. The body's
   * filter objects reuse the query-param field names (`serializeEventFilterParams`
   * output maps 1:1), so a compare layer is exactly an Explorer filter set.
   */
  compare: (
    caseId: string,
    timelineId: string,
    body: {
      kind: "time" | "terms" | "numeric";
      field?: string;
      primary: EventFilters;
      comparison: CompareMode;
      buckets?: number;
      bins?: number;
      limit?: number;
    },
  ): Promise<CompareTimeResponse | CompareTermsResponse | CompareNumericResponse> =>
    post(`/cases/${caseId}/timelines/${timelineId}/viz/compare`, {
      kind: body.kind,
      field: body.field,
      primary: serializeEventFilterParams(body.primary),
      comparison:
        body.comparison.mode === "custom"
          ? { mode: "custom", filters: serializeEventFilterParams(body.comparison.filters) }
          : { mode: "baseline" },
      buckets: body.buckets,
      bins: body.bins,
      limit: body.limit,
    }),
};

/** Saved chart configs, scoped to a timeline (patterned on saved Views). */
export const savedChartsApi = {
  list: (caseId: string, timelineId: string): Promise<{ charts: SavedChart[] }> =>
    get(`/cases/${caseId}/timelines/${timelineId}/viz/charts`),

  create: (
    caseId: string,
    timelineId: string,
    name: string,
    config: Record<string, unknown>,
  ): Promise<{ chart: SavedChart }> =>
    post(`/cases/${caseId}/timelines/${timelineId}/viz/charts`, { name, config }),

  rename: (
    caseId: string,
    timelineId: string,
    chartId: string,
    name: string,
  ): Promise<{ chart: SavedChart }> =>
    patch(`/cases/${caseId}/timelines/${timelineId}/viz/charts/${chartId}`, { name }),

  delete: (
    caseId: string,
    timelineId: string,
    chartId: string,
  ): Promise<{ deleted: boolean }> =>
    del(`/cases/${caseId}/timelines/${timelineId}/viz/charts/${chartId}`),
};
