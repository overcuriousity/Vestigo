/**
 * Smoke render for every chart component — verifies each renders an <svg>
 * without throwing, given minimal-but-realistic fixture data shaped like
 * the corresponding `vizApi`/`eventsApi` response. Not a pixel/geometry
 * test; catches the "throws on mount" and "throws on empty data" classes of
 * bug cheaply.
 *
 * The `time-field labelling` block is not a smoke test: it pins that every
 * chart humanises a virtual `time:` field's values in its text while keeping
 * the canonical value in every key, colour and click payload.
 */
import { describe, it, expect, beforeAll, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { installFakeResizeObserver } from "./helpers/resizeObserver";
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
import { WaffleChart } from "@/components/viz/charts/WaffleChart";
import { CorrMatrix } from "@/components/viz/charts/CorrMatrix";
import { FacetGrid } from "@/components/viz/FacetGrid";
import { GroupedDistribution } from "@/components/viz/charts/GroupedDistribution";
import type {
  FieldCorrelationResponse,
  FieldNumericGroupedResponse,
  FieldNumericResponse,
  FieldPivotResponse,
  FieldScatterResponse,
  FieldTermsResponse,
  FieldTimeseriesResponse,
  HistogramBucket,
  PunchcardResponse,
} from "@/api/types";

beforeAll(() => installFakeResizeObserver());

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
  skewness: 0,
  points: null,
  bin_rule: "manual",
  bin_width: 25,
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
  x_bounded: false,
  y_bounded: false,
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
  stats: {
    n: 1000,
    basis: "full",
    pearson: { r: 0.8, p: 1e-9 },
    spearman: { rho: 0.75, p: 1e-8 },
    kendall: { tau: 0.6, p: 0.001, basis: "sample", n: 3 },
    regression: { slope: 0.5, intercept: 2, r_squared: 0.64 },
    shapiro: { x: { w: 0.98, p: 0.3 }, y: { w: 0.97, p: 0.2 }, basis: "sample", n: 3 },
    recommendation: "pearson",
  },
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

describe("time-field labelling", () => {
  // "1".."7" are the canonical day-of-week values (ISO, Mon=1), which is what
  // filters, keys and colours use. Only text should read "Mon".
  const DOW_TERMS: FieldTermsResponse = {
    field: "time:day_of_week",
    total: 90,
    distinct: 7,
    other_count: 0,
    values: [
      { value: "1", count: 60 },
      { value: "7", count: 30 },
    ],
  };

  const DOW_TIMESERIES: FieldTimeseriesResponse = {
    field: "time:day_of_week",
    interval_seconds: 3600,
    min: "2024-01-01T00:00:00Z",
    max: "2024-01-01T01:00:00Z",
    series: [
      { value: "1", buckets: HIST_BUCKETS },
      {
        value: "7",
        buckets: [
          { start: "2024-01-01T00:00:00Z", count: 1 },
          { start: "2024-01-01T01:00:00Z", count: 4 },
        ],
      },
    ],
  };

  it("BarChart labels values in the horizontal row column", () => {
    render(<BarChart terms={DOW_TERMS} />);
    expect(screen.getByText("Mon")).toBeInTheDocument();
    expect(screen.getByText("Sun")).toBeInTheDocument();
  });

  it("BarChart labels values on the vertical band axis", () => {
    // A different code path from the row column: AxisBottomBand.labelFormat.
    render(<BarChart terms={DOW_TERMS} orientation="vertical" />);
    expect(screen.getByText("Mon")).toBeInTheDocument();
  });

  it("PieChart labels its legend entries", () => {
    render(<PieChart terms={DOW_TERMS} />);
    expect(screen.getByText(/^Mon \(/)).toBeInTheDocument();
  });

  it("Heatmap labels its row axis", () => {
    render(<Heatmap data={DOW_TIMESERIES} />);
    expect(screen.getByText("Mon")).toBeInTheDocument();
  });

  it("LineChart labels its legend entries", () => {
    render(<LineChart data={DOW_TIMESERIES} showLegend />);
    expect(screen.getByText("Mon")).toBeInTheDocument();
  });

  it("SankeyFlow labels the axis whose field is virtual, and only that one", () => {
    const pivot: FieldPivotResponse = {
      kind: "pivot",
      field_x: "time:day_of_week",
      field_y: "artifact",
      x_values: ["1", "7"],
      y_values: ["FILE"],
      x_distinct: 7,
      y_distinct: 1,
      x_bounded: false,
      y_bounded: false,
      cells: [
        { x: "1", y: "FILE", count: 40 },
        { x: "7", y: "FILE", count: 12 },
      ],
      total: 52,
    };
    render(<SankeyFlow data={pivot} />);
    expect(screen.getByText("Mon")).toBeInTheDocument();
    expect(screen.getByText("FILE")).toBeInTheDocument();
  });

  it("PivotHeatmap labels each axis by its own field", () => {
    // x is virtual and y is ordinary — the case a single shared labeller
    // would get wrong.
    const pivot: FieldPivotResponse = {
      kind: "pivot",
      field_x: "time:hour_of_day",
      field_y: "artifact",
      x_values: ["09", "10"],
      y_values: ["FILE"],
      x_distinct: 24,
      y_distinct: 1,
      x_bounded: false,
      y_bounded: false,
      cells: [
        { x: "09", y: "FILE", count: 40 },
        { x: "10", y: "FILE", count: 12 },
      ],
      total: 52,
    };
    render(<PivotHeatmap data={pivot} />);
    expect(screen.getByText("09:00")).toBeInTheDocument();
    // The ordinary axis is untouched — not relabelled, not blanked.
    expect(screen.getByText("FILE")).toBeInTheDocument();
  });

  it("LineChart legend click reports the canonical value, not the label", () => {
    // Legend falls back to `entry.key ?? entry.label`; without an explicit
    // key this would emit "Mon" and filter on a value that cannot exist.
    const onValueClick = vi.fn();
    render(<LineChart data={DOW_TIMESERIES} showLegend onValueClick={onValueClick} />);
    fireEvent.click(screen.getByText("Mon"));
    expect(onValueClick).toHaveBeenCalledTimes(1);
    expect(onValueClick.mock.calls[0][0].entries).toEqual([["time:day_of_week", "1"]]);
  });

  it("PivotHeatmap cell click reports canonical values for both axes", () => {
    const onValueClick = vi.fn();
    const pivot: FieldPivotResponse = {
      kind: "pivot",
      field_x: "time:hour_of_day",
      field_y: "artifact",
      x_values: ["09"],
      y_values: ["FILE"],
      x_distinct: 24,
      y_distinct: 1,
      x_bounded: false,
      y_bounded: false,
      cells: [{ x: "09", y: "FILE", count: 40 }],
      total: 40,
    };
    const { container } = render(<PivotHeatmap data={pivot} onValueClick={onValueClick} />);
    const cell = container.querySelector("rect[style*='cursor']");
    fireEvent.click(cell!);
    expect(onValueClick.mock.calls[0][0].entries).toEqual([
      ["time:hour_of_day", "09"],
      ["artifact", "FILE"],
    ]);
  });
});

describe("lecture-driven marks", () => {
  const GROUPED: FieldNumericGroupedResponse = {
    kind: "numeric_grouped",
    field: "attr:latency_ms",
    group_field: "attr:user",
    total: 100,
    min: 0,
    max: 100,
    distinct_groups: 5,
    omitted_groups: 3,
    omitted_count: 30,
    groups: [
      {
        value: "alice",
        count: 40,
        min: 0,
        max: 90,
        mean: 25,
        stddev: 8,
        skewness: 0.3,
        quantiles: { "0.25": 10, "0.5": 20, "0.75": 30 },
        bins: [
          { x0: 0, x1: 50, count: 35 },
          { x0: 50, x1: 100, count: 5 },
        ],
      },
      {
        value: "bob",
        count: 30,
        min: 5,
        max: 100,
        mean: 50,
        stddev: 9,
        skewness: -0.2,
        quantiles: { "0.25": 40, "0.5": 50, "0.75": 60 },
        bins: [
          { x0: 0, x1: 50, count: 12 },
          { x0: 50, x1: 100, count: 18 },
        ],
      },
    ],
    points: {
      total: 70,
      shown: 4,
      values: [
        ["alice", 12],
        ["alice", 22],
        ["bob", 48],
        ["bob", 61],
      ],
    },
  };

  it("renders a waffle grid of exactly 100 cells", () => {
    const { container } = render(<WaffleChart terms={TERMS} />);
    // 100 cells + the legend swatches, so count only the SVG rects.
    expect(container.querySelectorAll("svg rect")).toHaveLength(100);
  });

  it("renders grouped box and violin marks with a point overlay", () => {
    for (const mark of ["box", "violin"] as const) {
      const { container } = render(
        <GroupedDistribution data={GROUPED} mark={mark} showPoints />,
      );
      expect(container.querySelector("svg")).toBeTruthy();
      // One jittered dot per sampled value.
      expect(container.querySelectorAll("svg circle")).toHaveLength(4);
    }
  });

  it("grouped charts report an empty state instead of throwing", () => {
    const { container } = render(
      <GroupedDistribution
        data={{ ...GROUPED, total: 0, groups: [], points: null }}
        mark="box"
      />,
    );
    expect(container.textContent).toContain("No numeric values");
  });

  it("histogram draws density curve and mean/median markers", () => {
    const { container } = render(
      <NumericHistogram stats={NUMERIC} showDensity showMarkers />,
    );
    expect(container.querySelector("svg path")).toBeTruthy();
    expect(container.textContent).toContain("median");
    expect(container.textContent).toContain("mean");
  });

  it("scatter draws the server-computed regression line", () => {
    const { container } = render(<ScatterChart data={SCATTER} showRegression />);
    const dashed = [...container.querySelectorAll("line")].filter(
      (l) => l.getAttribute("stroke-dasharray") === "6 4",
    );
    expect(dashed).toHaveLength(1);
  });
});

describe("correlation matrix", () => {
  const CORR: FieldCorrelationResponse = {
    kind: "corr",
    fields: ["attr:bytes", "attr:latency", "attr:retries"],
    total: 1000,
    numeric_counts: { "attr:bytes": 1000, "attr:latency": 900, "attr:retries": 0 },
    pairs: [
      {
        x: "attr:bytes",
        y: "attr:latency",
        n: 900,
        pearson: 0.82,
        p_pearson: 1e-9,
        spearman: 0.75,
        p_spearman: 1e-8,
      },
      {
        x: "attr:bytes",
        y: "attr:retries",
        n: 0,
        pearson: null,
        p_pearson: null,
        spearman: null,
        p_spearman: null,
      },
      {
        x: "attr:latency",
        y: "attr:retries",
        n: 0,
        pearson: null,
        p_pearson: null,
        spearman: null,
        p_spearman: null,
      },
    ],
    dropped_fields: [{ field: "attr:retries", reason: "non_numeric" }],
  };

  it("draws only the lower triangle and prints each coefficient", () => {
    const { container } = render(<CorrMatrix data={CORR} />);
    // 3 fields -> 3 lower-triangle cells (the diagonal is never drawn).
    expect(container.querySelectorAll("svg rect")).toHaveLength(3);
    expect(container.textContent).toContain("+0.82");
    // A pair with no shared numeric events renders as an explicit gap.
    expect(container.textContent).toContain("—");
  });

  it("switches the filled coefficient without refetching", () => {
    const { container } = render(<CorrMatrix data={CORR} method="spearman" />);
    expect(container.textContent).toContain("+0.75");
    expect(container.textContent).toContain("Spearman");
  });

  it("reports an empty state when there is nothing to correlate", () => {
    const { container } = render(
      <CorrMatrix data={{ ...CORR, fields: ["attr:bytes"], pairs: [] }} />,
    );
    expect(container.textContent).toContain("No field pairs");
  });

  it("opens a pair as a scatter plot on click", () => {
    const opened: [string, string][] = [];
    const { container } = render(
      <CorrMatrix data={CORR} onPairClick={(x, y) => opened.push([x, y])} />,
    );
    // The first cell is the (bytes, latency) pair; click its group, not the
    // frame's margin <g>.
    fireEvent.click(container.querySelector("svg rect")!.parentElement!);
    expect(opened).toEqual([["attr:bytes", "attr:latency"]]);
  });
});

describe("FacetGrid", () => {
  it("draws one panel per value and states what was left out", () => {
    render(
      <FacetGrid
        field="attr:status"
        omittedValues={3}
        omittedCount={120}
        panels={[
          { value: "200", count: 90, isLoading: false, chart: <div>panel-200</div> },
          { value: "500", count: 10, isLoading: false, chart: <div>panel-500</div> },
        ]}
      />,
    );
    expect(screen.getByText("panel-200")).toBeTruthy();
    expect(screen.getByText("panel-500")).toBeTruthy();
    expect(screen.getByText(/3 further values/)).toBeTruthy();
    expect(screen.getByText(/not merged into an "Other" panel/)).toBeTruthy();
  });

  it("says so when no value matches instead of drawing an empty grid", () => {
    render(<FacetGrid field="attr:status" panels={[]} />);
    expect(screen.getByText(/No values of/)).toBeTruthy();
  });
});
