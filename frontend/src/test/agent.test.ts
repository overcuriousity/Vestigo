/**
 * specToEventFilters — the contract that makes agent findings applyable.
 * A backend FilterSpec (snake_case) must map losslessly onto the Explorer's
 * EventFilters shape (camelCase) so "Apply to Explorer" reproduces exactly
 * the filter set the agent ran.
 */
import { describe, expect, it } from "vitest";
import {
  specToEventFilters,
  formatTokenCount,
  type AgentFilterSpec,
  type AgentProposal,
} from "@/api/agent";
import { filtersToParams, paramsToFilters } from "@/lib/queryParams";
import { computeEffectiveFilters, overlaysFromApplied } from "@/lib/effectiveFilters";
import type { EventFilters } from "@/api/types";

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

/**
 * End-to-end apply seam: FindingCard → onApply(specToEventFilters(spec)) →
 * ExplorerPage.handleApplyAgentFilters, which splits the applied filters into
 * the URL layer (setFilters → filtersToParams → paramsToFilters) and the
 * session overlays (overlaysFromApplied), then re-merges via
 * computeEffectiveFilters into the filter set actually queried. The whole
 * point of the fix: `anomalyRunId`, `ids`, `collapseRoutine` survive even
 * though they are never URL-serialized.
 */
describe("agent finding apply → effective filters", () => {
  /** Reproduce exactly what ExplorerPage does on "Apply to Explorer". */
  function applyToEffective(applied: EventFilters): EventFilters {
    const urlLayer = paramsToFilters(filtersToParams(applied)); // setFilters round-trip
    const overlays = overlaysFromApplied(applied);
    return computeEffectiveFilters(urlLayer, {
      anomalyRunId: overlays.anomalyRunId,
      appliedIds: overlays.ids,
      semanticSearchIds: null,
      collapseRoutine: overlays.collapseRoutine,
    });
  }

  it("carries all five new FilterSpec fields (plus base fields) into the applied view", () => {
    const spec: AgentFilterSpec = {
      q: "ssh",
      q_regex: true,
      artifacts: ["syslog"],
      source_id: "s1",
      start: "2026-01-01T00:00:00Z",
      end: "2026-01-02T00:00:00Z",
      filters: { username: ["root"] },
      exclusions: { status: ["200"] },
      filter_modes: { username: "wildcard" },
      exclusion_modes: { status: "regex" },
      tags_include: ["suspicious"],
      tags_exclude: ["benign"],
      // The five fields the apply path previously dropped or ignored:
      annotated: ["tag", "anomaly"],
      annotation_tag_value: "bad",
      run_id: "run-1",
      event_ids: ["e1", "e2"],
      collapse_routine: true,
    };
    const applied = specToEventFilters(spec);
    const effective = applyToEffective(applied);
    // Nothing is lost: the applied view equals the agent's own filter set.
    expect(effective).toEqual(applied);
    // Explicit checks on the three previously-dropped overlay fields.
    expect(effective.anomalyRunId).toBe("run-1");
    expect(effective.ids).toEqual(["e1", "e2"]);
    expect(effective.collapseRoutine).toBe(true);
  });

  it("documents the regression: the URL layer alone drops the three overlay fields", () => {
    const applied = specToEventFilters({
      run_id: "run-1",
      event_ids: ["e1", "e2"],
      collapse_routine: true,
      annotated: ["anomaly"],
    });
    // The old apply path (setFilters only, no overlays) silently loses them.
    const urlOnly = paramsToFilters(filtersToParams(applied));
    expect(urlOnly.anomalyRunId).toBeUndefined();
    expect(urlOnly.ids).toBeUndefined();
    expect(urlOnly.collapseRoutine).toBeUndefined();
    // The fixed path restores them.
    const effective = applyToEffective(applied);
    expect(effective.anomalyRunId).toBe("run-1");
    expect(effective.ids).toEqual(["e1", "e2"]);
    expect(effective.collapseRoutine).toBe(true);
  });

  it("an agent event_id allowlist wins over an active semantic search", () => {
    const applied = specToEventFilters({ event_ids: ["a", "b"] });
    const overlays = overlaysFromApplied(applied);
    const effective = computeEffectiveFilters(paramsToFilters(filtersToParams(applied)), {
      anomalyRunId: undefined,
      appliedIds: overlays.ids,
      semanticSearchIds: ["x", "y", "z"],
      collapseRoutine: false,
    });
    expect(effective.ids).toEqual(["a", "b"]);
  });
});

/**
 * ProposalCard's "Open in Explorer" reuses FindingCard's apply path: a
 * proposal's events map onto EventFilters.ids via the same
 * specToEventFilters({ event_ids }) seam, so it inherits the overlay-loss
 * fix covered above rather than re-deriving the mapping.
 */
describe("proposal events → Explorer filter mapping", () => {
  it("round-trips a proposal's event ids into EventFilters.ids", () => {
    const proposal: AgentProposal = {
      id: "prop-1",
      conversation_id: "conv-1",
      case_id: "case-1",
      timeline_id: "tl-1",
      status: "confirmed",
      tag: "suspicious",
      comment: null,
      rationale: "clustered auth failures",
      events: [
        { source_id: "s1", event_id: "e1" },
        { source_id: "s2", event_id: "e2" },
      ],
      created_at: null,
      decided_by: "alice",
      decided_at: null,
    };
    const f = specToEventFilters({ event_ids: proposal.events.map((e) => e.event_id) });
    expect(f.ids).toEqual(["e1", "e2"]);
  });
});

describe("formatTokenCount", () => {
  it("formats plain, k and M", () => {
    expect(formatTokenCount(890)).toBe("890");
    expect(formatTokenCount(12400)).toBe("12.4k");
    expect(formatTokenCount(1200000)).toBe("1.2M");
  });
});
