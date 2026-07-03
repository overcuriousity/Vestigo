import type { ScaleBand, ScaleLinear, ScaleTime } from "d3-scale";

type NumericOrTimeScale = ScaleLinear<number, number> | ScaleTime<number, number>;

const TICK_LEN = 5;

/** Minimum horizontal pixels a rotated time/number tick label needs before
 * the next one starts overlapping it — used to cap tick count by available
 * width rather than a fixed count that overlaps on a narrow chart. */
const MIN_TICK_SPACING = 70;

/** Bottom axis for a linear or time scale — gridline-free, recessive per the
 * dataviz skill (axis line + tick label only; no chart border, no full grid).
 * Tick count is capped by `innerWidth` so labels never overlap regardless of
 * chart size; labels rotate -40° by default since time/number ticks are
 * wider than the space between them once there's more than a couple. */
export function AxisBottom({
  scale,
  innerWidth,
  innerHeight,
  ticks = 8,
  tickFormat,
  rotate = true,
}: {
  scale: NumericOrTimeScale;
  innerWidth: number;
  innerHeight: number;
  ticks?: number;
  tickFormat: (value: number | Date) => string;
  rotate?: boolean;
}) {
  const maxByWidth = Math.max(2, Math.floor(innerWidth / MIN_TICK_SPACING));
  const tickValues = scale.ticks(Math.min(ticks, maxByWidth));
  return (
    <g transform={`translate(0,${innerHeight})`}>
      <line x1={0} x2={scale.range()[1]} stroke="var(--viz-axis)" strokeWidth={1} />
      {tickValues.map((v, i) => {
        const x = scale(v as never);
        return (
          <g key={i} transform={`translate(${x},0)`}>
            <line y1={0} y2={TICK_LEN} stroke="var(--viz-axis)" strokeWidth={1} />
            <text
              y={rotate ? TICK_LEN + 4 : TICK_LEN + 12}
              textAnchor={rotate ? "end" : "middle"}
              transform={rotate ? "rotate(-40)" : undefined}
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

/** Minimum horizontal pixels between labelled bands. Rotated labels project
 * mostly vertically, so they tolerate much tighter horizontal packing than
 * upright ones. */
const MIN_BAND_LABEL_SPACING_ROTATED = 20;
const MIN_BAND_LABEL_SPACING_UPRIGHT = 70;

/** Bottom axis for a band (categorical) scale — labels rotated when dense.
 * When bands are narrower than a label needs, only every Nth band is
 * labelled (every band keeps its tick mark) instead of overlapping.
 * `maxLabelChars` caps label length before ellipsis; charts whose labels are
 * uniform-width strings (e.g. Heatmap timestamps) should raise it, since a
 * shared prefix + ellipsis makes every label identical and useless. */
export function AxisBottomBand({
  scale,
  innerHeight,
  rotate = false,
  labelFormat,
  maxLabelChars = 14,
}: {
  scale: ScaleBand<string>;
  innerHeight: number;
  rotate?: boolean;
  labelFormat?: (value: string) => string;
  maxLabelChars?: number;
}) {
  const domain = scale.domain();
  const minSpacing = rotate ? MIN_BAND_LABEL_SPACING_ROTATED : MIN_BAND_LABEL_SPACING_UPRIGHT;
  const labelStep = Math.max(1, Math.ceil(minSpacing / Math.max(scale.step(), 1)));
  return (
    <g transform={`translate(0,${innerHeight})`}>
      <line x1={0} x2={scale.range()[1]} stroke="var(--viz-axis)" strokeWidth={1} />
      {domain.map((v, i) => {
        const x = (scale(v) ?? 0) + scale.bandwidth() / 2;
        const label = labelFormat ? labelFormat(v) : v;
        const showLabel = i % labelStep === 0;
        return (
          <g key={v} transform={`translate(${x},0)`}>
            <line y1={0} y2={TICK_LEN} stroke="var(--viz-axis)" strokeWidth={1} />
            {showLabel && (
              <text
                y={TICK_LEN + (rotate ? 10 : 12)}
                textAnchor={rotate ? "end" : "middle"}
                fontSize={10}
                fill="var(--viz-ink-muted)"
                transform={rotate ? `translate(0,${TICK_LEN + 4}) rotate(-40)` : undefined}
              >
                {label.length > maxLabelChars ? label.slice(0, maxLabelChars - 1) + "…" : label}
              </text>
            )}
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
