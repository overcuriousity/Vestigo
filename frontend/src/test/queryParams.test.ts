import { describe, it, expect } from "vitest";
import {
  filtersToParams,
  paramsToFilters,
  filtersToViewPayload,
  viewPayloadToFilters,
  serializeEventFilterFields,
  serializeEventFilterParams,
} from "@/lib/queryParams";
import type { EventFilters } from "@/api/types";

describe("filtersToParams / paramsToFilters round-trip", () => {
  it("round-trips an empty filter", () => {
    const f: EventFilters = {};
    const p = filtersToParams(f);
    expect(paramsToFilters(p)).toEqual({});
  });

  it("round-trips simple scalar filters", () => {
    const f: EventFilters = {
      q: "powershell",
      artifact: "WinEvtx",
      tag: "suspicious",
      start: "2024-01-01T00:00:00.000Z",
      end: "2024-01-31T23:59:59.000Z",
    };
    const p = filtersToParams(f);
    const out = paramsToFilters(p);
    expect(out.q).toBe("powershell");
    expect(out.artifact).toBe("WinEvtx");
    expect(out.tag).toBe("suspicious");
    expect(out.start).toBe("2024-01-01T00:00:00.000Z");
    expect(out.end).toBe("2024-01-31T23:59:59.000Z");
  });

  it("round-trips field include/exclude filters", () => {
    const f: EventFilters = {
      filters: { ip_address_city: "Falkenstein", status_code: "200" },
      exclusions: { user_agent: ["bot"] },
    };
    const p = filtersToParams(f);
    const out = paramsToFilters(p);
    expect(out.filters).toEqual({ ip_address_city: "Falkenstein", status_code: "200" });
    expect(out.exclusions).toEqual({ user_agent: ["bot"] });
  });

  it("round-trips field match modes", () => {
    const f: EventFilters = {
      filters: { src_ip: "10.0.*" },
      filterModes: { src_ip: "wildcard" },
      exclusions: { msg: ["^error"] },
      exclusionModes: { msg: "regex" },
    };
    const out = paramsToFilters(filtersToParams(f));
    expect(out.filterModes).toEqual({ src_ip: "wildcard" });
    expect(out.exclusionModes).toEqual({ msg: "regex" });
  });

  it("legacy URL without mode params yields no mode maps", () => {
    const p = filtersToParams({ filters: { a: "b" }, exclusions: { c: ["d"] } });
    const out = paramsToFilters(p);
    expect(out.filterModes).toBeUndefined();
    expect(out.exclusionModes).toBeUndefined();
  });

  it("drops invalid and explicit-exact mode values from the URL", () => {
    const p = new URLSearchParams();
    p.set("filters", JSON.stringify({ a: "x", b: "y", c: "z" }));
    p.set("filterModes", JSON.stringify({ a: "glob", b: "exact", c: "regex" }));
    const out = paramsToFilters(p);
    expect(out.filterModes).toEqual({ c: "regex" });
  });

  it("round-trips the merged tag filter and multi-select artifacts", () => {
    const f: EventFilters = {
      tagsInclude: ["exfil", "malware"],
      tagsExclude: ["benign"],
      artifacts: ["WinEvtx", "Prefetch"],
    };
    const p = filtersToParams(f);
    const out = paramsToFilters(p);
    expect(out.tagsInclude).toEqual(["exfil", "malware"]);
    expect(out.tagsExclude).toEqual(["benign"]);
    expect(out.artifacts).toEqual(["WinEvtx", "Prefetch"]);
  });

  it("omits undefined fields from params", () => {
    const f: EventFilters = { q: "test" };
    const p = filtersToParams(f);
    expect(p.has("artifact")).toBe(false);
    expect(p.has("tag")).toBe(false);
    expect(p.has("filters")).toBe(false);
    expect(p.has("qMode")).toBe(false);
    expect(p.has("qRegex")).toBe(false);
  });

  it("round-trips search mode and regex flag", () => {
    const f: EventFilters = { q: "^Login fail", qRegex: true };
    const out = paramsToFilters(filtersToParams(f));
    expect(out.qMode).toBeUndefined();
    expect(out.qRegex).toBe(true);
  });

  it("drops regex flag when search mode is semantic", () => {
    const f: EventFilters = { q: "Login fail", qMode: "semantic", qRegex: true };
    const out = paramsToFilters(filtersToParams(f));
    expect(out.qMode).toBe("semantic");
    expect(out.qRegex).toBeUndefined();
  });

  it("ignores unknown qMode values from the URL", () => {
    const p = new URLSearchParams({ q: "x", qMode: "bogus" });
    expect(paramsToFilters(p).qMode).toBeUndefined();
  });
});

describe("filtersToViewPayload / viewPayloadToFilters round-trip", () => {
  it("round-trips correctly", () => {
    const f: EventFilters = {
      q: "mimikatz",
      filters: { event_id: "4624" },
      exclusions: { status: ["ok"] },
    };
    const payload = filtersToViewPayload(f);
    const out = viewPayloadToFilters(payload);
    expect(out.q).toBe("mimikatz");
    expect(out.filters).toEqual({ event_id: "4624" });
    expect(out.exclusions).toEqual({ status: ["ok"] });
  });

  it("handles empty filters gracefully", () => {
    const out = viewPayloadToFilters({});
    expect(out).toEqual({});
  });

  it("round-trips qMode/qRegex so a saved view reproduces search semantics", () => {
    const f: EventFilters = { q: "lateral movement", qMode: "semantic" };
    expect(viewPayloadToFilters(filtersToViewPayload(f)).qMode).toBe("semantic");
    const g: EventFilters = { q: "^4624$", qRegex: true };
    const out = viewPayloadToFilters(filtersToViewPayload(g));
    expect(out.qRegex).toBe(true);
    expect(out.qMode).toBeUndefined();
  });

  it("treats legacy payloads without qMode/qRegex as keyword, non-regex", () => {
    const out = viewPayloadToFilters({ q: "old view" });
    expect(out.qMode).toBeUndefined();
    expect(out.qRegex).toBeUndefined();
  });

  it("round-trips match modes so a saved view reproduces match semantics", () => {
    const f: EventFilters = {
      filters: { src_ip: "10.0.*" },
      filterModes: { src_ip: "wildcard" },
      exclusions: { msg: ["^err"] },
      exclusionModes: { msg: "regex" },
    };
    const out = viewPayloadToFilters(filtersToViewPayload(f));
    expect(out.filterModes).toEqual({ src_ip: "wildcard" });
    expect(out.exclusionModes).toEqual({ msg: "regex" });
  });

  it("treats legacy payloads without mode keys as exact and drops invalid modes", () => {
    const out = viewPayloadToFilters({ filters: { a: "b" } });
    expect(out.filterModes).toBeUndefined();
    const bad = viewPayloadToFilters({
      filters: { a: "b" },
      filterModes: { a: "glob", b: "exact" },
    });
    expect(bad.filterModes).toBeUndefined();
  });
});

describe("serializeEventFilterParams match modes", () => {
  it("emits filter_modes/exclusion_modes JSON only when non-empty", () => {
    expect(serializeEventFilterParams({ filters: { a: "b" } }).filter_modes).toBeUndefined();
    const out = serializeEventFilterParams({
      filters: { src_ip: "10.0.*" },
      filterModes: { src_ip: "wildcard" },
      exclusions: { msg: ["x"] },
      exclusionModes: { msg: "regex" },
    });
    expect(out.filter_modes).toBe(JSON.stringify({ src_ip: "wildcard" }));
    expect(out.exclusion_modes).toBe(JSON.stringify({ msg: "regex" }));
  });
});

describe("serializeEventFilterFields (C17 — shared by list/histogram/bulk-annotate/export)", () => {
  it("returns an empty object for no filters", () => {
    expect(serializeEventFilterFields({})).toEqual({});
  });

  it("emits q_regex only for a regex keyword search", () => {
    expect(serializeEventFilterFields({ q: "^x", qRegex: true }).q_regex).toBe(true);
    // No query text — nothing for the flag to apply to.
    expect(serializeEventFilterFields({ qRegex: true }).q_regex).toBeUndefined();
    // Semantic mode replaces q with ids client-side; regex is meaningless.
    expect(
      serializeEventFilterFields({ q: "^x", qRegex: true, qMode: "semantic" }).q_regex,
    ).toBeUndefined();
    expect(serializeEventFilterFields({ q: "plain" }).q_regex).toBeUndefined();
  });

  it("joins array fields with commas and maps to snake_case API names", () => {
    const f: EventFilters = {
      artifacts: ["WinEvtx", "Prefetch"],
      tagsInclude: ["exfil", "malware"],
      tagsExclude: ["benign"],
      ids: ["evt-1", "evt-2"],
      annotated: ["tag", "anomaly"],
      anomalyRunId: "run-1",
      sourceId: "src-1",
      excludeTag: "noisy",
      annotationTagValue: "reviewed",
    };
    expect(serializeEventFilterFields(f)).toEqual({
      artifacts: "WinEvtx,Prefetch",
      tags_include: "exfil,malware",
      tags_exclude: "benign",
      ids: "evt-1,evt-2",
      annotated: "tag,anomaly",
      run_id: "run-1",
      source_id: "src-1",
      exclude_tag: "noisy",
      annotation_tag_value: "reviewed",
    });
  });

  it("omits empty arrays and falsy scalars rather than sending empty strings", () => {
    const f: EventFilters = { artifacts: [], tagsInclude: [], q: "" };
    const out = serializeEventFilterFields(f);
    expect(out.artifacts).toBeUndefined();
    expect(out.tags_include).toBeUndefined();
    expect(out.q).toBeUndefined();
  });

  it("does not touch the object-shaped filters/exclusions fields", () => {
    const f: EventFilters = { filters: { a: "b" }, exclusions: { c: ["d"] } };
    const out = serializeEventFilterFields(f) as Record<string, unknown>;
    expect(out.filters).toBeUndefined();
    expect(out.exclusions).toBeUndefined();
  });
});
