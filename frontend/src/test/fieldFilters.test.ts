import { describe, expect, it } from "vitest";
import {
  applyFieldEntries,
  applyFieldFilter,
  dropMode,
  mapFieldTokenToFilterKey,
} from "@/lib/fieldFilters";
import type { EventFilters } from "@/api/types";

describe("dropMode", () => {
  it("removes the key's mode and collapses an emptied map to undefined", () => {
    expect(dropMode({ ip: "wildcard" }, "ip")).toBeUndefined();
    expect(dropMode({ ip: "wildcard", user: "regex" }, "ip")).toEqual({ user: "regex" });
  });
  it("returns the input untouched when the key has no mode", () => {
    const modes = { user: "regex" as const };
    expect(dropMode(modes, "ip")).toBe(modes);
    expect(dropMode(undefined, "ip")).toBeUndefined();
  });
});

describe("applyFieldFilter", () => {
  it("adds an include filter without mutating the input", () => {
    const f: EventFilters = { q: "dos" };
    const next = applyFieldFilter(f, "status", "500", true);
    expect(next.filters).toEqual({ status: ["500"] });
    expect(f.filters).toBeUndefined();
  });

  it("appends to an existing key and dedupes", () => {
    const base: EventFilters = { filters: { status: ["500"] } };
    expect(applyFieldFilter(base, "status", "404", true).filters).toEqual({
      status: ["500", "404"],
    });
    expect(applyFieldFilter(base, "status", "500", true).filters).toEqual({ status: ["500"] });
  });

  it("resets a pattern match mode on the key — clicked values are literal", () => {
    const base: EventFilters = {
      filters: { ip: ["10.*"] },
      filterModes: { ip: "wildcard" },
    };
    const next = applyFieldFilter(base, "ip", "10.0.0.7", true);
    expect(next.filterModes).toBeUndefined();
  });

  it("routes exclusions into exclusions{} and resets exclusion modes", () => {
    const base: EventFilters = { exclusionModes: { ip: "regex" } };
    const next = applyFieldFilter(base, "ip", "10.0.0.7", false);
    expect(next.exclusions).toEqual({ ip: ["10.0.0.7"] });
    expect(next.exclusionModes).toBeUndefined();
  });

  it("special-cases q (include-only free text)", () => {
    expect(applyFieldFilter({}, "q", "ssh", true).q).toBe("ssh");
  });

  it("special-cases artifact: dedicated param on include, exclusions on exclude", () => {
    expect(applyFieldFilter({}, "artifact", "auth", true).artifact).toBe("auth");
    expect(applyFieldFilter({}, "artifact", "auth", false).exclusions).toEqual({
      artifact: ["auth"],
    });
  });

  it("special-cases tag: dedicated include/exclude params", () => {
    expect(applyFieldFilter({}, "tag", "suspicious", true).tag).toBe("suspicious");
    expect(applyFieldFilter({}, "tag", "noise", false).excludeTag).toBe("noise");
  });
});

describe("mapFieldTokenToFilterKey", () => {
  it("strips attr: prefixes, maps tags → tag, passes columns through", () => {
    expect(mapFieldTokenToFilterKey("attr:status_code")).toBe("status_code");
    expect(mapFieldTokenToFilterKey("tags")).toBe("tag");
    expect(mapFieldTokenToFilterKey("artifact")).toBe("artifact");
  });
});

describe("applyFieldEntries", () => {
  it("applies a two-field conjunction in one pass (no clobbering)", () => {
    const next = applyFieldEntries(
      {},
      [
        ["attr:username", "alice"],
        ["attr:workstation", "WS01"],
      ],
      true,
    );
    expect(next.filters).toEqual({ username: ["alice"], workstation: ["WS01"] });
  });
});
