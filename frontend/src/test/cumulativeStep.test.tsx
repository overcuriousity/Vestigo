import { beforeAll, describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { CumulativeStep } from "@/components/viz/charts/CumulativeStep";
import type { CumulativeResponse, ResolvedMark } from "@/api/types";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

beforeAll(() => installFakeResizeObserver());

const data: CumulativeResponse = {
  kind: "cumulative",
  quantity: "events",
  field: null,
  interval_seconds: 3600,
  min: "2026-07-20T00:00:00+00:00",
  max: "2026-07-20T03:59:59+00:00",
  buckets: [
    { start: "2026-07-20T00:00:00+00:00", delta: 2, value: 2 },
    { start: "2026-07-20T01:00:00+00:00", delta: 0, value: 2 },
    { start: "2026-07-20T02:00:00+00:00", delta: 3, value: 5 },
    { start: "2026-07-20T03:00:00+00:00", delta: 2, value: 7 },
  ],
  total: 7,
  events: 7,
  unparsed: 0,
};

describe("CumulativeStep", () => {
  it("draws one step path through every bucket and never interpolates", () => {
    const { container } = render(<CumulativeStep data={data} />);
    const path = container.querySelector("path[data-cumulative-step]")!;
    const d = path.getAttribute("d")!;
    // curveStepAfter: straight segments only, each horizontal or vertical —
    // never a diagonal (which would assert growth inside a bucket).
    expect(d.startsWith("M")).toBe(true);
    expect(d).not.toMatch(/[CQ]/);
    const pts = (d.match(/-?[\d.]+,-?[\d.]+/g) ?? []).map((p) => p.split(",").map(Number));
    // Four buckets plus the closing point: two segments per step.
    expect(pts.length).toBeGreaterThanOrEqual(2 * data.buckets.length + 1);
    for (let i = 1; i < pts.length; i++) {
      const sameX = Math.abs(pts[i][0] - pts[i - 1][0]) < 1e-6;
      const sameY = Math.abs(pts[i][1] - pts[i - 1][1]) < 1e-6;
      expect(sameX || sameY).toBe(true);
    }
  });

  it("overlays marks on the same time axis", () => {
    const marks: ResolvedMark[] = [
      {
        kind: "instant",
        at: "2026-07-20T02:30:00+00:00",
        label: "x",
        source: 0,
        provenance: { kind: "analyst" },
      },
    ];
    const { container } = render(<CumulativeStep data={data} marks={marks} />);
    expect(container.querySelectorAll("line[data-mark-instant]")).toHaveLength(1);
  });

  it("renders the empty state without dated events", () => {
    const { getByText } = render(
      <CumulativeStep
        data={{
          ...data,
          buckets: [],
          total: 0,
          events: 0,
          min: null,
          max: null,
          interval_seconds: 0,
        }}
      />,
    );
    getByText(/no dated events/i);
  });
});
