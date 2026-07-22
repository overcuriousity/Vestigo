/**
 * The teaching copy is only useful if it is complete and actually reachable:
 * every chart type needs a "how to read this", every explainer id referenced
 * by a component must exist, and no entry may quietly lose a section.
 */
import { describe, it, expect } from "vitest";
import {
  CHART_HOW_TO_READ,
  EXPLAINERS,
  type ExplainerId,
} from "@/components/viz/lib/explainers";
import { CHART_META } from "@/components/viz/lib/chartMeta";

// Vite's raw glob rather than node:fs — the frontend tsconfig carries no node
// types, and this keeps the scan inside the bundler's module graph.
const VIZ_SOURCES = {
  ...(import.meta.glob("../components/viz/**/*.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
  ...(import.meta.glob("../pages/VisualizePage.tsx", {
    query: "?raw",
    import: "default",
    eager: true,
  }) as Record<string, string>),
};

describe("explainer copy", () => {
  it("covers every chart type with a how-to-read line", () => {
    expect(Object.keys(CHART_HOW_TO_READ).sort()).toEqual(Object.keys(CHART_META).sort());
    for (const [chartType, line] of Object.entries(CHART_HOW_TO_READ)) {
      expect(line.length, chartType).toBeGreaterThan(20);
    }
  });

  it("gives every explainer all three sections", () => {
    for (const [id, explainer] of Object.entries(EXPLAINERS)) {
      expect(explainer.title, id).toBeTruthy();
      expect(explainer.what, id).toBeTruthy();
      expect(explainer.howToRead, id).toBeTruthy();
      // "When to distrust it" is the section that keeps these honest — a
      // statistic explained without its failure mode teaches overconfidence.
      expect(explainer.distrust, id).toBeTruthy();
    }
  });

  it("has an entry for every id a component asks for", () => {
    const used = new Set<string>();
    for (const source of Object.values(VIZ_SOURCES)) {
      for (const match of source.matchAll(/ExplainerPopover\s+id="([^"]+)"/g)) {
        used.add(match[1]);
      }
    }
    expect(used.size).toBeGreaterThan(0);
    for (const id of used) {
      expect(EXPLAINERS[id as ExplainerId], `${id} is used but has no copy`).toBeTruthy();
    }
  });
});
