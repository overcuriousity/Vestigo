/**
 * URL search-param serialization for filter state.
 * All filter state lives in the URL so investigation links are shareable.
 */
import type { EventFilters, FieldMatchMode } from "@/api/types";

/** Sanitize an untrusted parsed object into a match-mode map.
 *
 * Drops anything that isn't "wildcard"/"regex" — including explicit "exact"
 * (absence already means exact) and unknown strings from hand-edited URLs
 * or legacy payloads, so downstream code never sees an invalid mode. */
function sanitizeModes(raw: unknown): Record<string, FieldMatchMode> | undefined {
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return undefined;
  const out: Record<string, FieldMatchMode> = {};
  for (const [k, v] of Object.entries(raw)) {
    if (v === "wildcard" || v === "regex") out[k] = v;
  }
  return Object.keys(out).length > 0 ? out : undefined;
}

/** Scalar/comma-joined API fields shared by every request that carries
 * `EventFilters` — GET query params (events list, histogram) and JSON POST
 * bodies that mirror the same param names (bulk-annotate, export). */
export interface SerializedEventFilterFields {
  q?: string;
  q_regex?: boolean;
  artifact?: string;
  artifacts?: string;
  source_id?: string;
  tag?: string;
  exclude_tag?: string;
  tags_include?: string;
  tags_exclude?: string;
  ids?: string;
  start?: string;
  end?: string;
  annotated?: string;
  annotation_tag_value?: string;
  run_id?: string;
}

/**
 * Serialize the scalar/comma-joined fields of `EventFilters` shared by
 * `eventsApi.list`, `eventsApi.histogram`, `annotationsApi.bulkByFilter`, and
 * `downloadExport` — previously reimplemented ~13 fields deep in each of the
 * four, with real risk of one being updated and the others silently missing
 * a new filter.
 *
 * Deliberately excludes `filters`/`exclusions` (the object-shaped field
 * filter/exclusion maps): whether those get JSON-stringified (query params,
 * bulk-annotate's JSON body mirroring query-param conventions) or sent as
 * raw objects (export's already-structured JSON body) depends on the
 * transport, so each caller still sets those itself.
 */
export function serializeEventFilterFields(
  filters: EventFilters,
): SerializedEventFilterFields {
  const out: SerializedEventFilterFields = {};
  if (filters.q) out.q = filters.q;
  // q_regex only applies to the server-side keyword search — in semantic
  // mode `q` is replaced by result ids client-side before any request.
  if (filters.q && filters.qRegex && filters.qMode !== "semantic") out.q_regex = true;
  if (filters.artifact) out.artifact = filters.artifact;
  if (filters.artifacts && filters.artifacts.length > 0) {
    out.artifacts = filters.artifacts.join(",");
  }
  if (filters.sourceId) out.source_id = filters.sourceId;
  if (filters.tag) out.tag = filters.tag;
  if (filters.excludeTag) out.exclude_tag = filters.excludeTag;
  if (filters.tagsInclude && filters.tagsInclude.length > 0) {
    out.tags_include = filters.tagsInclude.join(",");
  }
  if (filters.tagsExclude && filters.tagsExclude.length > 0) {
    out.tags_exclude = filters.tagsExclude.join(",");
  }
  if (filters.ids && filters.ids.length > 0) {
    out.ids = filters.ids.join(",");
  }
  if (filters.start) out.start = filters.start;
  if (filters.end) out.end = filters.end;
  if (filters.annotated && filters.annotated.length > 0) {
    out.annotated = filters.annotated.join(",");
  }
  if (filters.annotationTagValue) out.annotation_tag_value = filters.annotationTagValue;
  if (filters.anomalyRunId) out.run_id = filters.anomalyRunId;
  return out;
}

/**
 * Query-param transport shape of `EventFilters`: the shared scalar fields
 * plus the object-shaped `filters`/`exclusions` maps JSON-stringified, as
 * every GET-style request (events list, histogram, the viz aggregations)
 * sends them. Callers with a structured JSON body (export) keep using
 * `serializeEventFilterFields` and attach the maps as raw objects.
 */
export function serializeEventFilterParams(
  filters: EventFilters,
): SerializedEventFilterFields & {
  filters?: string;
  exclusions?: string;
  filter_modes?: string;
  exclusion_modes?: string;
} {
  const out: ReturnType<typeof serializeEventFilterParams> = {
    ...serializeEventFilterFields(filters),
  };
  if (filters.filters && Object.keys(filters.filters).length > 0) {
    out.filters = JSON.stringify(filters.filters);
  }
  if (filters.exclusions && Object.keys(filters.exclusions).length > 0) {
    out.exclusions = JSON.stringify(filters.exclusions);
  }
  if (filters.filterModes && Object.keys(filters.filterModes).length > 0) {
    out.filter_modes = JSON.stringify(filters.filterModes);
  }
  if (filters.exclusionModes && Object.keys(filters.exclusionModes).length > 0) {
    out.exclusion_modes = JSON.stringify(filters.exclusionModes);
  }
  return out;
}

export function filtersToParams(filters: EventFilters): URLSearchParams {
  const p = new URLSearchParams();
  if (filters.q) p.set("q", filters.q);
  if (filters.qMode) p.set("qMode", filters.qMode);
  if (filters.qRegex) p.set("qRegex", "1");
  if (filters.artifact) p.set("artifact", filters.artifact);
  if (filters.artifacts && filters.artifacts.length > 0) {
    p.set("artifacts", filters.artifacts.join(","));
  }
  if (filters.sourceId) p.set("sourceId", filters.sourceId);
  if (filters.tag) p.set("tag", filters.tag);
  if (filters.excludeTag) p.set("excludeTag", filters.excludeTag);
  if (filters.tagsInclude && filters.tagsInclude.length > 0) {
    p.set("tagsInclude", filters.tagsInclude.join(","));
  }
  if (filters.tagsExclude && filters.tagsExclude.length > 0) {
    p.set("tagsExclude", filters.tagsExclude.join(","));
  }
  if (filters.start) p.set("start", filters.start);
  if (filters.end) p.set("end", filters.end);
  if (filters.filters && Object.keys(filters.filters).length > 0) {
    p.set("filters", JSON.stringify(filters.filters));
  }
  if (filters.exclusions && Object.keys(filters.exclusions).length > 0) {
    p.set("exclusions", JSON.stringify(filters.exclusions));
  }
  if (filters.filterModes && Object.keys(filters.filterModes).length > 0) {
    p.set("filterModes", JSON.stringify(filters.filterModes));
  }
  if (filters.exclusionModes && Object.keys(filters.exclusionModes).length > 0) {
    p.set("exclusionModes", JSON.stringify(filters.exclusionModes));
  }
  if (filters.annotated && filters.annotated.length > 0) {
    p.set("annotated", filters.annotated.join(","));
  }
  if (filters.annotationTagValue) {
    p.set("annotationTagValue", filters.annotationTagValue);
  }
  return p;
}

export function paramsToFilters(params: URLSearchParams): EventFilters {
  const filters: EventFilters = {};
  const q = params.get("q");
  const artifact = params.get("artifact");
  const artifacts = params.get("artifacts");
  const sourceId = params.get("sourceId");
  const tag = params.get("tag");
  const excludeTag = params.get("excludeTag");
  const tagsInclude = params.get("tagsInclude");
  const tagsExclude = params.get("tagsExclude");
  const start = params.get("start");
  const end = params.get("end");
  const rawFilters = params.get("filters");
  const rawExclusions = params.get("exclusions");
  const annotated = params.get("annotated");
  const annotationTagValue = params.get("annotationTagValue");

  if (q) filters.q = q;
  if (params.get("qMode") === "semantic") filters.qMode = "semantic";
  if (params.get("qRegex") === "1" && filters.qMode !== "semantic") filters.qRegex = true;
  if (artifact) filters.artifact = artifact;
  if (artifacts) {
    filters.artifacts = artifacts.split(",").map((a) => a.trim()).filter(Boolean);
  }
  if (sourceId) filters.sourceId = sourceId;
  if (tag) filters.tag = tag;
  if (excludeTag) filters.excludeTag = excludeTag;
  if (tagsInclude) {
    filters.tagsInclude = tagsInclude.split(",").map((t) => t.trim()).filter(Boolean);
  }
  if (tagsExclude) {
    filters.tagsExclude = tagsExclude.split(",").map((t) => t.trim()).filter(Boolean);
  }
  if (start) filters.start = start;
  if (end) filters.end = end;
  if (rawFilters) {
    try {
      filters.filters = JSON.parse(rawFilters);
    } catch {
      // ignore malformed
    }
  }
  if (rawExclusions) {
    try {
      filters.exclusions = JSON.parse(rawExclusions);
    } catch {
      // ignore malformed
    }
  }
  const rawFilterModes = params.get("filterModes");
  if (rawFilterModes) {
    try {
      filters.filterModes = sanitizeModes(JSON.parse(rawFilterModes));
    } catch {
      // ignore malformed
    }
  }
  const rawExclusionModes = params.get("exclusionModes");
  if (rawExclusionModes) {
    try {
      filters.exclusionModes = sanitizeModes(JSON.parse(rawExclusionModes));
    } catch {
      // ignore malformed
    }
  }
  if (annotated) {
    filters.annotated = annotated
      .split(",")
      .map((t) => t.trim())
      .filter((t): t is "tag" | "anomaly" => t === "tag" || t === "anomaly");
  }
  if (annotationTagValue) filters.annotationTagValue = annotationTagValue;
  return filters;
}

/** Serialize filters into a plain Record suitable for storing in a View.
 *
 * `qMode`/`qRegex` are part of the payload so a saved view reproduces the
 * exact search semantics (keyword vs semantic, literal vs regex) — a
 * forensic-reproducibility requirement, not a convenience. */
export function filtersToViewPayload(
  filters: EventFilters,
): Record<string, unknown> {
  return {
    q: filters.q ?? null,
    qMode: filters.qMode ?? null,
    qRegex: filters.qRegex ?? false,
    artifact: filters.artifact ?? null,
    artifacts: filters.artifacts ?? [],
    sourceId: filters.sourceId ?? null,
    tag: filters.tag ?? null,
    excludeTag: filters.excludeTag ?? null,
    tagsInclude: filters.tagsInclude ?? [],
    tagsExclude: filters.tagsExclude ?? [],
    start: filters.start ?? null,
    end: filters.end ?? null,
    filters: filters.filters ?? {},
    exclusions: filters.exclusions ?? {},
    // Match modes are part of the payload for the same reason as qRegex:
    // a saved view must reproduce the exact match semantics.
    filterModes: filters.filterModes ?? {},
    exclusionModes: filters.exclusionModes ?? {},
    annotated: filters.annotated ?? [],
    annotationTagValue: filters.annotationTagValue ?? null,
  };
}

/** Deserialize a View's filter payload back to EventFilters. */
export function viewPayloadToFilters(
  payload: Record<string, unknown>,
): EventFilters {
  const f: EventFilters = {};
  if (typeof payload.q === "string" && payload.q) f.q = payload.q;
  // Legacy payloads predate these keys — absent means keyword, non-regex.
  if (payload.qMode === "semantic") f.qMode = "semantic";
  if (payload.qRegex === true && f.qMode !== "semantic") f.qRegex = true;
  if (typeof payload.artifact === "string" && payload.artifact)
    f.artifact = payload.artifact;
  if (Array.isArray(payload.artifacts) && payload.artifacts.length > 0) {
    f.artifacts = (payload.artifacts as unknown[]).filter(
      (a): a is string => typeof a === "string" && a.length > 0,
    );
  }
  if (typeof payload.sourceId === "string" && payload.sourceId)
    f.sourceId = payload.sourceId;
  if (typeof payload.tag === "string" && payload.tag) f.tag = payload.tag;
  if (typeof payload.excludeTag === "string" && payload.excludeTag)
    f.excludeTag = payload.excludeTag;
  if (Array.isArray(payload.tagsInclude) && payload.tagsInclude.length > 0) {
    f.tagsInclude = (payload.tagsInclude as unknown[]).filter(
      (t): t is string => typeof t === "string" && t.length > 0,
    );
  }
  if (Array.isArray(payload.tagsExclude) && payload.tagsExclude.length > 0) {
    f.tagsExclude = (payload.tagsExclude as unknown[]).filter(
      (t): t is string => typeof t === "string" && t.length > 0,
    );
  }
  if (typeof payload.start === "string" && payload.start) f.start = payload.start;
  if (typeof payload.end === "string" && payload.end) f.end = payload.end;
  if (
    payload.filters &&
    typeof payload.filters === "object" &&
    Object.keys(payload.filters).length > 0
  ) {
    f.filters = payload.filters as Record<string, string>;
  }
  if (
    payload.exclusions &&
    typeof payload.exclusions === "object" &&
    Object.keys(payload.exclusions).length > 0
  ) {
    f.exclusions = payload.exclusions as Record<string, string[]>;
  }
  // Legacy payloads predate match modes — absent means exact for every field.
  const viewFilterModes = sanitizeModes(payload.filterModes);
  if (viewFilterModes) f.filterModes = viewFilterModes;
  const viewExclusionModes = sanitizeModes(payload.exclusionModes);
  if (viewExclusionModes) f.exclusionModes = viewExclusionModes;
  if (Array.isArray(payload.annotated) && payload.annotated.length > 0) {
    f.annotated = (payload.annotated as string[]).filter(
      (t): t is "tag" | "anomaly" => t === "tag" || t === "anomaly",
    );
  }
  if (typeof payload.annotationTagValue === "string" && payload.annotationTagValue) {
    f.annotationTagValue = payload.annotationTagValue;
  }
  return f;
}
