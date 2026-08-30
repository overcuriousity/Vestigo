import { describe, expect, it } from "vitest";
import { SCALE_DISPLAY, scaleTooltip, treatAsNotice } from "@/components/viz/lib/scaleDisplay";
import { SCALES } from "@/components/viz/lib/chartMeta";

describe("treat-as display", () => {
  it("labels every scale in plain language, with the Stevens term only in the tooltip", () => {
    for (const s of SCALES) {
      const d = SCALE_DISPLAY[s];
      expect(d.label).not.toMatch(/nominal|ordinal|interval|ratio/i);
      expect(scaleTooltip(s)).toContain(d.stevens);
      expect(scaleTooltip(s)).toContain(d.examples);
    }
    expect(SCALE_DISPLAY.nominal.label).toBe("Categories");
    expect(SCALE_DISPLAY.ordinal.label).toBe("Ordered categories");
    expect(SCALE_DISPLAY.interval.label).toBe("Number or time");
    expect(SCALE_DISPLAY.ratio.label).toBe("Measure");
  });

  it("phrases the probe's pre-selection as a suggestion the analyst may overrule", () => {
    expect(treatAsNotice("src_port", "ratio", true)).toBe(
      "src_port looks numeric — treating it as a measure; change this if its values are categories to you.",
    );
    expect(treatAsNotice("artifact", "nominal", false)).toBe(
      "artifact has no numeric values — treating it as categories.",
    );
  });
});
