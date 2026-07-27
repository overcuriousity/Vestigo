/**
 * Pure filter-mutation helpers shared by every "click a value → filter on
 * it" surface: the Explorer's detail panel and grid cells, the anomaly
 * views' drill-downs, and the Visualize page's click-to-filter charts.
 * Extracted from ExplorerPage so both pages apply byte-identical semantics
 * (same special cases, same match-mode resets).
 */
import type { EventFilters, FieldMatchMode } from "@/api/types";

/** Remove `key`'s match-mode entry; collapse to undefined when the map empties. */
export function dropMode(
  modes: Record<string, FieldMatchMode> | undefined,
  key: string,
): Record<string, FieldMatchMode> | undefined {
  if (!modes || !(key in modes)) return modes;
  const { [key]: _removed, ...rest } = modes;
  return Object.keys(rest).length > 0 ? rest : undefined;
}

/**
 * Return a copy of *f* with `fieldKey = value` applied as an include or
 * exclude filter. Special cases:
 *   - filterKey "q"        → sets the free-text search (include only)
 *   - filterKey "artifact" → sets the dedicated artifact param (include only)
 *   - filterKey "tag"      → sets the dedicated tag param
 *   - everything else      → goes into filters{} or exclusions{}
 * Values are literal — any glob/regex match mode on the key is reset so the
 * clicked text is never reinterpreted as a pattern.
 */
export function applyFieldFilter(
  f: EventFilters,
  fieldKey: string,
  value: string,
  include: boolean,
): EventFilters {
  const next = { ...f };

  if (fieldKey === "q") {
    // Full-text search: always "include" (no exclusion concept for free text)
    next.q = value;
  } else if (fieldKey === "artifact") {
    if (include) {
      next.artifact = value;
    } else {
      const prev = next.exclusions?.artifact ?? [];
      if (!prev.includes(value)) {
        next.exclusions = { ...(next.exclusions ?? {}) as Record<string, string[]>, artifact: [...prev, value] };
      }
    }
  } else if (fieldKey === "tag") {
    if (include) {
      next.tag = value;
    } else {
      next.excludeTag = value;
    }
  } else if (include) {
    const prev = next.filters?.[fieldKey] ?? [];
    if (!prev.includes(value)) {
      next.filters = { ...(next.filters ?? {}), [fieldKey]: [...prev, value] };
    }
    // Clicked values are literal — reset any pattern mode on the key,
    // otherwise the clicked text would be reinterpreted as glob/regex.
    next.filterModes = dropMode(next.filterModes, fieldKey);
  } else {
    const prev = next.exclusions?.[fieldKey] ?? [];
    if (!prev.includes(value)) {
      next.exclusions = { ...(next.exclusions ?? {}) as Record<string, string[]>, [fieldKey]: [...prev, value] };
      // Same literal-value rule; mode is per key, so this also flips any
      // pre-existing pattern-mode values of the key back to exact —
      // visible via the chips' badge disappearing.
      next.exclusionModes = dropMode(next.exclusionModes, fieldKey);
    }
  }

  return next;
}

/**
 * Does *f* narrow the event set at all?
 *
 * "At all" means exactly "`FilterChips` would render at least one chip" — the
 * two must agree, because every surface that asks this question does so in
 * order to decide between showing chips and showing an empty state. Keep this
 * list in step with `components/explorer/FilterChips.tsx`.
 *
 * Time bounds count: a chart of one brushed hour is not an unfiltered chart.
 * Presentation-only members (`limit`, `sort`, the match-mode maps, and
 * `collapseRoutine`, which is not URL-serialized) deliberately do not.
 */
export function hasActiveFilters(f: EventFilters): boolean {
  return Boolean(
    f.q ||
      f.artifact ||
      f.artifacts?.length ||
      f.sourceId ||
      f.tag ||
      f.tagsInclude?.length ||
      f.tagsExclude?.length ||
      f.annotated?.length ||
      f.start ||
      f.end ||
      Object.keys(f.filters ?? {}).length ||
      Object.keys(f.exclusions ?? {}).length,
  );
}

/**
 * Return a copy of *f* with one filter chip's contribution removed.
 *
 * `key` names the `EventFilters` member; `fieldKey` picks the entry inside the
 * `filters`/`exclusions` maps; `value` removes a single value from a multi-value
 * entry (omit it to drop the whole entry). Emptying an entry also drops its
 * match-mode, so a re-added value is never silently reinterpreted as a pattern.
 *
 * Shared by the Explorer's filter chips and Visualize's inherited-filters bar:
 * both surfaces render the same chips, so removal must mean the same thing.
 */
export function removeFilterEntry(
  f: EventFilters,
  key: keyof EventFilters | string,
  fieldKey?: string,
  value?: string,
): EventFilters {
  const next = { ...f };
  if (key === "filters" && fieldKey) {
    if (value !== undefined) {
      const remaining = (next.filters?.[fieldKey] ?? []).filter((v) => v !== value);
      if (remaining.length === 0) {
        const { [fieldKey]: _removed, ...rest } = next.filters ?? {};
        next.filters = rest;
        next.filterModes = dropMode(next.filterModes, fieldKey);
      } else {
        next.filters = { ...(next.filters ?? {}), [fieldKey]: remaining };
      }
    } else {
      const { [fieldKey]: _removed, ...rest } = next.filters ?? {};
      next.filters = rest;
      next.filterModes = dropMode(next.filterModes, fieldKey);
    }
  } else if (key === "exclusions" && fieldKey) {
    if (value !== undefined) {
      const remaining = (next.exclusions?.[fieldKey] ?? []).filter((v) => v !== value);
      if (remaining.length === 0) {
        const { [fieldKey]: _removed, ...rest } = next.exclusions ?? {};
        next.exclusions = rest;
        next.exclusionModes = dropMode(next.exclusionModes, fieldKey);
      } else {
        next.exclusions = {
          ...((next.exclusions ?? {}) as Record<string, string[]>),
          [fieldKey]: remaining,
        };
      }
    } else {
      const { [fieldKey]: _removed, ...rest } = next.exclusions ?? {};
      next.exclusions = rest;
      next.exclusionModes = dropMode(next.exclusionModes, fieldKey);
    }
  } else if (key === "artifacts" || key === "tagsInclude" || key === "tagsExclude") {
    const listKey = key as "artifacts" | "tagsInclude" | "tagsExclude";
    const remaining =
      value !== undefined ? (next[listKey] ?? []).filter((v) => v !== value) : [];
    if (remaining.length > 0) next[listKey] = remaining;
    else delete next[listKey];
  } else if (key === "annotated") {
    const remaining =
      value !== undefined ? (next.annotated ?? []).filter((t) => t !== value) : [];
    if (remaining.length > 0) {
      next.annotated = remaining;
    } else {
      delete next.annotated;
      // The tag-value refinement only means anything alongside `annotated`.
      delete next.annotationTagValue;
    }
  } else {
    delete next[key as keyof EventFilters];
  }
  return next;
}

/** Maps a backend field token (viz/anomaly form) to a filter-rail filterKey:
 * `attr:status_code` → `status_code`, `tags` → `tag`, top-level columns as-is. */
export function mapFieldTokenToFilterKey(field: string): string {
  if (field.startsWith("attr:")) return field.slice(5);
  if (field === "tags") return "tag";
  return field;
}

/** Apply several `[fieldToken, value]` pairs as one conjunctive update —
 * folding over one filters object instead of looping a setter avoids each
 * call clobbering the previous against the same stale snapshot. */
export function applyFieldEntries(
  f: EventFilters,
  entries: [string, string][],
  include: boolean,
): EventFilters {
  let next = f;
  for (const [field, value] of entries) {
    next = applyFieldFilter(next, mapFieldTokenToFilterKey(field), value, include);
  }
  return next;
}
