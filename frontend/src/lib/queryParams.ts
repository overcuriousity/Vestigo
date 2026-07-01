/**
 * URL search-param serialization for filter state.
 * All filter state lives in the URL so investigation links are shareable.
 */
import type { EventFilters } from "@/api/types";

export function filtersToParams(filters: EventFilters): URLSearchParams {
  const p = new URLSearchParams();
  if (filters.q) p.set("q", filters.q);
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
  if (annotated) {
    filters.annotated = annotated
      .split(",")
      .map((t) => t.trim())
      .filter((t): t is "tag" | "anomaly" => t === "tag" || t === "anomaly");
  }
  if (annotationTagValue) filters.annotationTagValue = annotationTagValue;
  return filters;
}

/** Serialize filters into a plain Record suitable for storing in a View. */
export function filtersToViewPayload(
  filters: EventFilters,
): Record<string, unknown> {
  return {
    q: filters.q ?? null,
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
  if (payload.filters && typeof payload.filters === "object") {
    f.filters = payload.filters as Record<string, string>;
  }
  if (payload.exclusions && typeof payload.exclusions === "object") {
    f.exclusions = payload.exclusions as Record<string, string[]>;
  }
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
