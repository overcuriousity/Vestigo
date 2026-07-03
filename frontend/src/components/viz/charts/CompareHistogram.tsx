import { useState } from "react";
import { scaleLinear, scaleTime } from "d3-scale";
import { utcFormat } from "d3-time-format";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { applyMetric, METRIC_INFO, type Metric } from "@/components/viz/lib/transforms";
import type { CompareTimeResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtMetric = formatNum(",.2~f");
// utcFormat, not timeFormat — bucket starts are UTC instants (see TimeHistogram).
const fmtTick = utcFormat("%b %d %H:%M");
const fmtFull = utcFormat("%Y-%m-%d %H:%M:%S UTC");

interface CompareHistogramProps {
  data: CompareTimeResponse;
  metric: Metric;
  /** Whether a comparison layer is active — off means `data.comparison` is
   * all zeros and only the primary series is drawn. */
  hasComparison: boolean;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
}

/**
 * Event-count-over-time histogram with an optional comparison layer — the
 * "filtered subset against everything" chart. The primary layer renders as
 * filled vertical bars in front of the comparison layer's hollow outline
 * bars ("part of whole" reads instantly); the active metric transforms both
 * layers identically, except `ratio` which collapses them into one derived
 * percentage line. Null metric bins (first delta bin, zero-baseline ratio
 * bins) are skipped, never drawn as 0.
 */
export function CompareHistogram({
  data,
  metric,
  hasComparison,
  svgRef,
  height = 280,
}: CompareHistogramProps) {
  const [hover, setHover] = useState<{ x: number; y: number; index: number } | null>(null);
  const ref = useChartRef(svgRef);

  const buckets = data.buckets;
  if (buckets.length === 0) {
    return <ChartEmptyState>No data in the current filter range.</ChartEmptyState>;
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
                        onMouseEnter={() =>
                          setHover({
                            x: x(dates[i]) + barWidth / 2 + margin.left,
                            y: by + margin.top,
                            index: i,
                          })
                        }
                        onMouseLeave={() => setHover(null)}
                      />
                    );
                  })}
                {isRatio && (
                  <RatioLine
                    values={primaryValues}
                    dates={dates}
                    x={x}
                    y={y}
                    barWidth={barWidth}
                    onHover={(i, px, py) =>
                      setHover(
                        i == null ? null : { x: px + margin.left, y: py + margin.top, index: i },
                      )
                    }
                  />
                )}
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
  onHover,
}: {
  values: (number | null)[];
  dates: Date[];
  x: (d: Date) => number;
  y: (v: number) => number;
  barWidth: number;
  onHover: (index: number | null, px: number, py: number) => void;
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
        <circle
          key={p.i}
          cx={p.px}
          cy={p.py}
          r={3}
          fill="var(--color-accent)"
          onMouseEnter={() => onHover(p.i, p.px, p.py)}
          onMouseLeave={() => onHover(null, 0, 0)}
        />
      ))}
    </>
  );
}
