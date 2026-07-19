/**
 * specToEventFilters — the contract that makes agent findings applyable.
 * A backend FilterSpec (snake_case) must map losslessly onto the Explorer's
 * EventFilters shape (camelCase) so "Apply to Explorer" reproduces exactly
 * the filter set the agent ran.
 */
import { describe, expect, it } from "vitest";
import { specToEventFilters } from "@/api/agent";

describe("specToEventFilters", () => {
  it("maps every FilterSpec field onto EventFilters", () => {
    const filters = specToEventFilters({
      q: "ssh",
      q_regex: true,
      artifacts: ["syslog", "auth"],
      source_id: "s1",
      start: "2026-01-01T00:00:00Z",
      end: "2026-01-02T00:00:00Z",
      filters: { username: ["root", "admin"] },
      exclusions: { status: ["200"] },
      filter_modes: { username: "wildcard" },
      exclusion_modes: { status: "regex" },
      tags_include: ["suspicious"],
      tags_exclude: ["benign"],
    });
    expect(filters).toEqual({
      q: "ssh",
      qRegex: true,
      artifacts: ["syslog", "auth"],
      sourceId: "s1",
      start: "2026-01-01T00:00:00Z",
      end: "2026-01-02T00:00:00Z",
      filters: { username: ["root", "admin"] },
      exclusions: { status: ["200"] },
      filterModes: { username: "wildcard" },
      exclusionModes: { status: "regex" },
      tagsInclude: ["suspicious"],
      tagsExclude: ["benign"],
    });
  });

  it("drops empty and null fields instead of serializing them", () => {
    expect(specToEventFilters({})).toEqual({});
    expect(
      specToEventFilters({
        q: null,
        artifacts: [],
        filters: {},
        exclusions: {},
        filter_modes: {},
        tags_include: null,
      }),
    ).toEqual({});
  });

  it('ignores unknown match modes (only "wildcard"/"regex" survive)', () => {
    const filters = specToEventFilters({
      filters: { f: ["v"] },
      filter_modes: { f: "exact" },
    });
    expect(filters.filterModes).toBeUndefined();
  });
});
