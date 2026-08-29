/**
 * specToEventFilters — the contract that makes agent findings applyable.
 * A backend FilterSpec (snake_case) must map losslessly onto the Explorer's
 * EventFilters shape (camelCase) so "Apply to Explorer" reproduces exactly
 * the filter set the agent ran.
 */
import { describe, expect, it } from "vitest";
import {
  specToEventFilters,
  specToChartConfig,
  formatTokenCount,
  parseToolArgObject,
  type AgentChartSpec,
  type AgentChartSpecLegacy,
  type AgentFilterSpec,
  type AgentProposal,
} from "@/api/agent";
import { chartConfigToParams, paramsToChartConfig } from "@/components/viz/lib/chartConfig";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import type { ChartType } from "@/components/viz/lib/chartConfig";

const CHART_TYPES = Object.keys(CHART_META) as ChartType[];
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

  it('ignores unknown match modes (only "wildcard"/"regex"/"empty" survive)', () => {
    const filters = specToEventFilters({
      filters: { f: ["v"] },
      filter_modes: { f: "exact" },
    });
    expect(filters.filterModes).toBeUndefined();
  });

  it("keeps the empty presence mode alongside its placeholder value", () => {
    // Dropping the mode but keeping FilterSpec's `[""]` placeholder would run
    // an *exact* match on the literal empty string, which skips `ifNull(...)`
    // and so shows a different row set than the agent's own answer.
    const filters = specToEventFilters({
      filters: { user_agent: [""] },
      filter_modes: { user_agent: "empty" },
    });
    expect(filters.filterModes).toEqual({ user_agent: "empty" });
    expect(filters.filters).toEqual({ user_agent: [""] });
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
    kind: "annotation" as const,
    payload: null,
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

describe("isAnalystAnnotation", () => {
  it("treats user and agentic-analysis as analyst-visible, system not", async () => {
    const { isAnalystAnnotation } = await import("../api/types");
    const base = { annotation_type: "tag" };
    expect(isAnalystAnnotation({ ...base, origin: "user" } as never)).toBe(true);
    expect(isAnalystAnnotation({ ...base, origin: "agentic-analysis" } as never)).toBe(true);
    expect(isAnalystAnnotation({ ...base, origin: "system" } as never)).toBe(false);
  });
});

describe("specToChartConfig (legacy `kind` shape)", () => {
  // Frozen: these are what historical chart cards rendered as. Persisted
  // `tool_args` still flow through this path, so a change here rewrites the past.
  it("maps each kind to its chart type, round-tripping through URL params", () => {
    const cases: [AgentChartSpecLegacy["kind"], string][] = [
      ["terms", "bar"],
      ["numeric", "histogram"],
      ["timeseries", "line"],
      ["punchcard", "punchcard"],
      ["pivot", "pivot"],
      ["scatter", "scatter"],
      ["compare_time", "time"],
      ["compare_terms", "bar"],
      ["compare_numeric", "histogram"],
    ];
    for (const [kind, chartType] of cases) {
      const config = specToChartConfig({ kind, field: "artifact" });
      expect(config.chartType).toBe(chartType);
      // Round-trip through the same URL-param path "Open in Visualize" uses.
      const params = chartConfigToParams(config);
      expect(paramsToChartConfig(params).chartType).toBe(chartType);
    }
  });

  it("carries field/fieldY through", () => {
    const config = specToChartConfig({ kind: "pivot", field: "attr:user", field_y: "attr:host" });
    expect(config.field).toBe("attr:user");
    expect(config.fieldY).toBe("attr:host");
  });

  it("maps buckets/series_limit into options for timeseries", () => {
    const config = specToChartConfig({
      kind: "timeseries",
      field: "attr:status",
      buckets: 40,
      series_limit: 5,
    });
    expect(config.options.buckets).toBe(40);
    expect(config.options.topN).toBe(5);
  });

  it("maps pivot limit/limit_y into limitX/limitY, not topN", () => {
    const config = specToChartConfig({
      kind: "pivot",
      field: "attr:user",
      field_y: "attr:host",
      limit: 6,
      limit_y: 9,
    });
    expect(config.options.limitX).toBe(6);
    expect(config.options.limitY).toBe(9);
    expect(config.options.topN).toBeUndefined();
  });

  it("maps numeric limit into bins, not topN", () => {
    // `limit` means bin count for the numeric kinds — the histogram data path
    // reads options.bins, so routing it to topN would drop it silently.
    for (const kind of ["numeric", "compare_numeric"] as const) {
      const config = specToChartConfig({ kind, field: "attr:bytes", limit: 20 });
      expect(config.options.bins).toBe(20);
      expect(config.options.topN).toBeUndefined();
    }
  });

  it("maps scatter limit into sampleLimit", () => {
    const config = specToChartConfig({
      kind: "scatter",
      field: "attr:bytes",
      field_y: "attr:latency",
      limit: 500,
    });
    expect(config.options.sampleLimit).toBe(500);
  });

  it("maps terms limit into topN", () => {
    const config = specToChartConfig({ kind: "terms", field: "artifact", limit: 25 });
    expect(config.options.topN).toBe(25);
  });

  it("non-compare kinds ignore comparison_filters", () => {
    const config = specToChartConfig({
      kind: "terms",
      field: "artifact",
      comparison_filters: { q: "should be ignored" },
    });
    expect(config.compare.mode).toBe("off");
  });

  it("compare_* kinds map comparison_filters to a custom compare layer", () => {
    const config = specToChartConfig({
      kind: "compare_terms",
      field: "artifact",
      comparison_filters: { source_id: "s2" },
    });
    expect(config.compare.mode).toBe("custom");
    if (config.compare.mode === "custom") {
      expect(config.compare.filters.sourceId).toBe("s2");
    }
  });

  it("compare_* kind without comparison_filters falls back to off", () => {
    const config = specToChartConfig({ kind: "compare_time" });
    expect(config.compare.mode).toBe("off");
  });

  it("scale matches the chart type's valid scales", () => {
    // numeric/scatter/compare_numeric need interval|ratio scales in CHART_META.
    expect(specToChartConfig({ kind: "numeric", field: "attr:bytes" }).scale).toBe("ratio");
    expect(
      specToChartConfig({ kind: "scatter", field: "a", field_y: "b" }).scale,
    ).toBe("ratio");
  });
});

describe("buildUserNameMap", () => {
  it("prefers display_name, falls back to username, resolver falls back to raw id", async () => {
    const { buildUserNameMap, resolveUserName } = await import("../lib/userNames");
    const map = buildUserNameMap([
      { id: "user_1", username: "mmustermann", display_name: "Max Mustermann" },
      { id: "user_2", username: "jdoe", display_name: null },
    ]);
    expect(resolveUserName(map, "user_1")).toBe("Max Mustermann");
    expect(resolveUserName(map, "user_2")).toBe("jdoe");
    expect(resolveUserName(map, "mmustermann")).toBe("Max Mustermann");
    expect(resolveUserName(map, "legacy-username")).toBe("legacy-username");
    expect(resolveUserName(map, null)).toBe("anonymous");
  });
});

describe("parseToolArgObject", () => {
  it("parses a provider-stringified object argument", () => {
    const raw = '{"chart_type": "bar", "field": "src_ip:geo_country"}';
    const spec = parseToolArgObject<AgentChartSpec>(raw);
    expect(spec).toEqual({ chart_type: "bar", field: "src_ip:geo_country" });
    // The whole point: the parsed spec survives the shape check that used to
    // throw "Cannot use 'in' operator" on the raw string.
    expect(specToChartConfig(spec!).chartType).toBe("bar");
  });

  it("passes an object through and rejects everything unusable", () => {
    const obj = { chart_type: "pie" as const, field: "a" };
    expect(parseToolArgObject(obj)).toBe(obj);
    expect(parseToolArgObject("not json")).toBeNull();
    expect(parseToolArgObject("[1,2]")).toBeNull();
    expect(parseToolArgObject(null)).toBeNull();
    expect(parseToolArgObject(undefined)).toBeNull();
  });
});

describe("stringified values nested inside a spec", () => {
  it("specToEventFilters takes a stringified spec and stringified maps inside it", () => {
    // `Object.keys` on a string yields character indices, so an unparsed map
    // built a filter set that was *wrong* rather than absent — the failure
    // mode this whole path exists to prevent.
    const f = specToEventFilters(
      '{"q": "boom", "filters": "{\\"attr:status\\": [\\"500\\"]}", "filter_modes": "{\\"attr:status\\": \\"wildcard\\"}"}',
    );
    expect(f.q).toBe("boom");
    expect(f.filters).toEqual({ "attr:status": ["500"] });
    expect(f.filterModes).toEqual({ "attr:status": "wildcard" });
  });

  it("specToChartConfig keeps the comparison layer when `compare` arrives stringified", () => {
    // Silently the worst of the family: an unparsed `compare` made
    // `compare?.mode` undefined, so the chart drew one layer where the model
    // proposed two, with no error anywhere.
    const config = specToChartConfig({
      chart_type: "bar",
      field: "attr:status",
      compare: '{"mode": "custom", "filters": {"q": "baseline"}}',
      options: '{"top_n": 7}',
    } as unknown as AgentChartSpec);
    expect(config.compare).toEqual({ mode: "custom", filters: { q: "baseline" } });
    expect(config.options.topN).toBe(7);
  });

  it("specToChartConfig throws on a spec that is not an object", () => {
    // AgentPanel filters these out before an item exists; the throw is the
    // far side of that guard, contained by the card's ErrorBoundary.
    expect(() => specToChartConfig("not json" as unknown as AgentChartSpec)).toThrow(
      /not a JSON object/,
    );
  });
});

describe("specToChartConfig (current shape)", () => {
  it("reaches every chart type, including the six the old `kind` enum could not", () => {
    for (const chartType of CHART_TYPES) {
      const config = specToChartConfig({ chart_type: chartType, field: "artifact" });
      expect(config.chartType).toBe(chartType);
      // Round-trip through the same URL-param path "Open in Visualize" uses.
      expect(paramsToChartConfig(chartConfigToParams(config)).chartType).toBe(chartType);
    }
    for (const previouslyUnreachable of ["pie", "heatmap", "box", "violin", "ecdf", "sankey"]) {
      expect(CHART_TYPES).toContain(previouslyUnreachable);
    }
  });

  it("defaults an omitted scale to the chart type's default", () => {
    expect(specToChartConfig({ chart_type: "pie", field: "a" }).scale).toBe("nominal");
    expect(specToChartConfig({ chart_type: "histogram", field: "a" }).scale).toBe("ratio");
  });

  it("honours an explicit scale", () => {
    expect(specToChartConfig({ chart_type: "bar", field: "a", scale: "ordinal" }).scale).toBe(
      "ordinal",
    );
  });

  it("carries metric through instead of hardcoding count", () => {
    expect(specToChartConfig({ chart_type: "time", metric: "rate" }).metric).toBe("rate");
    expect(specToChartConfig({ chart_type: "bar", field: "a" }).metric).toBe("count");
  });

  it("maps baseline compare, which the old shape could not express", () => {
    expect(specToChartConfig({ chart_type: "time", compare: { mode: "baseline" } }).compare).toEqual(
      { mode: "baseline" },
    );
  });

  it("maps a custom compare layer's filters", () => {
    const config = specToChartConfig({
      chart_type: "bar",
      field: "artifact",
      compare: { mode: "custom", filters: { source_id: "s2" } },
    });
    expect(config.compare.mode).toBe("custom");
    if (config.compare.mode === "custom") expect(config.compare.filters.sourceId).toBe("s2");
  });

  it("maps every option to its camelCase ChartConfig key", () => {
    const config = specToChartConfig({
      chart_type: "bar",
      field: "a",
      options: {
        top_n: 7,
        bins: 12,
        buckets: 20,
        limit_x: 3,
        limit_y: 4,
        sample_limit: 900,
        orientation: "vertical",
        sort: "value",
        log_scale: true,
        series_mode: "stacked",
        legend: false,
      },
    });
    expect(config.options).toEqual({
      topN: 7,
      bins: 12,
      buckets: 20,
      limitX: 3,
      limitY: 4,
      sampleLimit: 900,
      orientation: "vertical",
      sort: "value",
      logScale: true,
      seriesMode: "stacked",
      legend: false,
    });
  });

  it("keeps an explicit zero, which the old falsy guards dropped", () => {
    const config = specToChartConfig({ chart_type: "bar", field: "a", options: { top_n: 0 } });
    expect(config.options.topN).toBe(0);
  });

  it("does not collide top_n and buckets on a timeseries chart", () => {
    const config = specToChartConfig({
      chart_type: "line",
      field: "a",
      options: { top_n: 5, buckets: 20 },
    });
    expect(config.options.topN).toBe(5);
    expect(config.options.buckets).toBe(20);
  });
});

describe("specToChartConfig — derive", () => {
  it("carries the derivation across, in the frontend's casing", () => {
    expect(
      specToChartConfig({ chart_type: "bar", field: "a", derive: { kind: "time_part", part: "weekday" } }).derive,
    ).toEqual({ kind: "timePart", part: "weekday" });
    expect(
      specToChartConfig({ chart_type: "bar", field: "a", derive: { kind: "bins", mode: "log", count: 8 } }).derive,
    ).toEqual({ kind: "bins", mode: "log", count: 8 });
    expect(specToChartConfig({ chart_type: "bar", field: "a", derive: null }).derive).toBeNull();
  });
});

describe("specToChartConfig — inputs", () => {
  it("carries the table's columns across, dropping unknown ones", () => {
    expect(
      specToChartConfig({
        chart_type: "table",
        field: "a",
        inputs: { columns: ["count", "share", "bogus" as never] },
      }).inputs,
    ).toEqual({ columns: ["count", "share"] });
    expect(specToChartConfig({ chart_type: "bar", field: "a" }).inputs).toEqual({});
  });
});
