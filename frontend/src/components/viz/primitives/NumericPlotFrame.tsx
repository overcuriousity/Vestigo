import { useState } from "react";
import { scaleLinear, type ScaleLinear } from "d3-scale";
import type { ChartMargin } from "@/components/viz/primitives/ChartFrame";
import { AxisLeft } from "@/components/viz/primitives/Axis";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { numericDomain } from "@/components/viz/lib/stats";

const MARGIN: ChartMargin = { top: 16, right: 24, bottom: 24, left: 56 };

export interface NumericPlotHover {
  x: number;
  y: number;
  label: string;
}

interface NumericPlotFrameProps {
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height: number;
  min: number;
  max: number;
  yTickFormat: (v: number) => string;
  children: (ctx: {
    innerWidth: number;
    innerHeight: number;
    margin: ChartMargin;
    y: ScaleLinear<number, number>;
    cx: number;
    setHover: (hover: NumericPlotHover | null) => void;
  }) => React.ReactNode;
}

/**
 * Shared scaffold for the single-field-distribution numeric charts
 * (BoxPlot, ViolinPlot): identical ref/margin/y-scale/hover-tooltip
 * boilerplate around a chart-specific render prop for the marks themselves.
 */
export function NumericPlotFrame({
  svgRef,
  height,
  min,
  max,
  yTickFormat,
  children,
}: NumericPlotFrameProps) {
  const [hover, setHover] = useState<NumericPlotHover | null>(null);
  const ref = useChartRef(svgRef);

  return (
    <div className="relative">
      <ChartFrame height={height} svgRef={ref} margin={MARGIN}>
        {({ innerWidth, innerHeight, margin }) => {
          const y = scaleLinear().domain(numericDomain(min, max)).nice().range([innerHeight, 0]);
          const cx = innerWidth / 2;
          return (
            <>
              <AxisLeft scale={y} innerWidth={innerWidth} tickFormat={yTickFormat} />
              {children({ innerWidth, innerHeight, margin, y, cx, setHover })}
            </>
          );
        }}
      </ChartFrame>
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover?.label}
      </ChartTooltip>
    </div>
  );
}
