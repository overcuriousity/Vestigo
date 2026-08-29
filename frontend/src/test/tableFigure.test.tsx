import { beforeAll, describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { TableFigure, TableHtml } from "@/components/viz/charts/TableFigure";
import { DEFAULT_CHART_CONFIG, type ChartConfig } from "@/components/viz/lib/chartConfig";
import type { FieldTableResponse } from "@/api/types";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

beforeAll(() => installFakeResizeObserver());

const data: FieldTableResponse = {
  kind: "table",
  field: "time:day_of_week",
  second_field: null,
  total: 10,
  distinct: 3,
  rows: [
    {
      value: "1",
      count: 6,
      share: 0.6,
      first_seen: "2026-07-20T01:00:00+00:00",
      last_seen: "2026-07-21T05:00:00+00:00",
      distinct_second: null,
    },
    { value: "3", count: 3, share: 0.3, first_seen: null, last_seen: null, distinct_second: null },
  ],
  remainder: { count: 1, share: 0.1, distinct_values: 1 },
  sort: { by: "count", dir: "desc" },
};
const config: ChartConfig = {
  ...DEFAULT_CHART_CONFIG,
  chartType: "table",
  field: "time:day_of_week",
  scale: "ordinal",
};

describe("TableFigure (SVG)", () => {
  it("renders an <svg> with a header, one row per value, labelled time values and the remainder", () => {
    const { container } = render(<TableFigure data={data} config={config} highlight={["3"]} />);
    expect(container.querySelector("svg")).not.toBeNull();
    const texts = [...container.querySelectorAll("text")].map((t) => t.textContent);
    expect(texts).toContain("Mon");
    expect(texts).toContain("Wed");
    expect(texts).toContain("60.0%");
    expect(texts).toContain("Remainder (1 more value)");
    // The in-cell bar encodes count only: one <rect> per non-remainder row beyond the highlight band.
    expect(container.querySelectorAll("rect[data-cell-bar]").length).toBe(2);
    expect(container.querySelectorAll("rect[data-highlight-band]").length).toBe(1);
  });
});

describe("TableHtml", () => {
  it("is a real table with headers, highlighted and remainder rows marked", () => {
    render(<TableHtml data={data} config={config} highlight={["3"]} />);
    const table = screen.getByTestId("table-figure-html");
    expect(table.tagName).toBe("TABLE");
    const headers = [...table.querySelectorAll("th")].map((h) => h.textContent);
    expect(headers).toEqual(["Day of week (UTC)", "count", "share", "first seen", "last seen"]);
    const rows = [...table.querySelectorAll("tbody tr")];
    expect(rows).toHaveLength(3);
    expect(rows[1].getAttribute("data-highlighted")).toBe("true");
    expect(rows[2].getAttribute("data-remainder")).toBe("true");
    expect(rows[0].textContent).toContain("Mon");
  });
});
