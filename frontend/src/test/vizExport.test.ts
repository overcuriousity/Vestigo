import { describe, it, expect, beforeEach, afterEach, vi } from "vitest";
import { downloadChartSvg } from "@/components/viz/lib/export";

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
