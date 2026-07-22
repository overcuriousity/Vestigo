/**
 * The box and violin marks themselves, factored out of `BoxPlot`/`ViolinPlot`
 * so the ungrouped charts and the grouped (numeric × categorical) variant draw
 * identical geometry from identical inputs. Each mark takes a y-scale it does
 * not own — in grouped mode every group shares one scale, which is what makes
 * the groups comparable.
 */
import { area as d3area, curveBasis } from "d3-shape";
import { scaleLinear, type ScaleLinear } from "d3-scale";
import { kdeFromBins, boxPlotStats, type DistributionLike } from "@/components/viz/lib/stats";
import { jitterOffset } from "@/components/viz/lib/jitter";

export interface MarkHoverHandlers {
  onHover: (label: string, atY: number) => void;
  onLeave: () => void;
}

export function BoxMark({
  dist,
  cx,
  width,
  y,
  color,
  hover,
  fmt,
}: {
  dist: DistributionLike;
  cx: number;
  width: number;
  y: ScaleLinear<number, number>;
  color: string;
  hover?: MarkHoverHandlers;
  fmt: (v: number) => string;
}) {
  const box = boxPlotStats(dist);
  if (!box) return null;
  const boxTop = y(box.q3);
  const boxBottom = y(box.q1);
  const on = (label: string, value: number, atY: number) =>
    hover
      ? {
          onMouseEnter: () => hover.onHover(`${label}: ${fmt(value)}`, atY),
          onMouseLeave: hover.onLeave,
        }
      : {};

  return (
    <>
      <line
        x1={cx}
        x2={cx}
        y1={y(box.whiskerHigh)}
        y2={boxTop}
        stroke="var(--viz-axis)"
        strokeWidth={1.5}
      />
      <line
        x1={cx}
        x2={cx}
        y1={boxBottom}
        y2={y(box.whiskerLow)}
        stroke="var(--viz-axis)"
        strokeWidth={1.5}
      />
      <line
        x1={cx - width / 4}
        x2={cx + width / 4}
        y1={y(box.whiskerHigh)}
        y2={y(box.whiskerHigh)}
        stroke="var(--viz-axis)"
        strokeWidth={1.5}
        {...on("Upper whisker", box.whiskerHigh, y(box.whiskerHigh))}
      />
      <line
        x1={cx - width / 4}
        x2={cx + width / 4}
        y1={y(box.whiskerLow)}
        y2={y(box.whiskerLow)}
        stroke="var(--viz-axis)"
        strokeWidth={1.5}
        {...on("Lower whisker", box.whiskerLow, y(box.whiskerLow))}
      />
      <rect
        x={cx - width / 2}
        y={boxTop}
        width={width}
        height={Math.max(1, boxBottom - boxTop)}
        fill={color}
        fillOpacity={0.25}
        stroke={color}
        strokeWidth={1.5}
        {...on("Q1–Q3", box.q1, (boxTop + boxBottom) / 2)}
      />
      <line
        x1={cx - width / 2}
        x2={cx + width / 2}
        y1={y(box.median)}
        y2={y(box.median)}
        stroke={color}
        strokeWidth={2.5}
        {...on("Median", box.median, y(box.median))}
      />
    </>
  );
}

export function ViolinMark({
  dist,
  cx,
  halfWidth,
  y,
  color,
  /** Density normalizer — pass a shared value across groups so a wide violin
   * really does mean "more events here" and not just "this group's own peak". */
  maxDensity,
}: {
  dist: DistributionLike;
  cx: number;
  halfWidth: number;
  y: ScaleLinear<number, number>;
  color: string;
  maxDensity?: number;
}) {
  const density = kdeFromBins(dist.bins);
  if (density.length === 0) return null;
  const peak = maxDensity ?? Math.max(1e-9, ...density.map((d) => d.density));
  const w = scaleLinear().domain([0, peak]).range([0, halfWidth]);

  const side = (sign: number) =>
    d3area<{ x: number; density: number }>()
      .curve(curveBasis)
      .x0(cx)
      .x1((d) => cx + sign * w(d.density))
      .y((d) => y(d.x))(density) ?? undefined;

  const median = dist.quantiles["0.5"];
  return (
    <>
      <path d={side(1)} fill={color} fillOpacity={0.35} stroke={color} strokeWidth={1} />
      <path d={side(-1)} fill={color} fillOpacity={0.35} stroke={color} strokeWidth={1} />
      {median != null && (
        <line
          x1={cx - 12}
          x2={cx + 12}
          y1={y(median)}
          y2={y(median)}
          stroke="var(--viz-ink-primary)"
          strokeWidth={2}
        />
      )}
    </>
  );
}

/** Jittered strip of raw sampled values over a box/violin mark. */
export function PointStrip({
  values,
  cx,
  spread,
  y,
  seed = 0,
}: {
  values: number[];
  cx: number;
  spread: number;
  y: ScaleLinear<number, number>;
  seed?: number;
}) {
  return (
    <>
      {values.map((v, i) => (
        <circle
          key={i}
          cx={cx + jitterOffset(seed + i) * spread}
          cy={y(v)}
          r={1.6}
          fill="var(--viz-ink-primary)"
          fillOpacity={0.45}
          pointerEvents="none"
        />
      ))}
    </>
  );
}
