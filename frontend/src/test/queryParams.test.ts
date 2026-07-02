import { describe, it, expect } from "vitest";
import {
  filtersToParams,
  paramsToFilters,
  filtersToViewPayload,
  viewPayloadToFilters,
  serializeEventFilterFields,
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
});

describe("serializeEventFilterFields (C17 — shared by list/histogram/bulk-annotate/export)", () => {
  it("returns an empty object for no filters", () => {
    expect(serializeEventFilterFields({})).toEqual({});
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
