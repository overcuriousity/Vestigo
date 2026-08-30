/**
 * SVG/PNG chart export.
 *
 * Charts render as real `<svg>`, so SVG export is a straight serialization
 * and PNG export is a canvas redraw at `width * scale` — the resolution
 * knob `ExportControls` exposes. Every export appends a small caption block
 * (case/timeline/field/filters/bin params) so the image stays
 * self-describing outside the app, per the forensic-reproducibility goal in
 * CLAUDE.md.
 *
 * CSS custom properties (`var(--viz-series-1)`, etc.) only resolve while the
 * SVG is attached to this document — a rasterized/standalone copy has no
 * access to `index.css`'s `[data-theme]` rules, so every `var(--x)` is
 * inlined to its live computed value before serialization.
 *
 * PNG is additionally bounded by what a browser canvas can be: a horizontal
 * bar chart sizes itself to its row count, so at the 500-value ceiling the
 * `<svg>` is already taller than any canvas may be, and the requested scale
 * is lowered to fit rather than handing back a failed (or blank) export.
 * SVG has no such limit and is never downscaled.
 */

import { triggerDownload } from "@/lib/download";

const SVG_NS = "http://www.w3.org/2000/svg";

function resolveElementSize(svg: SVGSVGElement): { width: number; height: number } {
  // `ChartFrame` always sets plain numeric `width`/`height` attributes, so
  // prefer those; `baseVal` (SVGAnimatedLength) is a fallback for a
  // differently-sourced svg and isn't implemented in jsdom (test env).
  const width = parseInt(svg.getAttribute("width") || "0", 10) || svg.width?.baseVal?.value || 0;
  const height = parseInt(svg.getAttribute("height") || "0", 10) || svg.height?.baseVal?.value || 0;
  return { width, height };
}

/** Clone *svg*, append an opaque background + caption text block, and resize
 * the viewBox/height to fit it. Returns the clone and its final dimensions. */
function cloneWithCaption(
  svg: SVGSVGElement,
  captionLines: string[],
): { svgEl: SVGSVGElement; width: number; height: number } {
  const { width, height } = resolveElementSize(svg);
  const clone = svg.cloneNode(true) as SVGSVGElement;

  const lineHeight = 13;
  const padTop = 10;
  const captionHeight = captionLines.length > 0 ? padTop + captionLines.length * lineHeight + 4 : 0;
  const totalHeight = height + captionHeight;

  clone.setAttribute("width", String(width));
  clone.setAttribute("height", String(totalHeight));
  clone.setAttribute("viewBox", `0 0 ${width} ${totalHeight}`);
  clone.setAttribute("xmlns", SVG_NS);

  // Opaque background — otherwise a rasterized PNG has a transparent chart
  // area, which reads as broken when pasted into a report.
  const bg = document.createElementNS(SVG_NS, "rect");
  bg.setAttribute("x", "0");
  bg.setAttribute("y", "0");
  bg.setAttribute("width", String(width));
  bg.setAttribute("height", String(totalHeight));
  bg.setAttribute("fill", "var(--color-bg-elevated)");
  clone.insertBefore(bg, clone.firstChild);

  if (captionLines.length > 0) {
    const g = document.createElementNS(SVG_NS, "g");
    g.setAttribute("transform", `translate(8, ${height + padTop})`);
    captionLines.forEach((line, i) => {
      const t = document.createElementNS(SVG_NS, "text");
      t.setAttribute("x", "0");
      t.setAttribute("y", String(i * lineHeight + 9));
      t.setAttribute("font-size", "9.5");
      t.setAttribute("font-family", "ui-monospace, monospace");
      t.setAttribute("fill", "var(--color-fg-muted)");
      t.textContent = line;
      g.appendChild(t);
    });
    clone.appendChild(g);
  }

  return { svgEl: clone, width, height: totalHeight };
}

/** Replace every `var(--x)` occurrence in a serialized SVG string with its
 * current computed value, so the export renders correctly detached from the
 * app's stylesheet (rasterization, opening the file standalone, etc.). */
function inlineCssVars(svgString: string): string {
  const varNames = new Set<string>();
  const re = /var\((--[\w-]+)\)/g;
  let match: RegExpExecArray | null;
  while ((match = re.exec(svgString))) varNames.add(match[1]);
  if (varNames.size === 0) return svgString;

  const computed = getComputedStyle(document.documentElement);
  let out = svgString;
  for (const name of varNames) {
    const value = computed.getPropertyValue(name).trim();
    if (value) out = out.split(`var(${name})`).join(value);
  }
  return out;
}

function serialize(svg: SVGSVGElement): string {
  const raw = new XMLSerializer().serializeToString(svg);
  return inlineCssVars(raw);
}

function withExt(filename: string, ext: string): string {
  return filename.toLowerCase().endsWith(`.${ext}`) ? filename : `${filename}.${ext}`;
}

export function downloadChartSvg(
  svg: SVGSVGElement,
  filename: string,
  captionLines: string[] = [],
): void {
  const { svgEl } = cloneWithCaption(svg, captionLines);
  const svgString = serialize(svgEl);
  const blob = new Blob([svgString], { type: "image/svg+xml" });
  triggerDownload(blob, withExt(filename, "svg"));
}

/** A text file the figure already holds (the table's CSV) — no SVG involved. */
export function downloadCsv(text: string, filename: string): void {
  const blob = new Blob([text], { type: "text/csv;charset=utf-8" });
  triggerDownload(blob, withExt(filename, "csv"));
}

/**
 * The largest side, and the largest area, a `<canvas>` may have.
 *
 * Both are browser limits, not ours, and every engine we target sits at or
 * above these: Chrome and Firefox refuse a dimension past 16,384px, and
 * Chrome caps the total bitmap at 2^28 pixels. Past either, `toBlob` yields
 * `null` — or, worse on some builds, a blank bitmap — so an unclamped export
 * fails with "PNG export failed" on exactly the charts a high `topN` makes
 * reachable (a 500-row horizontal bar is ~13,000px tall before the caption,
 * and ~21,000px in compare mode, so even 1× overruns it).
 */
export const MAX_CANVAS_DIM = 16384;
export const MAX_CANVAS_AREA = 268_435_456;

/**
 * The scale a chart this size can actually be rasterized at.
 *
 * Never raises the requested scale, and may drop below 1× for a chart that
 * exceeds a canvas at its natural size — a downscaled PNG is a worse image
 * than the analyst asked for, but it is an image, and `ExportControls`
 * discloses the number it was lowered to. For a very tall chart, SVG is the
 * lossless way out.
 */
export function effectiveExportScale(width: number, height: number, scale: number): number {
  if (!(width > 0) || !(height > 0)) return scale;
  return Math.min(
    scale,
    MAX_CANVAS_DIM / width,
    MAX_CANVAS_DIM / height,
    Math.sqrt(MAX_CANVAS_AREA / (width * height)),
  );
}

function loadImage(url: string): Promise<HTMLImageElement> {
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => resolve(img);
    img.onerror = () => reject(new Error("Failed to rasterize chart SVG"));
    img.src = url;
  });
}

/**
 * Rasterize the chart to a PNG at `width * scale` resolution — `scale` is
 * the export-resolution knob surfaced in `ExportControls` (e.g. 1x/2x/3x/4x).
 *
 * Returns the scale actually used, which is the requested one unless the
 * canvas limits forced it down (`effectiveExportScale`). The caller discloses
 * the difference; silently shipping a lower-resolution image than the picker
 * reads would make the knob lie.
 */
export async function downloadChartPng(
  svg: SVGSVGElement,
  filename: string,
  scale: number,
  captionLines: string[] = [],
): Promise<number> {
  const { svgEl, width, height } = cloneWithCaption(svg, captionLines);
  const usedScale = effectiveExportScale(width, height, scale);
  const svgString = serialize(svgEl);
  const svgBlob = new Blob([svgString], { type: "image/svg+xml" });
  const url = URL.createObjectURL(svgBlob);
  try {
    const img = await loadImage(url);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * usedScale));
    canvas.height = Math.max(1, Math.round(height * usedScale));
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Canvas 2D context unavailable");
    ctx.scale(usedScale, usedScale);
    ctx.drawImage(img, 0, 0, width, height);
    const pngBlob = await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("PNG export failed"))), "image/png");
    });
    triggerDownload(pngBlob, withExt(filename, "png"));
    return usedScale;
  } finally {
    URL.revokeObjectURL(url);
  }
}
