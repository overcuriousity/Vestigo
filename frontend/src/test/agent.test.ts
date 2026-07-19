/**
 * specToEventFilters — the contract that makes agent findings applyable.
 * A backend FilterSpec (snake_case) must map losslessly onto the Explorer's
 * EventFilters shape (camelCase) so "Apply to Explorer" reproduces exactly
 * the filter set the agent ran.
 */
import { describe, expect, it } from "vitest";
import { specToEventFilters, type AgentFilterSpec } from "@/api/agent";

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

  it("maps annotation-state, run, ids and routine-collapse fields", () => {
    const spec: AgentFilterSpec = {
      annotated: ["tag", "anomaly"],
      annotation_tag_value: "bad",
      run_id: "run-1",
      event_ids: ["e1", "e2"],
      collapse_routine: true,
    };
    const f = specToEventFilters(spec);
    expect(f.annotated).toEqual(["tag", "anomaly"]);
    expect(f.annotationTagValue).toBe("bad");
    expect(f.anomalyRunId).toBe("run-1");
    expect(f.ids).toEqual(["e1", "e2"]);
    expect(f.collapseRoutine).toBe(true);
  });

  it("omits the new fields when absent", () => {
    const f = specToEventFilters({});
    expect(f.annotated).toBeUndefined();
    expect(f.annotationTagValue).toBeUndefined();
    expect(f.anomalyRunId).toBeUndefined();
    expect(f.ids).toBeUndefined();
    expect(f.collapseRoutine).toBeUndefined();
  });
});
