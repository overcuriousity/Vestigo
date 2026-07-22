import { useState } from "react";
import { scaleLinear, scaleLog } from "d3-scale";
import { format as formatNum } from "d3-format";
import { line as d3line, curveMonotoneX } from "d3-shape";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { kdeFromBins, numericDomain } from "@/components/viz/lib/stats";
import type { CompareNumericResponse, FieldNumericResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtValue = formatNum(",.3~f");

interface NumericHistogramProps {
  stats?: FieldNumericResponse;
  /** Two-layer numeric result — when set, the comparison layer renders as a
   * hollow outline behind the filled primary bars (shared bin edges are
   * guaranteed server-side) and `stats` is ignored. */
  compare?: CompareNumericResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
  /** Log-scaled count axis — zero-count bins render as zero-height bars. */
  logScale?: boolean;
  /** Density (KDE) curve overlay — single-layer mode only; the curve is a
   * smoothed reading of the same bins, so it never disagrees with the bars. */
  showDensity?: boolean;
  /** Dashed mean + solid median marker lines (single-layer mode only). */
  showMarkers?: boolean;
  /** Pin the count axis to a shared maximum — see `BarChart.countMax`. */
  countMax?: number;
}

/** Fixed-width value histogram for a numeric (interval/ratio) field —
 * distribution of magnitudes, e.g. response bytes, latency, request rate. */
export function NumericHistogram({
  stats,
  compare,
  svgRef,
  height = 220,
  color = "var(--color-accent)",
  logScale = false,
  showDensity = false,
  showMarkers = false,
  countMax,
}: NumericHistogramProps) {
  const [hover, setHover] = useState<{
    x: number;
    y: number;
    x0: number;
    x1: number;
    count: number;
    comparison?: number;
  } | null>(null);
  const ref = useChartRef(svgRef);

  const bins = compare
    ? compare.bins.map((b) => ({ x0: b.x0, x1: b.x1, count: b.primary, comparison: b.comparison }))
    : (stats?.bins ?? []).map((b) => ({ ...b, comparison: undefined as number | undefined }));
  const min = compare ? compare.min : stats?.min;
  const max = compare ? compare.max : stats?.max;

  if (bins.length === 0 || min == null || max == null) {
    return (
      <ChartEmptyState hint="This field may not be numeric — try a Top-values (bar) chart instead.">
        No numeric values for this field in range.
      </ChartEmptyState>
    );
  }

  const maxCount = Math.max(
    1,
    countMax ?? 0,
    ...bins.map((b) => b.count),
    ...bins.map((b) => b.comparison ?? 0),
  );

  // Density curve + markers are single-layer readings of `stats`; both are
  // suppressed in compare mode (two overlaid curves would be unreadable) and
  // the curve additionally under log scale (a density shape drawn against a
  // log count axis misrepresents area).
  const density = showDensity && !compare && !logScale && stats ? kdeFromBins(stats.bins) : [];
  const maxDensity = Math.max(1e-9, ...density.map((d) => d.density));
  const mean = !compare && showMarkers ? stats?.mean : null;
  const median = !compare && showMarkers ? (stats?.quantiles["0.5"] ?? null) : null;

  return (
    <div className="flex flex-col gap-2">
      {compare && (
        <Legend
          entries={[
            { label: "Filtered events", color },
            { label: "Comparison layer", color: "var(--color-fg-disabled)", muted: true },
          ]}
        />
      )}
      <div className="relative">
        <ChartFrame height={height} svgRef={ref}>
          {({ innerWidth, innerHeight, margin }) => {
            const x = scaleLinear().domain(numericDomain(min, max)).range([0, innerWidth]);
            // A log scale has no 0 — clamp to [1, max] and draw zero counts flat.
            const y = logScale
              ? scaleLog().domain([1, maxCount]).range([innerHeight, 0]).clamp(true)
              : scaleLinear().domain([0, maxCount]).nice().range([innerHeight, 0]);
            const topOf = (count: number) =>
              logScale && count < 1 ? innerHeight : y(Math.max(count, logScale ? 1 : 0));
            const gap = 1;

            return (
              <>
                <AxisLeft
                  scale={y as never}
                  innerWidth={innerWidth}
                  tickFormat={(v) => fmtCount(v)}
                />
                <AxisBottom
                  scale={x}
                  innerWidth={innerWidth}
                  innerHeight={innerHeight}
                  tickFormat={(v) => fmtValue(v as number)}
                />
                {bins.map((b, i) => {
                  const bx = x(b.x0);
                  const bw = Math.max(1, x(b.x1) - x(b.x0) - gap);
                  const py = topOf(b.count);
                  return (
                    <g key={i}>
                      {b.comparison != null && b.comparison > 0 && (
                        <rect
                          x={bx}
                          y={topOf(b.comparison)}
                          width={bw}
                          height={innerHeight - topOf(b.comparison)}
                          fill="none"
                          stroke="var(--color-fg-disabled)"
                          strokeWidth={1}
                        />
                      )}
                      <rect
                        x={bx}
                        y={py}
                        width={bw}
                        height={innerHeight - py}
                        fill={color}
                        opacity={compare ? 0.9 : 1}
                        onMouseEnter={() =>
                          setHover({
                            x: bx + bw / 2 + margin.left,
                            y: py + margin.top,
                            x0: b.x0,
                            x1: b.x1,
                            count: b.count,
                            comparison: b.comparison,
                          })
                        }
                        onMouseLeave={() => setHover(null)}
                      />
                    </g>
                  );
                })}
                {density.length > 1 && (
                  <path
                    d={
                      d3line<{ x: number; density: number }>()
                        .curve(curveMonotoneX)
                        .x((d) => x(d.x))
                        .y((d) => y((d.density / maxDensity) * maxCount))(density) ?? undefined
                    }
                    fill="none"
                    stroke="var(--viz-ink-primary)"
                    strokeWidth={1.5}
                    opacity={0.75}
                    pointerEvents="none"
                  />
                )}
                {median != null && (
                  <g pointerEvents="none">
                    <line
                      x1={x(median)}
                      x2={x(median)}
                      y1={0}
                      y2={innerHeight}
                      stroke="var(--viz-ink-primary)"
                      strokeWidth={1.5}
                    />
                    <text
                      x={x(median) + 4}
                      y={10}
                      fontSize={10}
                      fill="var(--viz-ink-primary)"
                    >
                      median {fmtValue(median)}
                    </text>
                  </g>
                )}
                {mean != null && (
                  <g pointerEvents="none">
                    <line
                      x1={x(mean)}
                      x2={x(mean)}
                      y1={0}
                      y2={innerHeight}
                      stroke="var(--viz-ink-primary)"
                      strokeWidth={1.5}
                      strokeDasharray="4 3"
                    />
                    <text x={x(mean) + 4} y={22} fontSize={10} fill="var(--viz-ink-muted)">
                      mean {fmtValue(mean)}
                    </text>
                  </g>
                )}
              </>
            );
          }}
        </ChartFrame>
        <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
          {hover && (
            <>
              [{fmtValue(hover.x0)}, {fmtValue(hover.x1)})<br />
              <strong>{fmtCount(hover.count)}</strong> events
              {hover.comparison != null && <> · comparison: {fmtCount(hover.comparison)}</>}
            </>
          )}
        </ChartTooltip>
      </div>
    </div>
  );
}
