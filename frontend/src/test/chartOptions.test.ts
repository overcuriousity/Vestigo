/**
 * resolveChartOptions — the one place a ChartConfig's optional knobs become
 * concrete. Shared by the Visualize page and the agent's ChartProposalCard so
 * an agent-proposed chart and a hand-built one are the same chart; before it
 * existed the two applied different defaults.
 */
import { describe, expect, it } from "vitest";
import { DEFAULT_CHART_CONFIG, type ChartConfig } from "@/components/viz/lib/chartConfig";
import { resolveChartOptions } from "@/components/viz/lib/chartOptions";

const config = (patch: Partial<ChartConfig>): ChartConfig => ({
  ...DEFAULT_CHART_CONFIG,
  ...patch,
});

describe("resolveChartOptions", () => {
  it("fills every option with the analyst-facing default", () => {
    expect(resolveChartOptions(config({ chartType: "bar" }))).toEqual({
      topN: 10,
      bins: 30,
      buckets: 60,
      limitX: 10,
      limitY: 10,
      sampleLimit: 5000,
      orientation: "horizontal",
      sort: "count",
      logScale: false,
      seriesMode: "overlay",
      legend: true,
    });
  });

  it("passes explicit values through", () => {
    const resolved = resolveChartOptions(
      config({ chartType: "bar", options: { topN: 25, logScale: true, sort: "value" } }),
    );
    expect(resolved.topN).toBe(25);
    expect(resolved.logScale).toBe(true);
    expect(resolved.sort).toBe("value");
  });

  it("caps topN lower for value-over-time charts than for a bar axis", () => {
    // One line per value, so a timeseries caps at 20 where a bar caps at 50.
    expect(resolveChartOptions(config({ chartType: "line", options: { topN: 999 } })).topN).toBe(20);
    expect(
      resolveChartOptions(config({ chartType: "heatmap", options: { topN: 999 } })).topN,
    ).toBe(20);
    expect(resolveChartOptions(config({ chartType: "bar", options: { topN: 999 } })).topN).toBe(50);
  });

  it("keeps a legend explicitly turned off, rather than treating false as unset", () => {
    expect(resolveChartOptions(config({ chartType: "line", options: { legend: false } })).legend).toBe(
      false,
    );
  });

  it("keeps an explicit zero", () => {
    expect(resolveChartOptions(config({ chartType: "bar", options: { topN: 0 } })).topN).toBe(0);
  });
});
