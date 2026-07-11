import { useRef, useState } from "react";
import { scaleLinear, scaleTime } from "d3-scale";
import { bisector } from "d3-array";
import { utcFormat } from "d3-time-format";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { svgLocalPoint } from "@/components/viz/lib/pointer";
import { applyMetric, METRIC_INFO, type Metric } from "@/components/viz/lib/transforms";
import type { CompareTimeResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtMetric = formatNum(",.2~f");
// utcFormat, not timeFormat — bucket starts are UTC instants (see TimeHistogram).
const fmtTick = utcFormat("%b %d %H:%M");
const fmtFull = utcFormat("%Y-%m-%d %H:%M:%S UTC");
const bisectDate = bisector((d: Date) => d).left;

/** Drags narrower than this are treated as clicks, not range selections. */
const MIN_BRUSH_PX = 5;

interface CompareHistogramProps {
  data: CompareTimeResponse;
  metric: Metric;
  /** Whether a comparison layer is active — off means `data.comparison` is
   * all zeros and only the primary series is drawn. */
  hasComparison: boolean;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** Brush-to-zoom: dragging a span reports it (snapped outward to bucket
   * boundaries) so the page can narrow the shared start/end filters. */
  onRangeSelect?: (startIso: string, endIso: string) => void;
}

/**
 * Event-count-over-time histogram with an optional comparison layer — the
 * "filtered subset against everything" chart. The primary layer renders as
 * filled vertical bars in front of the comparison layer's hollow outline
 * bars ("part of whole" reads instantly); the active metric transforms both
 * layers identically, except `ratio` which collapses them into one derived
 * percentage line. Null metric bins (first delta bin, zero-baseline ratio
 * bins) are skipped, never drawn as 0.
 *
 * Hover and brush both run on one full-plot overlay (nearest-bucket readout,
 * like the Explorer's TimelineHistogram): drag a span and release to zoom
 * the page's time range to it via `onRangeSelect`.
 */
export function CompareHistogram({
  data,
  metric,
  hasComparison,
  svgRef,
  height = 280,
  onRangeSelect,
}: CompareHistogramProps) {
  const [hover, setHover] = useState<{ x: number; y: number; index: number } | null>(null);
  const [brush, setBrush] = useState<{ x0: number; x1: number } | null>(null);
  // Drag anchor lives in a ref: it must be readable synchronously in
  // mousemove/mouseup without re-render races (TimelineHistogram's pattern).
  const dragAnchorRef = useRef<number | null>(null);
  const ref = useChartRef(svgRef);

  const buckets = data.buckets;
  if (buckets.length === 0) {
    return (
      <ChartEmptyState
        hint="Events without a usable timestamp are excluded from time-based charts. Try widening the time range, or turn off compare."
      >
        No events over time here.
      </ChartEmptyState>
    );
  }

  const primaryCounts = buckets.map((b) => b.primary);
  const comparisonCounts = buckets.map((b) => b.comparison);
  const isRatio = metric === "ratio";

  const primaryValues = applyMetric(metric, primaryCounts, {
    intervalSeconds: data.interval_seconds,
    comparison: comparisonCounts,
  });
  // Ratio is a single derived series; other metrics transform both layers
  // identically so they stay comparable.
  const comparisonValues =
    hasComparison && !isRatio
      ? applyMetric(metric, comparisonCounts, { intervalSeconds: data.interval_seconds })
      : null;

  const allValues = [
    ...primaryValues.filter((v): v is number => v != null),
    ...(comparisonValues?.filter((v): v is number => v != null) ?? []),
  ];
  const dataMin = Math.min(0, ...allValues);
  const dataMax = isRatio ? Math.max(100, ...allValues, 0) : Math.max(1, ...allValues);

  const dates = buckets.map((b) => new Date(b.start));
  const domainMax = dates.length > 1 ? dates[dates.length - 1] : dates[0];

  /** Snap a dragged [t0, t1] outward to the epoch-aligned bucket grid the
   * server used, so the zoomed range never cuts a bucket in half. */
  const snapRange = (t0: number, t1: number): [string, string] => {
    const iv = Math.max(1, data.interval_seconds) * 1000;
    const start = Math.floor(t0 / iv) * iv;
    const end = Math.ceil(t1 / iv) * iv;
    return [
      new Date(start).toISOString(),
      new Date(end > start ? end : start + iv).toISOString(),
    ];
  };

  return (
    <div className="flex flex-col gap-2">
      {/* Ratio collapses both layers into one derived series — a single
          series needs no legend (the caption carries the formula). */}
      {hasComparison && !isRatio && (
        <Legend
          entries={[
            { label: "Filtered events", color: "var(--color-accent)" },
            { label: "Comparison layer", color: "var(--color-fg-disabled)", muted: true },
          ]}
        />
      )}
      <div className="relative">
        <ChartFrame height={height} svgRef={ref}>
          {({ innerWidth, innerHeight, margin }) => {
            const x = scaleTime().domain([dates[0], domainMax]).range([0, innerWidth]);
            const y = scaleLinear().domain([dataMin, dataMax]).nice().range([innerHeight, 0]);
            const barWidth = Math.max(1, innerWidth / buckets.length - 1);
            const yZero = y(0);

            const bar = (value: number) => ({
              y: y(Math.max(0, value)),
              height: Math.abs(y(value) - yZero),
            });

            const indexAt = (px: number): number => {
              const target = x.invert(px);
              let idx = bisectDate(dates, target, 1);
              idx = Math.min(dates.length - 1, Math.max(0, idx));
              if (
                idx > 0 &&
                target.getTime() - dates[idx - 1].getTime() <
                  dates[idx].getTime() - target.getTime()
              ) {
                idx -= 1;
              }
              return idx;
            };

            const hoverAt = (px: number) => {
              const idx = indexAt(px);
              const v = primaryValues[idx];
              const anchorY = v == null ? yZero : isRatio ? y(v) : bar(v).y;
              setHover({
                x: x(dates[idx]) + barWidth / 2 + margin.left,
                y: anchorY + margin.top,
                index: idx,
              });
            };

            const endBrush = (commit: boolean) => {
              const anchor = dragAnchorRef.current;
              dragAnchorRef.current = null;
              const b = brush;
              setBrush(null);
              if (!commit || anchor == null || b == null || onRangeSelect == null) return;
              const lo = Math.min(b.x0, b.x1);
              const hi = Math.max(b.x0, b.x1);
              if (hi - lo < MIN_BRUSH_PX) return;
              const [startIso, endIso] = snapRange(
                x.invert(lo).getTime(),
                x.invert(hi).getTime(),
              );
              onRangeSelect(startIso, endIso);
            };

            return (
              <>
                <AxisLeft
                  scale={y}
                  innerWidth={innerWidth}
                  tickFormat={(v) => (isRatio ? `${fmtMetric(v)} %` : fmtMetric(v))}
                />
                <AxisBottom
                  scale={x}
                  innerWidth={innerWidth}
                  innerHeight={innerHeight}
                  tickFormat={(v) => fmtTick(v as Date)}
                />
                {dataMin < 0 && (
                  <line
                    x1={0}
                    x2={innerWidth}
                    y1={yZero}
                    y2={yZero}
                    stroke="var(--viz-axis)"
                    strokeWidth={1}
                  />
                )}
                {comparisonValues?.map((v, i) => {
                  if (v == null) return null;
                  const { y: by, height: bh } = bar(v);
                  return (
                    <rect
                      key={`cmp-${i}`}
                      x={x(dates[i])}
                      y={by}
                      width={barWidth}
                      height={bh}
                      fill="none"
                      stroke="var(--color-fg-disabled)"
                      strokeWidth={1}
                    />
                  );
                })}
                {!isRatio &&
                  primaryValues.map((v, i) => {
                    if (v == null) return null;
                    const { y: by, height: bh } = bar(v);
                    return (
                      <rect
                        key={i}
                        x={x(dates[i])}
                        y={by}
                        width={barWidth}
                        height={bh}
                        fill="var(--color-accent)"
                        opacity={0.9}
                      />
                    );
                  })}
                {isRatio && (
                  <RatioLine values={primaryValues} dates={dates} x={x} y={y} barWidth={barWidth} />
                )}
                {hover != null && brush == null && (
                  <line
                    x1={x(dates[hover.index]) + barWidth / 2}
                    x2={x(dates[hover.index]) + barWidth / 2}
                    y1={0}
                    y2={innerHeight}
                    stroke="var(--viz-axis)"
                    strokeWidth={1}
                    strokeDasharray="2,2"
                  />
                )}
                {brush != null && (
                  <rect
                    x={Math.min(brush.x0, brush.x1)}
                    y={0}
                    width={Math.abs(brush.x1 - brush.x0)}
                    height={innerHeight}
                    fill="var(--color-accent)"
                    opacity={0.15}
                    stroke="var(--color-accent)"
                    strokeWidth={1}
                  />
                )}
                {/* One overlay drives hover readout AND the brush gesture —
                    per-bar handlers would fight the drag. */}
                <rect
                  x={0}
                  y={0}
                  width={innerWidth}
                  height={innerHeight}
                  fill="transparent"
                  style={onRangeSelect ? { cursor: "crosshair" } : undefined}
                  onMouseDown={
                    onRangeSelect
                      ? (e) => {
                          const local = svgLocalPoint(e, margin);
                          if (!local) return;
                          e.preventDefault();
                          dragAnchorRef.current = local.x;
                          setBrush({ x0: local.x, x1: local.x });
                        }
                      : undefined
                  }
                  onMouseMove={(e) => {
                    const local = svgLocalPoint(e, margin);
                    if (!local) return;
                    if (dragAnchorRef.current != null) {
                      const clamped = Math.max(0, Math.min(innerWidth, local.x));
                      setBrush({ x0: dragAnchorRef.current, x1: clamped });
                      setHover(null);
                      return;
                    }
                    hoverAt(local.x);
                  }}
                  onMouseUp={() => endBrush(true)}
                  onMouseLeave={() => {
                    // Leaving mid-drag commits like TimelineHistogram (the
                    // analyst dragged past the edge on purpose more often
                    // than not); a plain hover just clears.
                    if (dragAnchorRef.current != null) endBrush(true);
                    else setHover(null);
                  }}
                />
              </>
            );
          }}
        </ChartFrame>
        <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
          {hover && (
            <>
              {fmtFull(dates[hover.index])}
              <br />
              <strong>{fmtCount(buckets[hover.index].primary)}</strong> filtered events
              {hasComparison && (
                <>
                  <br />
                  {fmtCount(buckets[hover.index].comparison)} comparison events
                </>
              )}
              {metric !== "count" && (
                <>
                  <br />
                  {METRIC_INFO[metric].label}:{" "}
                  <strong>
                    {primaryValues[hover.index] == null
                      ? "undefined"
                      : `${fmtMetric(primaryValues[hover.index]!)}${isRatio ? " %" : ""}`}
                  </strong>
                </>
              )}
            </>
          )}
        </ChartTooltip>
      </div>
    </div>
  );
}

/** Ratio line: null bins split the path into segments (never drawn as 0). */
function RatioLine({
  values,
  dates,
  x,
  y,
  barWidth,
}: {
  values: (number | null)[];
  dates: Date[];
  x: (d: Date) => number;
  y: (v: number) => number;
  barWidth: number;
}) {
  const segments: { i: number; px: number; py: number }[][] = [];
  let current: { i: number; px: number; py: number }[] = [];
  values.forEach((v, i) => {
    if (v == null) {
      if (current.length > 0) segments.push(current);
      current = [];
      return;
    }
    current.push({ i, px: x(dates[i]) + barWidth / 2, py: y(v) });
  });
  if (current.length > 0) segments.push(current);

  return (
    <>
      {segments.map((seg, s) => (
        <polyline
          key={s}
          points={seg.map((p) => `${p.px},${p.py}`).join(" ")}
          fill="none"
          stroke="var(--color-accent)"
          strokeWidth={1.5}
        />
      ))}
      {segments.flat().map((p) => (
        <circle key={p.i} cx={p.px} cy={p.py} r={3} fill="var(--color-accent)" />
      ))}
    </>
  );
}
