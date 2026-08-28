/**
 * A `useQuery` against a gated scan endpoint must opt into `busyRetry`.
 *
 * The chart lane (#300) answers 503 after a bounded wait instead of queueing
 * forever, which turns "slow" into "failed" for any caller that does not
 * retry. That is a *per-call-site* opt-in with no type to enforce it, and the
 * gap it leaves is invisible in review: `VisualizePage` — the surface with
 * eleven gated queries, more than the rest of the app together — shipped
 * without it while every smaller surface was wired up, so a busy lane put an
 * error toast on the main Visualize tab where the old unbounded wait had
 * always produced an answer.
 *
 * File granularity on purpose. Matching a spread to the `useQuery` it belongs
 * to needs a parser, and the failure this guards against is a whole surface
 * that was never considered, not one query in a file that already knows about
 * the lane.
 */
import { describe, it, expect } from "vitest";

// Raw glob rather than node:fs — the frontend tsconfig carries no node types.
// Same constraint and same solution as guidanceCoverage.test.ts.
const SOURCES = {
  ...(import.meta.glob("../components/**/*.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
  ...(import.meta.glob("../pages/**/*.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
};

/**
 * The client calls that reach a `_foreground_scan` in `db/queries.py`. Keep in
 * step with that decorator: a new gated aggregation added here starts failing
 * the surfaces that call it without retrying, which is the point.
 */
const GATED_CALLS = [
  "eventsApi.histogram(",
  "vizApi.fieldTerms(",
  "vizApi.fieldNumeric(",
  "vizApi.fieldNumericGrouped(",
  "vizApi.fieldCorrelation(",
  "vizApi.fieldTimeseries(",
  "vizApi.punchcard(",
  "vizApi.fieldPivot(",
  "vizApi.fieldScatter(",
  "vizApi.compare(",
];

describe("busyRetry coverage", () => {
  it("every component issuing a gated scan through useQuery opts into busyRetry", () => {
    const missing: string[] = [];
    for (const [path, source] of Object.entries(SOURCES)) {
      if (!source.includes("useQuery(")) continue;
      const gated = GATED_CALLS.filter((call) => source.includes(call));
      if (gated.length === 0) continue;
      if (!source.includes("...busyRetry")) missing.push(`${path} (${gated.join(", ")})`);
    }
    expect(missing).toEqual([]);
  });

  it("recognizes at least the surfaces that are known to be gated", () => {
    // Guards the guard: a rename that stops every pattern from matching would
    // otherwise leave the test above passing over an empty set forever.
    const covered = Object.keys(SOURCES).filter(
      (path) => SOURCES[path].includes("useQuery(") && GATED_CALLS.some((c) => SOURCES[path].includes(c)),
    );
    expect(covered.length).toBeGreaterThanOrEqual(3);
    expect(covered.some((p) => p.endsWith("VisualizePage.tsx"))).toBe(true);
  });
});
