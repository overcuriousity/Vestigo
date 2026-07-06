import { describe, expect, it } from "vitest";
import { INVALIDATE_PREFIXES, shouldInvalidate } from "@/hooks/useCaseStream";

// Real query-key shapes as written in the components — if a component renames
// its key, this test is the tripwire that keeps SSE invalidation in sync.
const CASE = "case-1";
const ANNOTATION_SENSITIVE_KEYS: unknown[][] = [
  ["annotations", CASE, "t1"],
  ["tags", CASE],
  ["tags-merged", CASE, "t1"],
  ["histogram", CASE, "t1", { q: "" }], // TimelineHistogram
  ["anomalies-novelty", CASE, "t1", "self", "__auto__"], // ValueNoveltyView
  ["anomalies-frequency", CASE, "t1", "artifact", 3], // FrequencyView
  ["field-histogram", CASE, "t1", {}, 60], // FieldHistogramModal
  ["field-histogram-total", CASE, "t1", {}, 60],
  ["field-terms", CASE, "t1", "attr:user", {}],
  ["viz-field-terms", CASE, "t1", "attr:user", {}, 10], // VisualizePage
];

const UNAFFECTED_KEYS: unknown[][] = [
  ["anomaly-fields", CASE, "t1"], // cardinality inventory — annotation-independent
  ["semantic-search", CASE, "query", "t1"], // user-input driven
  ["similar", CASE, "evt-1", "t1"],
  ["cases"],
];

describe("useCaseStream invalidation predicate", () => {
  it("invalidates every annotation/tag-sensitive panel key for the case", () => {
    for (const key of ANNOTATION_SENSITIVE_KEYS) {
      expect(shouldInvalidate(key, CASE), key.join("/")).toBe(true);
    }
  });

  it("leaves annotation-independent and input-driven queries alone", () => {
    for (const key of UNAFFECTED_KEYS) {
      expect(shouldInvalidate(key, CASE), key.join("/")).toBe(false);
    }
  });

  it("never invalidates another case's queries", () => {
    for (const key of ANNOTATION_SENSITIVE_KEYS) {
      expect(shouldInvalidate([key[0], "other-case", ...key.slice(2)], CASE)).toBe(false);
    }
  });

  it("keeps the prefix list free of dead entries", () => {
    const known = new Set(ANNOTATION_SENSITIVE_KEYS.map((k) => k[0] as string));
    for (const prefix of INVALIDATE_PREFIXES) {
      expect(known.has(prefix), `unknown prefix: ${prefix}`).toBe(true);
    }
  });
});
