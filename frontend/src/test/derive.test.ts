import { describe, expect, it } from "vitest";
import {
  defaultDerive,
  deriveOptionsFor,
  deriveToParam,
  describeDerive,
  effectiveScale,
  singleFixFor,
} from "@/components/viz/lib/derive";

describe("derivations", () => {
  it("offers a derivation only where the scale admits a change", () => {
    expect(deriveOptionsFor("nominal", "artifact")).toEqual([]);
    expect(deriveOptionsFor("ordinal", "artifact")).toEqual([]);
    expect(deriveOptionsFor("ratio", "attr:bytes")).toEqual(["bins"]);
    expect(deriveOptionsFor("interval", "attr:logon_at")).toEqual(["bins", "timePart"]);
    expect(deriveOptionsFor("interval", "time:date")).toEqual([]);
    expect(deriveOptionsFor("ratio", null)).toEqual([]);
  });

  it("a derived field is ordered categories", () => {
    expect(effectiveScale("ratio", { kind: "bins", mode: "log", count: 8 })).toBe("ordinal");
    expect(effectiveScale("ratio", null)).toBe("ratio");
  });

  it("serialises for the API in snake_case, and not at all when absent", () => {
    expect(deriveToParam(null)).toBeUndefined();
    expect(JSON.parse(deriveToParam({ kind: "timePart", part: "weekday" })!)).toEqual({
      kind: "time_part",
      part: "weekday",
    });
    expect(JSON.parse(deriveToParam({ kind: "bins", mode: "custom", edges: [0, 1024] })!)).toEqual({
      kind: "bins",
      mode: "custom",
      edges: [0, 1024],
    });
  });

  it("describes itself for the caption, with the resolved edges when known", () => {
    expect(
      describeDerive({ kind: "bins", mode: "log", count: 3 }, { kind: "bins", labels: [], edges: [10, 100] }),
    ).toBe("grouped into 3 log-spaced ranges (edges: 10 · 100)");
    expect(describeDerive({ kind: "bins", mode: "custom", edges: [0, 1024] })).toBe(
      "grouped by your edges: 0 · 1,024",
    );
    expect(describeDerive({ kind: "timePart", part: "hour" })).toBe("calendar part: hour of day (UTC)");
  });

  it("finds the single fix that lights a greyed figure, and refuses to guess between two", () => {
    // Bar is illegal at ratio; bins is the only derivation ratio offers.
    expect(singleFixFor("bar", "ratio", "attr:bytes")).toEqual(defaultDerive("bins", "ratio"));
    // Interval offers bins *and* a calendar part — two fixes, so none is applied.
    expect(singleFixFor("bar", "interval", "attr:logon_at")).toBeNull();
    // Already legal: nothing to fix.
    expect(singleFixFor("histogram", "ratio", "attr:bytes")).toBeNull();
    // Legal only via a derivation the figure does not admit (pie has no derives).
    expect(singleFixFor("pie", "ratio", "attr:bytes")).toBeNull();
  });
});
