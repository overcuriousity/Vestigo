import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import {
  downloadChartSvg,
  effectiveExportScale,
  MAX_CANVAS_AREA,
  MAX_CANVAS_DIM,
} from "@/components/viz/lib/export";

const SVG_NS = "http://www.w3.org/2000/svg";

function makeSvg(): SVGSVGElement {
  const svg = document.createElementNS(SVG_NS, "svg") as SVGSVGElement;
  svg.setAttribute("width", "200");
  svg.setAttribute("height", "100");
  const rect = document.createElementNS(SVG_NS, "rect");
  rect.setAttribute("fill", "var(--test-color)");
  svg.appendChild(rect);
  document.body.appendChild(svg);
  return svg;
}

describe("downloadChartSvg", () => {
  let capturedBlob: Blob | null;
  let originalCreateObjectURL: typeof URL.createObjectURL;
  let originalRevokeObjectURL: typeof URL.revokeObjectURL;

  beforeEach(() => {
    capturedBlob = null;
    originalCreateObjectURL = URL.createObjectURL;
    originalRevokeObjectURL = URL.revokeObjectURL;
    URL.createObjectURL = vi.fn((blob: Blob) => {
      capturedBlob = blob;
      return "blob:mock-url";
    }) as typeof URL.createObjectURL;
    URL.revokeObjectURL = vi.fn() as typeof URL.revokeObjectURL;
    document.documentElement.style.setProperty("--test-color", "#123456");
  });

  afterEach(() => {
    URL.createObjectURL = originalCreateObjectURL;
    URL.revokeObjectURL = originalRevokeObjectURL;
    document.documentElement.style.removeProperty("--test-color");
    document.body.innerHTML = "";
  });

  it("inlines CSS custom properties to their live computed value", async () => {
    const svg = makeSvg();
    downloadChartSvg(svg, "chart");
    expect(capturedBlob).not.toBeNull();
    const text = await capturedBlob!.text();
    expect(text).toContain("#123456");
    expect(text).not.toContain("var(--test-color)");
  });

  it("produces an image/svg+xml blob", () => {
    const svg = makeSvg();
    downloadChartSvg(svg, "chart");
    expect(capturedBlob!.type).toBe("image/svg+xml");
  });

  it("appends a caption line as visible text when provided", async () => {
    const svg = makeSvg();
    downloadChartSvg(svg, "chart", ["case abc123 / field: status_code"]);
    const text = await capturedBlob!.text();
    expect(text).toContain("case abc123 / field: status_code");
  });

  it("resizes the viewBox/height to include the caption block", async () => {
    const svg = makeSvg();
    downloadChartSvg(svg, "chart", ["one line of caption"]);
    const text = await capturedBlob!.text();
    const heightMatch = text.match(/height="(\d+)"/);
    expect(heightMatch).not.toBeNull();
    expect(Number(heightMatch![1])).toBeGreaterThan(100);
  });
});

/**
 * A horizontal bar chart sizes itself to its row count, so the 500-value
 * ceiling (#296) puts the `<svg>` past what any canvas may be — long before
 * the resolution picker multiplies it. An unclamped export failed with "PNG
 * export failed" on exactly those charts.
 */
describe("effectiveExportScale", () => {
  it("leaves an ordinary chart at the requested resolution", () => {
    expect(effectiveExportScale(800, 400, 4)).toBe(4);
  });

  it("never raises the requested resolution", () => {
    expect(effectiveExportScale(10, 10, 1)).toBe(1);
  });

  it("keeps a tall chart inside the canvas dimension limit", () => {
    // 500 rows x 26px + 40 = 13,040px tall: already 26,080px at the default 2x.
    const scale = effectiveExportScale(900, 13040, 2);
    expect(scale).toBeLessThan(2);
    expect(13040 * scale).toBeLessThanOrEqual(MAX_CANVAS_DIM);
  });

  it("drops below 1x for a chart that overruns a canvas at its natural size", () => {
    // Compare mode gives each row 41.6px: 500 rows is ~21,000px, past the
    // limit before any multiplier at all.
    const scale = effectiveExportScale(900, 20840, 1);
    expect(scale).toBeLessThan(1);
    expect(20840 * scale).toBeLessThanOrEqual(MAX_CANVAS_DIM);
  });

  it("respects the total-area limit, not only the longest side", () => {
    const w = 12000;
    const h = 12000;
    const scale = effectiveExportScale(w, h, 1);
    expect(w * scale * h * scale).toBeLessThanOrEqual(MAX_CANVAS_AREA + 1);
  });

  it("passes a degenerate size through rather than dividing by zero", () => {
    expect(effectiveExportScale(0, 0, 3)).toBe(3);
  });
});
