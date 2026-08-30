import { beforeAll, describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { IntervalLanes } from "@/components/viz/charts/IntervalLanes";
import type { LanesResponse, ResolvedMark } from "@/api/types";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

beforeAll(() => installFakeResizeObserver());

const data: LanesResponse = {
  kind: "lanes",
  field: "attr:host",
  pairing: "next_end",
  lanes: [
    {
      key: "h2",
      count: 4,
      intervals: [
        { start: "2026-07-20T09:00:00+00:00", end: null, start_event_id: "e2", end_event_id: null },
        {
          start: "2026-07-20T10:00:00+00:00",
          end: "2026-07-20T11:00:00+00:00",
          start_event_id: "e4",
          end_event_id: "e6",
        },
      ],
    },
    {
      key: "h1",
      count: 3,
      intervals: [
        {
          start: "2026-07-20T09:00:00+00:00",
          end: "2026-07-20T10:00:00+00:00",
          start_event_id: "e1",
          end_event_id: "e3",
        },
      ],
    },
  ],
  lane_cap: 10,
  lanes_total: 2,
  lane_cap_hit: false,
  other_lanes: 0,
  starts: 4,
  ends: 3,
  unpaired_starts: 1,
  orphan_ends: 1,
  rows_cap: 50000,
  rows_truncated: false,
  rows_paired: 7,
  undated: 0,
  slice_start: "2026-07-20T08:00:00+00:00",
  slice_end: "2026-07-20T14:00:00+00:00",
};

describe("IntervalLanes", () => {
  it("draws one rect per interval under its lane, in lane order", () => {
    const { container } = render(<IntervalLanes data={data} />);
    expect(container.querySelectorAll("rect[data-lane-interval]")).toHaveLength(3);
    const lanes = [...container.querySelectorAll("g[data-lane]")].map((g) =>
      g.getAttribute("data-lane"),
    );
    expect(lanes).toEqual(["h2", "h1"]);
    expect(
      container.querySelector('g[data-lane="h2"]')!.querySelectorAll("rect[data-lane-interval]"),
    ).toHaveLength(2);
  });

  it("runs an open-ended interval to the slice end under an arrowhead", () => {
    const { container } = render(<IntervalLanes data={data} />);
    const open = container.querySelector('rect[data-lane-interval][data-open="true"]')!;
    const closed = container.querySelector('rect[data-lane-interval][data-open="false"]')!;
    const right = (r: Element) => Number(r.getAttribute("x")) + Number(r.getAttribute("width"));
    expect(right(open)).toBeGreaterThan(right(closed));
    expect(container.querySelectorAll("path[data-lane-open-arrow]")).toHaveLength(1);
  });

  it("puts the arrowhead at the bar's own end, never left of where it starts", () => {
    // Pinned to the panel's right edge instead, an interval opening within a
    // few pixels of it drew its arrowhead *behind* its own start — pointing at
    // time the interval does not cover.
    const lateOpen: LanesResponse = {
      ...data,
      lanes: [
        {
          key: "h3",
          count: 1,
          intervals: [
            {
              start: "2026-07-20T13:59:00+00:00",
              end: null,
              start_event_id: "e9",
              end_event_id: null,
            },
          ],
        },
      ],
      lanes_total: 1,
    };
    const { container } = render(<IntervalLanes data={lateOpen} />);
    const bar = container.querySelector('rect[data-lane-interval][data-open="true"]')!;
    const barStart = Number(bar.getAttribute("x"));
    const barEnd = barStart + Number(bar.getAttribute("width"));
    const xs = [
      ...(container.querySelector("path[data-lane-open-arrow]")!.getAttribute("d") ?? "").matchAll(
        /[ML](-?[\d.]+)/g,
      ),
    ].map((m) => Number(m[1]));
    expect(Math.min(...xs)).toBeGreaterThanOrEqual(barStart);
    expect(Math.min(...xs)).toBeCloseTo(barEnd, 5);
  });

  it("nested intervals in one lane are both drawn", () => {
    const { container } = render(<IntervalLanes data={data} />);
    const h2 = [
      ...container.querySelector('g[data-lane="h2"]')!.querySelectorAll("rect[data-lane-interval]"),
    ];
    const xs = h2.map((r) => Number(r.getAttribute("x")));
    expect(xs[0]).toBeLessThan(xs[1]);
  });

  it("draws marks across the lanes", () => {
    const marks: ResolvedMark[] = [
      {
        kind: "instant",
        at: "2026-07-20T09:30:00+00:00",
        label: "beacon",
        source: 0,
        provenance: { kind: "analyst" },
      },
    ];
    const { container } = render(<IntervalLanes data={data} marks={marks} />);
    expect(container.querySelector("[data-marks]")).not.toBeNull();
    expect(container.querySelectorAll("line[data-mark-instant]")).toHaveLength(1);
  });

  it("renders the empty state with a pairing-specific hint", () => {
    const { getByText } = render(
      <IntervalLanes
        data={{ ...data, lanes: [], lanes_total: 0, slice_start: null, slice_end: null }}
      />,
    );
    getByText(/no intervals to draw/i);
    getByText(/start filter|end filter/i);
  });
});
