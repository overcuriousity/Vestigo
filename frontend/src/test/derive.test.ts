import { describe, expect, it } from "vitest";
import {
  defaultDerive,
  deriveOptionsFor,
  deriveSourceScale,
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

  it("prints the server's edge labels rather than rounding the floats itself", () => {
    // `db/derive.py::_fmt_edges` cuts these to the precision that names each
    // edge, and cuts the bin labels at the same place. Three significant
    // digits here instead said `4,000 · 4,001` under bins starting at
    // 4000.125 — the caption and the axis naming different boundaries.
    expect(
      describeDerive(
        { kind: "bins", mode: "width", count: 3 },
        {
          kind: "bins",
          labels: [],
          edges: [4000.125, 4000.875],
          edge_labels: ["4,000.125", "4,000.875"],
        },
      ),
    ).toBe("grouped into 3 equal-width ranges (edges: 4,000.125 · 4,000.875)");
  });

  it("names the ranges there are, not the ranges asked for", () => {
    // Over a range narrow relative to its magnitude float64 cannot separate
    // the edges, so `db/derive.py::bin_edges` places fewer than `count - 1`
    // and the axis carries fewer ranges than the analyst asked for (#332).
    expect(
      describeDerive(
        { kind: "bins", mode: "width", count: 8 },
        { kind: "bins", labels: [], edges: [10, 20], edge_labels: ["10", "20"] },
      ),
    ).toBe(
      "grouped into 3 equal-width ranges (edges: 10 · 20) — 8 asked for; the values in this slice do not separate more",
    );
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

describe("deriveSourceScale", () => {
  it("keeps a treat-as the derivation is offered under, and falls back to its natural one", () => {
    expect(deriveSourceScale("bins", "ratio")).toBe("ratio");
    expect(deriveSourceScale("bins", "interval")).toBe("interval");
    expect(deriveSourceScale("timePart", "interval")).toBe("interval");
    // The effective scale, and categories, are not what it was computed from.
    expect(deriveSourceScale("bins", "ordinal")).toBe("ratio");
    expect(deriveSourceScale("bins", "nominal")).toBe("ratio");
    expect(deriveSourceScale("timePart", "ratio")).toBe("interval");
    expect(deriveSourceScale("bins", null)).toBe("ratio");
    expect(deriveSourceScale("timePart", undefined)).toBe("interval");
  });

  it("agrees with deriveOptionsFor in both directions", () => {
    for (const scale of ["nominal", "ordinal", "interval", "ratio"] as const) {
      for (const kind of ["bins", "timePart"] as const) {
        const offered = deriveOptionsFor(scale, "attr:x").includes(kind);
        expect(deriveSourceScale(kind, scale) === scale).toBe(offered);
      }
    }
  });
});
