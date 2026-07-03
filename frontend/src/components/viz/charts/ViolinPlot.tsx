import { area as d3area, curveBasis } from "d3-shape";
import { scaleLinear } from "d3-scale";
import { format as formatNum } from "d3-format";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { NumericPlotFrame } from "@/components/viz/primitives/NumericPlotFrame";
import { svgLocalPoint } from "@/components/viz/lib/pointer";
import { kdeFromBins } from "@/components/viz/lib/stats";
import type { FieldNumericResponse } from "@/api/types";

const fmtValue = formatNum(",.3~f");
const MAX_HALF_WIDTH = 90;

interface ViolinPlotProps {
  stats: FieldNumericResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  color?: string;
}

/** Violin plot — the numeric field's distribution shape, from a smoothed
 * version of the server's fixed-width bin counts (see `kdeFromBins`). Shows
 * bimodality/skew a box plot's five-number summary would hide. */
export function ViolinPlot({
  stats,
  svgRef,
  height = 260,
  color = "var(--color-accent)",
}: ViolinPlotProps) {
  const density = kdeFromBins(stats.bins);
  if (stats.count === 0 || density.length === 0 || stats.min == null || stats.max == null) {
    return <ChartEmptyState>No numeric values in the current filter range.</ChartEmptyState>;
  }

  const maxDensity = Math.max(1e-9, ...density.map((d) => d.density));

  return (
    <NumericPlotFrame
      svgRef={svgRef}
      height={height}
      min={stats.min}
      max={stats.max}
      yTickFormat={(v) => fmtValue(v)}
    >
      {({ innerHeight, margin, y, cx, setHover }) => {
        const w = scaleLinear().domain([0, maxDensity]).range([0, MAX_HALF_WIDTH]);

        const rightPath =
          d3area<{ x: number; density: number }>()
            .curve(curveBasis)
            .x0(cx)
            .x1((d) => cx + w(d.density))
            .y((d) => y(d.x))(density) ?? undefined;
        const leftPath =
          d3area<{ x: number; density: number }>()
            .curve(curveBasis)
            .x0(cx)
            .x1((d) => cx - w(d.density))
            .y((d) => y(d.x))(density) ?? undefined;

        return (
          <>
            <path d={rightPath} fill={color} fillOpacity={0.35} stroke={color} strokeWidth={1} />
            <path d={leftPath} fill={color} fillOpacity={0.35} stroke={color} strokeWidth={1} />
            {stats.quantiles["0.5"] != null && (
              <line
                x1={cx - 12}
                x2={cx + 12}
                y1={y(stats.quantiles["0.5"])}
                y2={y(stats.quantiles["0.5"])}
                stroke="var(--viz-ink-primary)"
                strokeWidth={2}
              />
            )}
            {/* Invisible hover strip along the value axis for a value-at-cursor tooltip. */}
            <rect
              x={cx - MAX_HALF_WIDTH}
              y={0}
              width={MAX_HALF_WIDTH * 2}
              height={innerHeight}
              fill="transparent"
              onMouseMove={(e) => {
                const local = svgLocalPoint(e, margin);
                if (!local) return;
                const localY = local.y;
                const value = y.invert(localY);
                setHover({ x: cx + margin.left, y: localY + margin.top, label: fmtValue(value) });
              }}
              onMouseLeave={() => setHover(null)}
            />
          </>
        );
      }}
    </NumericPlotFrame>
  );
}
