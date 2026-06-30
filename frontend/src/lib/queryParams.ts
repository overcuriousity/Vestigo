/**
 * URL search-param serialization for filter state.
 * All filter state lives in the URL so investigation links are shareable.
 */
import type { EventFilters } from "@/api/types";

export function filtersToParams(filters: EventFilters): URLSearchParams {
  const p = new URLSearchParams();
  if (filters.q) p.set("q", filters.q);
  if (filters.artifact) p.set("artifact", filters.artifact);
  if (filters.sourceId) p.set("sourceId", filters.sourceId);
  if (filters.tag) p.set("tag", filters.tag);
  if (filters.excludeTag) p.set("excludeTag", filters.excludeTag);
  if (filters.start) p.set("start", filters.start);
  if (filters.end) p.set("end", filters.end);
  if (filters.filters && Object.keys(filters.filters).length > 0) {
    p.set("filters", JSON.stringify(filters.filters));
  }
  if (filters.exclusions && Object.keys(filters.exclusions).length > 0) {
    p.set("exclusions", JSON.stringify(filters.exclusions));
  }
  return p;
}

export function paramsToFilters(params: URLSearchParams): EventFilters {
  const filters: EventFilters = {};
  const q = params.get("q");
  const artifact = params.get("artifact");
  const sourceId = params.get("sourceId");
  const tag = params.get("tag");
  const excludeTag = params.get("excludeTag");
  const start = params.get("start");
  const end = params.get("end");
  const rawFilters = params.get("filters");
  const rawExclusions = params.get("exclusions");

  if (q) filters.q = q;
  if (artifact) filters.artifact = artifact;
  if (sourceId) filters.sourceId = sourceId;
  if (tag) filters.tag = tag;
  if (excludeTag) filters.excludeTag = excludeTag;
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
  return filters;
}

/** Serialize filters into a plain Record suitable for storing in a View. */
export function filtersToViewPayload(
  filters: EventFilters,
): Record<string, unknown> {
  return {
    q: filters.q ?? null,
    artifact: filters.artifact ?? null,
    sourceId: filters.sourceId ?? null,
    tag: filters.tag ?? null,
    excludeTag: filters.excludeTag ?? null,
    start: filters.start ?? null,
    end: filters.end ?? null,
    filters: filters.filters ?? {},
    exclusions: filters.exclusions ?? {},
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
  if (typeof payload.sourceId === "string" && payload.sourceId)
    f.sourceId = payload.sourceId;
  if (typeof payload.tag === "string" && payload.tag) f.tag = payload.tag;
  if (typeof payload.excludeTag === "string" && payload.excludeTag)
    f.excludeTag = payload.excludeTag;
  if (typeof payload.start === "string" && payload.start) f.start = payload.start;
  if (typeof payload.end === "string" && payload.end) f.end = payload.end;
  if (payload.filters && typeof payload.filters === "object") {
    f.filters = payload.filters as Record<string, string>;
  }
  if (payload.exclusions && typeof payload.exclusions === "object") {
    f.exclusions = payload.exclusions as Record<string, string[]>;
  }
  return f;
}
