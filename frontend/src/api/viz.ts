import { get } from "./client";
import { serializeEventFilterFields } from "@/lib/queryParams";
import type {
  EventFilters,
  FieldNumericResponse,
  FieldTermsResponse,
  FieldTimeseriesResponse,
} from "./types";

/**
 * Field-value aggregations for the per-value histogram modal and the
 * Visualization page. Every call accepts the same `EventFilters` shape as
 * `eventsApi.list`/`eventsApi.histogram` so a chart always matches the
 * currently-filtered Explorer view.
 */
export const vizApi = {
  /** Top-N value/count terms aggregation for a field. */
  fieldTerms: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    limit = 50,
  ): Promise<FieldTermsResponse> => {
    const params: Record<string, string | number | undefined | null> = {
      ...serializeEventFilterFields(filters),
      field,
      limit,
    };
    if (filters.filters && Object.keys(filters.filters).length > 0) {
      params.filters = JSON.stringify(filters.filters);
    }
    if (filters.exclusions && Object.keys(filters.exclusions).length > 0) {
      params.exclusions = JSON.stringify(filters.exclusions);
    }
    return get<FieldTermsResponse>(
      `/cases/${caseId}/timelines/${timelineId}/viz/field-terms`,
      params,
    );
  },

  /** Summary statistics + fixed-width histogram for a numeric field. */
  fieldNumeric: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    bins = 30,
  ): Promise<FieldNumericResponse> => {
    const params: Record<string, string | number | undefined | null> = {
      ...serializeEventFilterFields(filters),
      field,
      bins,
    };
    if (filters.filters && Object.keys(filters.filters).length > 0) {
      params.filters = JSON.stringify(filters.filters);
    }
    if (filters.exclusions && Object.keys(filters.exclusions).length > 0) {
      params.exclusions = JSON.stringify(filters.exclusions);
    }
    return get<FieldNumericResponse>(
      `/cases/${caseId}/timelines/${timelineId}/viz/field-numeric`,
      params,
    );
  },

  /** Per-value event counts bucketed over time (top values only). */
  fieldTimeseries: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    buckets = 60,
    seriesLimit = 12,
  ): Promise<FieldTimeseriesResponse> => {
    const params: Record<string, string | number | undefined | null> = {
      ...serializeEventFilterFields(filters),
      field,
      buckets,
      series_limit: seriesLimit,
    };
    if (filters.filters && Object.keys(filters.filters).length > 0) {
      params.filters = JSON.stringify(filters.filters);
    }
    if (filters.exclusions && Object.keys(filters.exclusions).length > 0) {
      params.exclusions = JSON.stringify(filters.exclusions);
    }
    return get<FieldTimeseriesResponse>(
      `/cases/${caseId}/timelines/${timelineId}/viz/field-timeseries`,
      params,
    );
  },
};
