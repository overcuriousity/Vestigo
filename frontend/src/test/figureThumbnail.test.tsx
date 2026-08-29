import { describe, expect, it } from "vitest";
import { render } from "@testing-library/react";
import { FigureThumbnail, THUMBNAILS } from "@/components/viz/primitives/FigureThumbnail";
import { CHART_META } from "@/components/viz/lib/chartMeta";
import type { ChartType } from "@/components/viz/lib/chartConfig";

describe("FigureThumbnail", () => {
  it("has a glyph for every figure in the registry", () => {
    for (const c of Object.keys(CHART_META) as ChartType[]) {
      expect(THUMBNAILS[c], c).toBeTypeOf("function");
      const { container } = render(<FigureThumbnail chartType={c} />);
      const svg = container.querySelector("svg");
      expect(svg).not.toBeNull();
      expect(svg?.getAttribute("aria-hidden")).toBe("true");
    }
  });
});
