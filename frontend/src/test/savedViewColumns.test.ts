/**
 * A saved view carries the column layout it was saved with. Views written
 * before this feature have no `columns` key at all, and applying one must
 * leave the analyst's current layout alone rather than blanking it.
 *
 * Also pins the round-trip of the `empty` match mode: modes pass through a
 * whitelist on the way back in, and a mode missing from it is dropped
 * silently — turning "user_agent is empty" into "user_agent = ''" as an exact
 * match, which is a different question.
 */
import { describe, it, expect } from "vitest";
import {
  filtersToParams,
  paramsToFilters,
  viewPayloadColumns,
  viewPayloadToFilters,
} from "@/lib/queryParams";

describe("viewPayloadColumns", () => {
  it("returns the sanitized column list", () => {
    expect(viewPayloadColumns({ columns: ["timestamp", "message"] })).toEqual([
      "timestamp",
      "message",
    ]);
  });

  it("remaps retired column ids", () => {
    expect(viewPayloadColumns({ columns: ["source", "message"] })).toEqual([
      "artifact",
      "message",
    ]);
  });

  it("dedupes repeated ids", () => {
    expect(viewPayloadColumns({ columns: ["message", "message"] })).toEqual(["message"]);
  });

  it("returns undefined for a legacy payload with no columns key", () => {
    expect(viewPayloadColumns({ filters: { a: ["b"] } })).toBeUndefined();
  });

  it("returns undefined when the key is not an array of strings", () => {
    expect(viewPayloadColumns({ columns: "message" })).toBeUndefined();
    expect(viewPayloadColumns({ columns: [1, 2] })).toBeUndefined();
  });

  it("treats a list that sanitizes to nothing as absent, not as 'no columns'", () => {
    expect(viewPayloadColumns({ columns: ["_select", "_expand"] })).toBeUndefined();
  });
});

describe("empty match mode round trip", () => {
  it("survives a saved-view payload", () => {
    const f = viewPayloadToFilters({
      filters: { user_agent: [""] },
      filterModes: { user_agent: "empty" },
    });
    expect(f.filterModes).toEqual({ user_agent: "empty" });
  });

  it("survives a saved-view payload on the exclusion side", () => {
    const f = viewPayloadToFilters({
      exclusions: { user_agent: [""] },
      exclusionModes: { user_agent: "empty" },
    });
    expect(f.exclusionModes).toEqual({ user_agent: "empty" });
  });

  it("survives a URL round trip", () => {
    const params = filtersToParams({
      filters: { user_agent: [""] },
      filterModes: { user_agent: "empty" },
    });
    const back = paramsToFilters(new URLSearchParams(params.toString()));
    expect(back.filterModes).toEqual({ user_agent: "empty" });
  });

  it("still drops an unknown mode from a hand-edited payload", () => {
    const f = viewPayloadToFilters({
      filters: { user_agent: ["x"] },
      filterModes: { user_agent: "blank" },
    });
    expect(f.filterModes).toBeUndefined();
  });
});
