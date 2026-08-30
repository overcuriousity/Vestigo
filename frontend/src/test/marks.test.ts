import { describe, expect, it } from "vitest";
import { layoutMarks, markCaptionLines, numberInstants } from "@/components/viz/lib/marks";
import type { ResolvedMark, ResolvedMarksResponse } from "@/api/types";

const T0 = new Date("2026-07-20T00:00:00Z").getTime();
const x = (d: Date) => (d.getTime() - T0) / 3_600_000; // 1px per hour, 24px wide axis

const marks: ResolvedMark[] = [
  { kind: "instant", at: "2026-07-20T05:00:00+00:00", label: "beacons", source: 0, provenance: { kind: "event", event_id: "e5", source_id: "s1" } },
  { kind: "instant", at: "2026-07-20T01:00:00+00:00", label: "beacons", source: 0, provenance: { kind: "event", event_id: "e1", source_id: "s1" } },
  { kind: "instant", at: "2026-07-20T01:30:00+00:00", label: "first", source: 1, provenance: { kind: "analyst" } },
  { kind: "instant", at: "2026-07-21T04:00:00+00:00", label: "beacons", source: 0, provenance: { kind: "event", event_id: "e9", source_id: "s1" } },
  { kind: "range", start: "2026-07-20T02:00:00+00:00", end: "2026-07-20T10:00:00+00:00", label: "Quiet — baseline", source: 2, provenance: { kind: "baseline", definition_id: "bd1", window_id: "baseline" } },
  { kind: "range", start: "2026-07-20T04:00:00+00:00", end: "2026-07-20T06:00:00+00:00", label: "Quiet — Exfil day", source: 2, provenance: { kind: "baseline", definition_id: "bd1", window_id: "w0" } },
  { kind: "range", start: "2026-07-19T00:00:00+00:00", end: "2026-07-19T12:00:00+00:00", label: "yesterday", source: 3, provenance: { kind: "analyst" } },
];

describe("numberInstants", () => {
  it("numbers instants by time, across sources", () => {
    expect(numberInstants(marks).map((i) => [i.n, i.mark.at.slice(11, 16), i.mark.source])).toEqual([
      [1, "01:00", 0],
      [2, "01:30", 1],
      [3, "05:00", 0],
      [4, "04:00", 0],
    ]);
  });
});

describe("layoutMarks", () => {
  it("drops off-axis instants, alternates tiers when crowded, stacks overlapping ranges", () => {
    const layout = layoutMarks(marks, x, 24);
    expect(layout.instants.map((i) => [i.n, i.px, i.tier])).toEqual([
      [1, 1, 0],
      [2, 1.5, 1], // 0.5px from #1 — crowded, so the label steps up a tier
      [3, 5, 0],
    ]);
    expect(layout.offscreen).toBe(1); // #4 is on the next day
    expect(layout.ranges.map((r) => [r.mark.label, r.x0, r.x1, r.tier])).toEqual([
      ["Quiet — baseline", 2, 10, 0],
      ["Quiet — Exfil day", 4, 6, 1], // overlaps the baseline band → next tier
    ]);
  });

  it("clamps a range that runs past the axis", () => {
    const layout = layoutMarks(
      [{ kind: "range", start: "2026-07-19T20:00:00+00:00", end: "2026-07-20T03:00:00+00:00", label: "w", source: 0, provenance: { kind: "analyst" } }],
      x,
      24,
    );
    expect(layout.ranges[0]).toMatchObject({ x0: 0, x1: 3, tier: 0 });
  });
});

describe("markCaptionLines", () => {
  const resp: ResolvedMarksResponse = {
    marks,
    cap: 50,
    sources: [
      { index: 0, kind: "events", label: "beacons", count: 3, shown: 3, overflow: false, undated: 1 },
      { index: 1, kind: "instant", label: "first", count: 1, shown: 1, overflow: false, undated: 0 },
      { index: 2, kind: "baseline", label: "Quiet", count: 2, shown: 2, overflow: false, undated: 0 },
      { index: 3, kind: "range", label: "yesterday", count: 1, shown: 1, overflow: false, undated: 0 },
    ],
  };
  it("names every source with its provenance, its numbers, the cap and the undated events", () => {
    expect(markCaptionLines(resp)).toEqual([
      'marks #1, #3, #4: "beacons" — 3 events matching a filter; 1 undated event not drawn',
      'mark #2: "first" at 2026-07-20 01:30:00Z — analyst-placed',
      'marks: baseline "Quiet" — its baseline window and 1 suspect window, as declared',
      'mark: "yesterday" 2026-07-19 00:00:00Z → 2026-07-19 12:00:00Z — analyst-placed',
    ]);
  });
  it("states the cap when a source overflowed and abbreviates long number lists", () => {
    const many: ResolvedMark[] = Array.from({ length: 7 }, (_, i) => ({
      kind: "instant" as const,
      at: `2026-07-20T0${i + 1}:00:00+00:00`,
      label: "v",
      source: 0,
      provenance: { kind: "view" as const, view_id: "v1", event_id: `e${i}`, source_id: "s1" },
    }));
    const lines = markCaptionLines({
      marks: many,
      cap: 7,
      sources: [{ index: 0, kind: "view", label: "Beacons", count: 40, shown: 7, overflow: true, undated: 0 }],
    });
    expect(lines).toEqual(['marks #1–#7: "Beacons" — 40 events of saved view; the earliest 7 drawn (cap 7), 33 not drawn']);
  });
  it("does not claim a contiguous range when two sources interleave", () => {
    // Numbering is global and by time, so source 0 owns #1,#3,#5,#7,#9,#11 —
    // "#1–#11" would name five rules that belong to source 1.
    const interleaved: ResolvedMark[] = Array.from({ length: 12 }, (_, i) => ({
      kind: "instant" as const,
      at: `2026-07-20T${String(i).padStart(2, "0")}:00:00+00:00`,
      label: i % 2 === 0 ? "X" : "Y",
      source: i % 2,
      provenance: { kind: "analyst" as const },
    }));
    const lines = markCaptionLines({
      marks: interleaved,
      cap: 50,
      sources: [
        { index: 0, kind: "events", label: "X", count: 6, shown: 6, overflow: false, undated: 0 },
        { index: 1, kind: "events", label: "Y", count: 6, shown: 6, overflow: false, undated: 0 },
      ],
    });
    expect(lines[0]).toBe('marks 6 of #1–#11: "X" — 6 events matching a filter');
    expect(lines[1]).toBe('marks 6 of #2–#12: "Y" — 6 events matching a filter');
  });
  it("says how many of a source's marks fall outside the drawn axis", () => {
    // The axis of `layoutMarks` above: 2026-07-20 00:00 → 24:00. Instant #4
    // (the 21st) and the "yesterday" range are outside it, and the caption
    // has to say so — nothing draws them.
    const domain: [Date, Date] = [
      new Date("2026-07-20T00:00:00Z"),
      new Date("2026-07-21T00:00:00Z"),
    ];
    const lines = markCaptionLines(resp, domain);
    expect(lines[0]).toBe(
      'marks #1, #3, #4: "beacons" — 3 events matching a filter; 1 undated event not drawn; 1 outside the drawn time axis, not drawn',
    );
    expect(lines[1]).not.toContain("outside the drawn time axis");
    expect(lines[2]).not.toContain("outside the drawn time axis");
    expect(lines[3]).toBe(
      'mark: "yesterday" 2026-07-19 00:00:00Z → 2026-07-19 12:00:00Z — analyst-placed; outside the drawn time axis, not drawn',
    );
  });
  it("counts exactly what the layout dropped", () => {
    const domain: [Date, Date] = [
      new Date("2026-07-20T00:00:00Z"),
      new Date("2026-07-21T00:00:00Z"),
    ];
    const layout = layoutMarks(marks, x, 24);
    const disclosed = markCaptionLines(resp, domain)
      .join(" ")
      .match(/outside the drawn time axis/g);
    // One instant (#4) and one range (source 3) — the layout drops both.
    expect(layout.offscreen).toBe(1);
    expect(disclosed).toHaveLength(2);
  });
});

describe("layoutMarks — degenerate ranges", () => {
  const zeroLength: ResolvedMark[] = [
    {
      kind: "range",
      start: "2026-07-20T03:00:00+00:00",
      end: "2026-07-20T03:00:00+00:00",
      label: "instant window",
      source: 0,
      provenance: { kind: "analyst" },
    },
  ];

  it("draws a zero-length range inside the domain rather than dropping it", () => {
    // `x1 > x0` used to drop it while `outsideDomain` still called it drawn —
    // only a range *fully* outside the domain is reported off-axis — so the
    // caption described a band that was not on the canvas (#332).
    const layout = layoutMarks(zeroLength, x, 24);
    expect(layout.ranges).toHaveLength(1);
    const [band] = layout.ranges;
    expect(band.x0).toBe(3);
    expect(band.x1).toBeGreaterThan(band.x0);
  });

  it("still drops a range that starts past the right edge", () => {
    // That one `outsideDomain` does report, so the caption stays honest.
    const offEdge: ResolvedMark[] = [
      {
        kind: "range",
        start: "2026-07-20T30:00:00+00:00",
        end: "2026-07-20T30:00:00+00:00",
        label: "past the axis",
        source: 0,
        provenance: { kind: "analyst" },
      },
    ];
    expect(layoutMarks(offEdge, x, 24).ranges).toHaveLength(0);
  });
});

describe("layoutMarks and the caption's off-axis verdict, at the edges", () => {
  // Two ranges that touch the domain at exactly one instant: one ending on the
  // first bucket start, one starting on the last. `layoutMarks` drew both as
  // minimum-width bands while `outsideDomain` (`end <= lo || start >= hi`)
  // called them not drawn, so the caption said "outside the drawn time axis,
  // not drawn" under a band visibly on the canvas (#332).
  const domain = [new Date(T0), new Date(T0 + 24 * 3_600_000)] as const;
  const touching: ResolvedMark[] = [
    {
      kind: "range",
      start: "2026-07-19T20:00:00+00:00",
      end: "2026-07-20T00:00:00+00:00",
      label: "ends on the first bucket",
      source: 0,
      provenance: { kind: "analyst" },
    },
    {
      kind: "range",
      start: "2026-07-21T00:00:00+00:00",
      end: "2026-07-21T04:00:00+00:00",
      label: "starts on the last",
      source: 1,
      provenance: { kind: "analyst" },
    },
  ];
  const resp: ResolvedMarksResponse = {
    marks: touching,
    cap: 50,
    sources: [
      { index: 0, kind: "range", label: "ends on the first bucket", count: 1, shown: 1, overflow: false, undated: 0 },
      { index: 1, kind: "range", label: "starts on the last", count: 1, shown: 1, overflow: false, undated: 0 },
    ],
  };

  it("draws both, at the edge they touch", () => {
    const ranges = layoutMarks(touching, x, 24).ranges;
    expect(ranges.map((r) => [r.mark.label, r.x0, r.x1])).toEqual([
      ["ends on the first bucket", 0, 2],
      ["starts on the last", 22, 24],
    ]);
  });

  it("and the caption calls neither of them off-axis", () => {
    for (const line of markCaptionLines(resp, domain)) {
      expect(line).not.toContain("outside the drawn time axis");
    }
  });

  it("still calls a range wholly outside off-axis, and does not draw it", () => {
    const away: ResolvedMark[] = [
      {
        kind: "range",
        start: "2026-07-18T00:00:00+00:00",
        end: "2026-07-19T00:00:00+00:00",
        label: "the day before",
        source: 0,
        provenance: { kind: "analyst" },
      },
    ];
    expect(layoutMarks(away, x, 24).ranges).toHaveLength(0);
    expect(
      markCaptionLines({ ...resp, marks: away, sources: [resp.sources[0]] }, domain)[0],
    ).toContain("outside the drawn time axis, not drawn");
  });
});
