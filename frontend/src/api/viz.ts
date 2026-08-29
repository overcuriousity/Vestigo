import { del, get, patch, post } from "./client";
import { deriveToParam } from "@/components/viz/lib/derive";
import type { DeriveSpec, MarkSource } from "@/components/viz/lib/chartConfig";
import { marksToPayload } from "@/components/viz/lib/chartConfig";
import { serializeEventFilterParams } from "@/lib/queryParams";
import type {
  CalendarResponse,
  ChangeResponse,
  CumulativeQuantity,
  CumulativeResponse,
  CompareNumericResponse,
  CompareTermsResponse,
  CompareTimeResponse,
  EventFilters,
  FieldCorrelationResponse,
  FieldNumericGroupedResponse,
  FieldNumericResponse,
  FieldPivotResponse,
  FieldTableResponse,
  LanesResponse,
  TableSortColumnWire,
  FieldScatterResponse,
  FieldTermsResponse,
  FieldTimeseriesResponse,
  PunchcardResponse,
  ResolvedMarksResponse,
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

  /** Top-N value/count terms aggregation for a field.
   *
   * `totals: false` drops the server's second scan over the field's whole
   * distribution. `total`/`distinct` then describe the returned rows only and
   * `other_count` is 0, so it is for callers that read `values` and nothing
   * else — never for a chart that renders an "Other" slice.
   *
   * `derive` groups the field's ranges or calendar part instead of its raw
   * values; the response then carries a `derive` echo with the labels/edges. */
  fieldTerms: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    limit = 50,
    opts: { totals?: boolean; derive?: DeriveSpec | null } = {},
  ): Promise<FieldTermsResponse> =>
    get<FieldTermsResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-terms`, {
      ...serializeEventFilterParams(filters),
      field,
      limit,
      ...(opts.totals === false ? { totals: false } : {}),
      ...(opts.derive ? { derive: deriveToParam(opts.derive) } : {}),
    }),

  /** Summary statistics + fixed-width histogram for a numeric field. */
  fieldNumeric: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    bins: number | null = null,
    points = false,
  ): Promise<FieldNumericResponse> =>
    get<FieldNumericResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-numeric`, {
      ...serializeEventFilterParams(filters),
      field,
      // bins omitted → server picks Freedman–Diaconis automatically.
      ...(bins != null ? { bins } : {}),
      ...(points ? { points: true } : {}),
    }),

  /** Pairwise correlations across 2–8 numeric fields. */
  fieldCorrelation: (
    caseId: string,
    timelineId: string,
    fields: string[],
    filters: EventFilters = {},
  ): Promise<FieldCorrelationResponse> =>
    get<FieldCorrelationResponse>(
      `/cases/${caseId}/timelines/${timelineId}/viz/field-correlation`,
      { ...serializeEventFilterParams(filters), fields },
    ),

  /** Per-group numeric distributions — grouped box/violin plots. */
  fieldNumericGrouped: (
    caseId: string,
    timelineId: string,
    field: string,
    groupField: string,
    filters: EventFilters = {},
    groups = 8,
    bins = 30,
    points = false,
  ): Promise<FieldNumericGroupedResponse> =>
    get<FieldNumericGroupedResponse>(
      `/cases/${caseId}/timelines/${timelineId}/viz/field-numeric-grouped`,
      {
        ...serializeEventFilterParams(filters),
        field,
        group_field: groupField,
        groups,
        bins,
        ...(points ? { points: true } : {}),
      },
    ),

  /** Per-value event counts bucketed over time (top values only). */
  fieldTimeseries: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    buckets = 60,
    seriesLimit = 12,
    derive: DeriveSpec | null = null,
  ): Promise<FieldTimeseriesResponse> =>
    get<FieldTimeseriesResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-timeseries`, {
      ...serializeEventFilterParams(filters),
      field,
      buckets,
      series_limit: seriesLimit,
      ...(derive ? { derive: deriveToParam(derive) } : {}),
    }),

  /** Event counts by (day-of-week × hour-of-day), UTC — the punch-card chart. */
  punchcard: (
    caseId: string,
    timelineId: string,
    filters: EventFilters = {},
  ): Promise<PunchcardResponse> =>
    get<PunchcardResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/time-punchcard`, {
      ...serializeEventFilterParams(filters),
    }),

  /** Running total over time — events, a measure's sum, or distinct values so far. */
  cumulative: (
    caseId: string,
    timelineId: string,
    filters: EventFilters = {},
    opts: { field?: string | null; quantity?: CumulativeQuantity; buckets?: number } = {},
  ): Promise<CumulativeResponse> =>
    get<CumulativeResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/cumulative`, {
      ...serializeEventFilterParams(filters),
      ...(opts.field ? { field: opts.field } : {}),
      ...(opts.quantity ? { quantity: opts.quantity } : {}),
      ...(opts.buckets ? { buckets: opts.buckets } : {}),
    }),

  /** Event count per UTC day, latest 53 weeks. */
  calendar: (
    caseId: string,
    timelineId: string,
    filters: EventFilters = {},
    opts: { field?: string | null } = {},
  ): Promise<CalendarResponse> =>
    get<CalendarResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/calendar`, {
      ...serializeEventFilterParams(filters),
      ...(opts.field ? { field: opts.field } : {}),
    }),

  /** Top-X × top-Y co-occurrence matrix — feeds the pivot heatmap and Sankey flow. */
  fieldPivot: (
    caseId: string,
    timelineId: string,
    fieldX: string,
    fieldY: string,
    filters: EventFilters = {},
    limitX = 10,
    limitY = 10,
    deriveX: DeriveSpec | null = null,
  ): Promise<FieldPivotResponse> =>
    get<FieldPivotResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-pivot`, {
      ...serializeEventFilterParams(filters),
      field_x: fieldX,
      field_y: fieldY,
      limit_x: limitX,
      limit_y: limitY,
      ...(deriveX ? { derive_x: deriveToParam(deriveX) } : {}),
    }),

  /** Top-N values of a field as table rows: count, share, first/last seen and,
   * with `secondField`, the distinct count of that field per row — the table
   * figure. A `remainder` row is present whenever values were cut. */
  fieldTable: (
    caseId: string,
    timelineId: string,
    field: string,
    filters: EventFilters = {},
    limit = 50,
    opts: {
      secondField?: string | null;
      sortBy?: TableSortColumnWire;
      sortDir?: "asc" | "desc";
      derive?: DeriveSpec | null;
    } = {},
  ): Promise<FieldTableResponse> =>
    get<FieldTableResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-table`, {
      ...serializeEventFilterParams(filters),
      field,
      limit,
      ...(opts.secondField ? { second_field: opts.secondField } : {}),
      ...(opts.sortBy ? { sort_by: opts.sortBy } : {}),
      ...(opts.sortDir ? { sort_dir: opts.sortDir } : {}),
      ...(opts.derive ? { derive: deriveToParam(opts.derive) } : {}),
    }),

  /** Resolve a chart's mark sources into instants/ranges with provenance.
   * Posts the stored `MarkSource` shape verbatim (`marksToPayload`) — the
   * same bytes `c_marks` and a saved chart carry. */
  resolveMarks: (
    caseId: string,
    timelineId: string,
    marks: MarkSource[],
  ): Promise<ResolvedMarksResponse> =>
    post<ResolvedMarksResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/marks`, {
      marks: marksToPayload(marks),
    }),

  /** Uniform random sample of numeric (x, y) pairs for the scatter plot. */
  fieldScatter: (
    caseId: string,
    timelineId: string,
    fieldX: string,
    fieldY: string,
    filters: EventFilters = {},
    limit = 5000,
  ): Promise<FieldScatterResponse> =>
    get<FieldScatterResponse>(`/cases/${caseId}/timelines/${timelineId}/viz/field-scatter`, {
      ...serializeEventFilterParams(filters),
      field_x: fieldX,
      field_y: fieldY,
      limit,
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
      kind: "time" | "terms" | "numeric" | "change";
      field?: string;
      primary: EventFilters;
      comparison: CompareMode;
      buckets?: number;
      bins?: number;
      limit?: number;
      /** kinds "terms" and "change" — both layers are counted on the primary's edges. */
      derive?: DeriveSpec | null;
    },
  ): Promise<
    CompareTimeResponse | CompareTermsResponse | CompareNumericResponse | ChangeResponse
  > =>
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
      derive: body.derive ? JSON.parse(deriveToParam(body.derive)!) : undefined,
    }),
  /** Interval lanes — a POST because three filter sets do not fit query params. */
  lanes: (
    caseId: string,
    timelineId: string,
    body: {
      field: string;
      pairing: "firstLast" | "nextEnd";
      primary: EventFilters;
      startFilter?: EventFilters;
      endFilter?: EventFilters;
      limitY: number;
    },
  ): Promise<LanesResponse> =>
    post(`/cases/${caseId}/timelines/${timelineId}/viz/lanes`, {
      field: body.field,
      pairing: body.pairing === "nextEnd" ? "next_end" : "first_last",
      primary: serializeEventFilterParams(body.primary),
      start_filter: body.startFilter ? serializeEventFilterParams(body.startFilter) : undefined,
      end_filter: body.endFilter ? serializeEventFilterParams(body.endFilter) : undefined,
      limit_y: body.limitY,
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
