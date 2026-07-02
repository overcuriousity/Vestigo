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
import { render } from "@testing-library/react";
import { BarChart } from "@/components/viz/charts/BarChart";
import { PieChart } from "@/components/viz/charts/PieChart";
import { NumericHistogram } from "@/components/viz/charts/NumericHistogram";
import { BoxPlot } from "@/components/viz/charts/BoxPlot";
import { ViolinPlot } from "@/components/viz/charts/ViolinPlot";
import { LineChart } from "@/components/viz/charts/LineChart";
import { Heatmap } from "@/components/viz/charts/Heatmap";
import { EcdfChart } from "@/components/viz/charts/EcdfChart";
import { TimeHistogram } from "@/components/viz/charts/TimeHistogram";
import type {
  FieldNumericResponse,
  FieldTermsResponse,
  FieldTimeseriesResponse,
  HistogramBucket,
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

function expectSvg(container: HTMLElement) {
  expect(container.querySelector("svg")).not.toBeNull();
}

describe("chart smoke render", () => {
  it("BarChart renders with terms data", () => {
    const { container } = render(<BarChart terms={TERMS} />);
    expectSvg(container);
  });
  it("BarChart renders empty state without an svg", () => {
    const { container, getByText } = render(
      <BarChart terms={{ ...TERMS, values: [], other_count: 0 }} />,
    );
    expect(container.querySelector("svg")).toBeNull();
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
    const { container } = render(
      <NumericHistogram stats={{ ...NUMERIC, count: 0, min: null, max: null, bins: [] }} />,
    );
    expect(container.querySelector("svg")).toBeNull();
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
  it("TimeHistogram renders empty state without an svg", () => {
    const { container } = render(<TimeHistogram buckets={[]} />);
    expect(container.querySelector("svg")).toBeNull();
  });
});
