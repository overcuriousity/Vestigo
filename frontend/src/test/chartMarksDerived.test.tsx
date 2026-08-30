/**
 * `ChartMarks` is the shared dispatch behind a Story snapshot, the HTML export
 * and the agent's proposal card. It has to hand each figure the same props the
 * Visualize page does, or the frozen renderings disagree with the live one.
 */
import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ChartMarks, type ChartResult } from "@/components/viz/ChartCanvas";
import { DEFAULT_CHART_CONFIG } from "@/components/viz/lib/chartConfig";
import { resolveChartOptions } from "@/components/viz/lib/chartOptions";
import { installFakeResizeObserver } from "./helpers/resizeObserver";

installFakeResizeObserver();

// Log-spaced ranges: "≥ 10,240" sorts before "< 1,024" as a string, because
// "<" and "≥" both sort after every digit. Value order is the only order.
const LABELS = ["< 1,024", "1,024 – 10,240", "≥ 10,240"];

const derivedTerms = {
  kind: "terms" as const,
  field: "attr:bytes",
  total: 60,
  distinct: 3,
  other_count: 0,
  values: [
    { value: "≥ 10,240", count: 30 },
    { value: "< 1,024", count: 20 },
    { value: "1,024 – 10,240", count: 10 },
  ],
  derive: { kind: "bins", labels: LABELS },
};

describe("ChartMarks — derived bar charts", () => {
  it("orders a derived bar chart by value order, not lexically", () => {
    // `applyDerive` sets `sort: "value"` on every derived bar chart, so this
    // is the default path. Without `valueOrder` the snapshot, the export and
    // the agent card put "≥ 10,240" first while the live page did not (#332).
    const config = {
      ...DEFAULT_CHART_CONFIG,
      chartType: "bar" as const,
      field: "attr:bytes",
      options: { ...DEFAULT_CHART_CONFIG.options, sort: "value" as const },
    };
    const { container } = render(
      <ChartMarks
        config={config}
        data={{ kind: "terms", data: derivedTerms, compare: false } as unknown as ChartResult}
        opts={resolveChartOptions(config)}
        compareOn={false}
      />,
    );
    const drawn = [...container.querySelectorAll("text")]
      .map((t) => t.textContent ?? "")
      .filter((t) => LABELS.includes(t));
    expect(drawn).toEqual(LABELS);
  });
});
