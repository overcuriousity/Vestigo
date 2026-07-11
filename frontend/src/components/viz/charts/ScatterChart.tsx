import { useMemo, useState } from "react";
import { scaleLinear, scaleLog, type ScaleLinear } from "d3-scale";
import { format as formatNum } from "d3-format";
import { AxisBottom, AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import type { FieldScatterResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtVal = formatNum(",.6~g");
const POINT_R = 2.5;

interface ScatterChartProps {
  data: FieldScatterResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** Log-scale both axes (falls back to linear per-axis when the axis's
   * full-data extent includes zero or negatives — log is undefined there). */
  logScale?: boolean;
}

/**
 * Numeric × numeric scatter of a server-drawn uniform random sample. Axis
 * domains come from the FULL data's extents (the response carries them
 * separately), so the frame is truthful even when only a sample of points
 * is drawn — the caption states "showing N of M points". Point hover
 * reports the exact pair.
 */
export function ScatterChart({ data, svgRef, height = 320, logScale = false }: ScatterChartProps) {
  const [hover, setHover] = useState<{ x: number; y: number; px: number; py: number } | null>(
    null,
  );
  const ref = useChartRef(svgRef);

  if (data.total === 0 || data.points.length === 0) {
    return (
      <ChartEmptyState hint="Both fields need numeric values on the same events. Non-numeric fields chart better as Bar / Pie (categorical).">
        No events with numeric values for both fields match the current filters.
      </ChartEmptyState>
    );
  }

  return (
    <div className="relative">
      <ChartFrame height={height} svgRef={ref} margin={{ top: 12, right: 16, bottom: 56, left: 64 }}>
        {({ innerWidth, innerHeight, margin }) => (
          <ScatterBody
            data={data}
            innerWidth={innerWidth}
            innerHeight={innerHeight}
            marginLeft={margin.left}
            marginTop={margin.top}
            logScale={logScale}
            setHover={setHover}
          />
        )}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover && (
          <>
            {data.field_x} = <strong>{fmtVal(hover.px)}</strong>
            <br />
            {data.field_y} = <strong>{fmtVal(hover.py)}</strong>
            <br />
            {fmtCount(data.sampled)} of {fmtCount(data.total)} points shown
          </>
        )}
      </ChartTooltip>
    </div>
  );
}

/** Build a linear or log scale over [min, max]; log silently degrades to
 * linear when the domain crosses or touches zero. A degenerate single-value
 * domain is padded so points don't all land on an axis line. */
function buildScale(
  min: number,
  max: number,
  range: [number, number],
  wantLog: boolean,
): ScaleLinear<number, number> {
  const pad = min === max ? Math.max(1, Math.abs(min) * 0.05) : 0;
  const lo = min - pad;
  const hi = max + pad;
  if (wantLog && lo > 0) {
    return scaleLog().domain([lo, hi]).range(range).nice() as unknown as ScaleLinear<
      number,
      number
    >;
  }
  return scaleLinear().domain([lo, hi]).range(range).nice();
}

function ScatterBody({
  data,
  innerWidth,
  innerHeight,
  marginLeft,
  marginTop,
  logScale,
  setHover,
}: {
  data: FieldScatterResponse;
  innerWidth: number;
  innerHeight: number;
  marginLeft: number;
  marginTop: number;
  logScale: boolean;
  setHover: (h: { x: number; y: number; px: number; py: number } | null) => void;
}) {
  const x = useMemo(
    () => buildScale(data.x_min ?? 0, data.x_max ?? 1, [0, innerWidth], logScale),
    [data.x_min, data.x_max, innerWidth, logScale],
  );
  const y = useMemo(
    () => buildScale(data.y_min ?? 0, data.y_max ?? 1, [innerHeight, 0], logScale),
    [data.y_min, data.y_max, innerHeight, logScale],
  );

  return (
    <>
      <AxisBottom
        scale={x}
        innerWidth={innerWidth}
        innerHeight={innerHeight}
        tickFormat={(v) => fmtVal(v as number)}
      />
      <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={fmtVal} />
      {data.points.map(([px, py], i) => (
        <circle
          key={i}
          cx={x(px)}
          cy={y(py)}
          r={POINT_R}
          fill="var(--viz-series-1)"
          fillOpacity={0.5}
          onMouseEnter={() =>
            setHover({ x: x(px) + marginLeft, y: y(py) + marginTop, px, py })
          }
          onMouseLeave={() => setHover(null)}
        />
      ))}
    </>
  );
}
