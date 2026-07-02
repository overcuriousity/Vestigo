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
 */

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

function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
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
 */
export async function downloadChartPng(
  svg: SVGSVGElement,
  filename: string,
  scale: number,
  captionLines: string[] = [],
): Promise<void> {
  const { svgEl, width, height } = cloneWithCaption(svg, captionLines);
  const svgString = serialize(svgEl);
  const svgBlob = new Blob([svgString], { type: "image/svg+xml" });
  const url = URL.createObjectURL(svgBlob);
  try {
    const img = await loadImage(url);
    const canvas = document.createElement("canvas");
    canvas.width = Math.max(1, Math.round(width * scale));
    canvas.height = Math.max(1, Math.round(height * scale));
    const ctx = canvas.getContext("2d");
    if (!ctx) throw new Error("Canvas 2D context unavailable");
    ctx.scale(scale, scale);
    ctx.drawImage(img, 0, 0, width, height);
    const pngBlob = await new Promise<Blob>((resolve, reject) => {
      canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("PNG export failed"))), "image/png");
    });
    triggerDownload(pngBlob, withExt(filename, "png"));
  } finally {
    URL.revokeObjectURL(url);
  }
}
