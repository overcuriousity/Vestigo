import type { ScaleBand, ScaleLinear, ScaleTime } from "d3-scale";

type NumericOrTimeScale = ScaleLinear<number, number> | ScaleTime<number, number>;

const TICK_LEN = 5;

/** Bottom axis for a linear or time scale — gridline-free, recessive per the
 * dataviz skill (axis line + tick label only; no chart border, no full grid). */
export function AxisBottom({
  scale,
  innerHeight,
  ticks = 6,
  tickFormat,
}: {
  scale: NumericOrTimeScale;
  innerHeight: number;
  ticks?: number;
  tickFormat: (value: number | Date) => string;
}) {
  const tickValues = scale.ticks(ticks);
  return (
    <g transform={`translate(0,${innerHeight})`}>
      <line x1={0} x2={scale.range()[1]} stroke="var(--viz-axis)" strokeWidth={1} />
      {tickValues.map((v, i) => {
        const x = scale(v as never);
        return (
          <g key={i} transform={`translate(${x},0)`}>
            <line y1={0} y2={TICK_LEN} stroke="var(--viz-axis)" strokeWidth={1} />
            <text
              y={TICK_LEN + 12}
              textAnchor="middle"
              fontSize={10}
              fill="var(--viz-ink-muted)"
            >
              {tickFormat(v)}
            </text>
          </g>
        );
      })}
    </g>
  );
}

/** Bottom axis for a band (categorical) scale — labels rotated when dense. */
export function AxisBottomBand({
  scale,
  innerHeight,
  rotate = false,
  labelFormat,
}: {
  scale: ScaleBand<string>;
  innerHeight: number;
  rotate?: boolean;
  labelFormat?: (value: string) => string;
}) {
  const domain = scale.domain();
  return (
    <g transform={`translate(0,${innerHeight})`}>
      <line x1={0} x2={scale.range()[1]} stroke="var(--viz-axis)" strokeWidth={1} />
      {domain.map((v) => {
        const x = (scale(v) ?? 0) + scale.bandwidth() / 2;
        const label = labelFormat ? labelFormat(v) : v;
        return (
          <g key={v} transform={`translate(${x},0)`}>
            <line y1={0} y2={TICK_LEN} stroke="var(--viz-axis)" strokeWidth={1} />
            <text
              y={TICK_LEN + (rotate ? 10 : 12)}
              textAnchor={rotate ? "end" : "middle"}
              fontSize={10}
              fill="var(--viz-ink-muted)"
              transform={rotate ? `translate(0,${TICK_LEN + 4}) rotate(-40)` : undefined}
            >
              {label.length > 14 ? label.slice(0, 13) + "…" : label}
            </text>
          </g>
        );
      })}
    </g>
  );
}

/** Left axis for a linear numeric scale, with light horizontal gridlines
 * (a recessive gridline is legible aid, not chart chrome — per skill guidance
 * only the y-axis carries them here, x stays gridline-free). */
export function AxisLeft({
  scale,
  innerWidth,
  ticks = 5,
  tickFormat,
  showGrid = true,
}: {
  scale: ScaleLinear<number, number>;
  innerWidth: number;
  ticks?: number;
  tickFormat?: (value: number) => string;
  showGrid?: boolean;
}) {
  const tickValues = scale.ticks(ticks);
  return (
    <g>
      <line
        x1={0}
        y1={scale.range()[0]}
        x2={0}
        y2={scale.range()[1]}
        stroke="var(--viz-axis)"
        strokeWidth={1}
      />
      {tickValues.map((v, i) => {
        const y = scale(v);
        return (
          <g key={i} transform={`translate(0,${y})`}>
            {showGrid && (
              <line x1={0} x2={innerWidth} stroke="var(--viz-grid)" strokeWidth={1} />
            )}
            <line x1={-TICK_LEN} x2={0} stroke="var(--viz-axis)" strokeWidth={1} />
            <text
              x={-TICK_LEN - 4}
              dy="0.32em"
              textAnchor="end"
              fontSize={10}
              fill="var(--viz-ink-muted)"
            >
              {tickFormat ? tickFormat(v) : v}
            </text>
          </g>
        );
      })}
    </g>
  );
}
