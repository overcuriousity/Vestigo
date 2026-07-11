/**
 * Smoke render for every chart component — verifies each renders an <svg>
 * without throwing, given minimal-but-realistic fixture data shaped like
 * the corresponding `vizApi`/`eventsApi` response. Not a pixel/geometry
 * test; catches the "throws on mount" and "throws on empty data" classes of
 * bug cheaply.
 *
 * jsdom has no ResizeObserver, which `ChartFrame` depends on to learn its
 * container width — polyfill it to synchronously report a fixed width so
 * the chart's `{width > 0 && ...}` gate actually renders content.
 */
import { describe, it, expect, beforeAll } from "vitest";
import { fireEvent, render } from "@testing-library/react";
import { CompareHistogram } from "@/components/viz/charts/CompareHistogram";
import { BarChart } from "@/components/viz/charts/BarChart";
import { PieChart } from "@/components/viz/charts/PieChart";
import { NumericHistogram } from "@/components/viz/charts/NumericHistogram";
import { BoxPlot } from "@/components/viz/charts/BoxPlot";
import { ViolinPlot } from "@/components/viz/charts/ViolinPlot";
import { LineChart } from "@/components/viz/charts/LineChart";
import { Heatmap } from "@/components/viz/charts/Heatmap";
import { EcdfChart } from "@/components/viz/charts/EcdfChart";
import { TimeHistogram } from "@/components/viz/charts/TimeHistogram";
import { PunchCard } from "@/components/viz/charts/PunchCard";
import { PivotHeatmap } from "@/components/viz/charts/PivotHeatmap";
import { SankeyFlow } from "@/components/viz/charts/SankeyFlow";
import { ScatterChart } from "@/components/viz/charts/ScatterChart";
import type {
  FieldNumericResponse,
  FieldPivotResponse,
  FieldScatterResponse,
  FieldTermsResponse,
  FieldTimeseriesResponse,
  HistogramBucket,
  PunchcardResponse,
} from "@/api/types";

beforeAll(() => {
  class FakeResizeObserver {
    private cb: ResizeObserverCallback;
    constructor(cb: ResizeObserverCallback) {
      this.cb = cb;
    }
    observe(target: Element) {
      // Synchronously report a fixed content width, as if the container
      // were already laid out — jsdom never actually lays anything out.
      this.cb(
        [{ target, contentRect: { width: 400 } } as unknown as ResizeObserverEntry],
        this as unknown as ResizeObserver,
      );
    }
    unobserve() {}
    disconnect() {}
  }
  // @ts-expect-error -- jsdom has no native ResizeObserver
  global.ResizeObserver = FakeResizeObserver;
});

const TERMS: FieldTermsResponse = {
  field: "artifact",
  total: 100,
  distinct: 3,
  other_count: 10,
  values: [
    { value: "GET", count: 60 },
    { value: "POST", count: 30 },
  ],
};

const NUMERIC: FieldNumericResponse = {
  field: "attr:bytes_sent",
  count: 100,
  min: 0,
  max: 100,
  mean: 50,
  stddev: 20,
  quantiles: { "0.25": 25, "0.5": 50, "0.75": 75 },
  bins: [
    { x0: 0, x1: 50, count: 60 },
    { x0: 50, x1: 100, count: 40 },
  ],
};

const HIST_BUCKETS: HistogramBucket[] = [
  { start: "2024-01-01T00:00:00Z", count: 5 },
  { start: "2024-01-01T01:00:00Z", count: 12 },
];

const TIMESERIES: FieldTimeseriesResponse = {
  field: "attr:status_code",
  interval_seconds: 3600,
  min: "2024-01-01T00:00:00Z",
  max: "2024-01-01T01:00:00Z",
  series: [
    {
      value: "200",
      buckets: HIST_BUCKETS,
    },
    {
      value: "500",
      buckets: [
        { start: "2024-01-01T00:00:00Z", count: 1 },
        { start: "2024-01-01T01:00:00Z", count: 0 },
      ],
    },
  ],
};

const PUNCHCARD: PunchcardResponse = {
  kind: "punchcard",
  total: 107,
  max_count: 60,
  cells: [
    { dow: 1, hour: 9, count: 40 },
    { dow: 1, hour: 10, count: 60 },
    { dow: 6, hour: 3, count: 7 },
  ],
};

const PIVOT: FieldPivotResponse = {
  kind: "pivot",
  field_x: "attr:username",
  field_y: "attr:workstation",
  x_values: ["alice", "bob"],
  y_values: ["WS01", "WS02"],
  x_distinct: 5,
  y_distinct: 3,
  cells: [
    { x: "alice", y: "WS01", count: 40 },
    { x: "bob", y: "WS02", count: 12 },
    { x: "", y: "WS01", count: 3 },
  ],
  total: 55,
};

const SCATTER: FieldScatterResponse = {
  kind: "scatter",
  field_x: "attr:bytes",
  field_y: "attr:latency",
  total: 1000,
  sampled: 3,
  x_min: 0,
  x_max: 100,
  y_min: 1,
  y_max: 50,
  points: [
    [10, 5],
    [50, 20],
    [99, 49],
  ],
};

function expectSvg(container: HTMLElement) {
  expect(container.querySelector("svg")).not.toBeNull();
}

describe("chart smoke render", () => {
  it("BarChart renders with terms data", () => {
    const { container } = render(<BarChart terms={TERMS} />);
    expectSvg(container);
  });
  it("BarChart renders the empty state", () => {
    const { getByText } = render(
      <BarChart terms={{ ...TERMS, values: [], other_count: 0 }} />,
    );
    getByText(/no values/i);
  });

  it("PieChart renders with terms data", () => {
    const { container } = render(<PieChart terms={TERMS} />);
    expectSvg(container);
  });

  it("NumericHistogram renders with numeric stats", () => {
    const { container } = render(<NumericHistogram stats={NUMERIC} />);
    expectSvg(container);
  });
  it("NumericHistogram renders empty state for a non-numeric field", () => {
    const { getByText } = render(
      <NumericHistogram stats={{ ...NUMERIC, count: 0, min: null, max: null, bins: [] }} />,
    );
    getByText(/no numeric values/i);
  });

  it("BoxPlot renders with numeric stats", () => {
    const { container } = render(<BoxPlot stats={NUMERIC} />);
    expectSvg(container);
  });

  it("ViolinPlot renders with numeric stats", () => {
    const { container } = render(<ViolinPlot stats={NUMERIC} />);
    expectSvg(container);
  });

  it("EcdfChart renders with numeric stats", () => {
    const { container } = render(<EcdfChart stats={NUMERIC} />);
    expectSvg(container);
  });

  it("LineChart renders with timeseries data", () => {
    const { container } = render(<LineChart data={TIMESERIES} />);
    expectSvg(container);
  });

  it("Heatmap renders with timeseries data", () => {
    const { container } = render(<Heatmap data={TIMESERIES} />);
    expectSvg(container);
  });

  it("TimeHistogram renders with histogram buckets", () => {
    const { container } = render(<TimeHistogram buckets={HIST_BUCKETS} />);
    expectSvg(container);
  });
  it("TimeHistogram renders the empty state", () => {
    const { getByText } = render(<TimeHistogram buckets={[]} />);
    getByText(/no events over time/i);
  });

  it("PunchCard renders with punchcard data", () => {
    const { container } = render(<PunchCard data={PUNCHCARD} />);
    expectSvg(container);
  });
  it("PunchCard renders the empty state", () => {
    const { getByText } = render(
      <PunchCard data={{ kind: "punchcard", total: 0, max_count: 0, cells: [] }} />,
    );
    getByText(/no dated events/i);
  });

  it("PivotHeatmap renders with pivot data (incl. Other rollup)", () => {
    const { container, getByText } = render(<PivotHeatmap data={PIVOT} />);
    expectSvg(container);
    getByText("Other"); // '' cell surfaces as an explicit Other row/column
  });
  it("PivotHeatmap renders the empty state", () => {
    const { getByText } = render(
      <PivotHeatmap data={{ ...PIVOT, cells: [], total: 0 }} />,
    );
    getByText(/no events with both fields/i);
  });
  it("PivotHeatmap reports both field=value pairs on cell click", () => {
    const clicks: [string, string][][] = [];
    const { container } = render(
      <PivotHeatmap data={PIVOT} onValueClick={(c) => clicks.push(c.entries)} />,
    );
    const cells = [...container.querySelectorAll("rect")].filter(
      (r) => r.style.cursor === "pointer",
    );
    expect(cells.length).toBeGreaterThan(0);
    cells[0].dispatchEvent(new MouseEvent("click", { bubbles: true }));
    expect(clicks[0]).toEqual([
      ["attr:username", "alice"],
      ["attr:workstation", "WS01"],
    ]);
  });

  it("SankeyFlow renders with pivot data", () => {
    const { container } = render(<SankeyFlow data={PIVOT} />);
    expectSvg(container);
    expect(container.querySelectorAll("path").length).toBeGreaterThan(0);
  });
  it("SankeyFlow renders the empty state", () => {
    const { getByText } = render(<SankeyFlow data={{ ...PIVOT, cells: [], total: 0 }} />);
    getByText(/no events with both fields/i);
  });

  it("ScatterChart renders sampled points", () => {
    const { container } = render(<ScatterChart data={SCATTER} />);
    expectSvg(container);
    expect(container.querySelectorAll("circle").length).toBe(3);
  });
  it("ScatterChart renders the categorical-fallback empty state", () => {
    const { getByText } = render(
      <ScatterChart
        data={{ ...SCATTER, total: 0, sampled: 0, points: [], x_min: null, x_max: null, y_min: null, y_max: null }}
      />,
    );
    getByText(/no events with numeric values/i);
  });
  it("ScatterChart renders with log scale (positive domain)", () => {
    const { container } = render(<ScatterChart data={{ ...SCATTER, x_min: 1 }} logScale />);
    expectSvg(container);
  });

  it("CompareHistogram brush drag reports a bucket-snapped range", () => {
    const ranges: [string, string][] = [];
    const { container } = render(
      <CompareHistogram
        data={{
          kind: "time",
          interval_seconds: 3600,
          min: "2024-01-01T00:00:00Z",
          max: "2024-01-01T02:00:00Z",
          buckets: [
            { start: "2024-01-01T00:00:00Z", primary: 5, comparison: 0 },
            { start: "2024-01-01T01:00:00Z", primary: 12, comparison: 0 },
          ],
          primary_total: 17,
          comparison_total: 0,
        }}
        metric="count"
        hasComparison={false}
        onRangeSelect={(start, end) => ranges.push([start, end])}
      />,
    );
    const overlay = container.querySelector('rect[fill="transparent"]');
    expect(overlay).not.toBeNull();
    fireEvent.mouseDown(overlay!, { clientX: 100, clientY: 60 });
    fireEvent.mouseMove(overlay!, { clientX: 250, clientY: 60 });
    fireEvent.mouseUp(overlay!, { clientX: 250, clientY: 60 });
    expect(ranges).toHaveLength(1);
    const [start, end] = ranges[0];
    // Snapped outward to the hour grid the buckets used.
    expect(new Date(start).getTime() % 3_600_000).toBe(0);
    expect(new Date(end).getTime() % 3_600_000).toBe(0);
    expect(new Date(end).getTime()).toBeGreaterThan(new Date(start).getTime());
  });

  it("CompareHistogram ignores sub-threshold drags (clicks)", () => {
    const ranges: [string, string][] = [];
    const { container } = render(
      <CompareHistogram
        data={{
          kind: "time",
          interval_seconds: 3600,
          min: "2024-01-01T00:00:00Z",
          max: "2024-01-01T02:00:00Z",
          buckets: [{ start: "2024-01-01T00:00:00Z", primary: 5, comparison: 0 }],
          primary_total: 5,
          comparison_total: 0,
        }}
        metric="count"
        hasComparison={false}
        onRangeSelect={(start, end) => ranges.push([start, end])}
      />,
    );
    const overlay = container.querySelector('rect[fill="transparent"]');
    fireEvent.mouseDown(overlay!, { clientX: 100, clientY: 60 });
    fireEvent.mouseUp(overlay!, { clientX: 102, clientY: 60 });
    expect(ranges).toHaveLength(0);
  });
});
