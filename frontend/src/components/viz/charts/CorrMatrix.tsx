import { useState } from "react";
import { format as formatNum } from "d3-format";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { divergingColor, divergingInk } from "@/components/viz/lib/colors";
import { fieldTokenLabel } from "@/components/viz/lib/fieldDisplay";
import type { FieldCorrelationResponse } from "@/api/types";

const fmtCoef = formatNum("+.2f");
const fmtInt = formatNum(",d");

export type CorrMethod = "pearson" | "spearman";

/** Significance threshold below which a coefficient is drawn as read-worthy. */
const CORR_ALPHA = 0.05;
/** Pairwise-complete count below which a coefficient is too thin to lean on. */
const CORR_MIN_N = 30;

function fmtP(p: number | null | undefined): string {
  if (p == null) return "n/a";
  if (p === 0) return "< 1e-300";
  if (p < 0.001) return p.toExponential(1);
  return p.toFixed(3);
}

interface CorrMatrixProps {
  data: FieldCorrelationResponse;
  /** Which coefficient fills the cells. Both are always in the tooltip. */
  method?: CorrMethod;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  /** Drill-down: a cell click opens that pair as a scatter plot. */
  onPairClick?: (x: string, y: string) => void;
}

/**
 * Lower-triangle correlation matrix over several numeric fields — the mark
 * the lecture prefers past three or four quantitative variables, where
 * reading scatter plots pair by pair stops scaling.
 *
 * Only the lower triangle is drawn: the upper half is the same numbers
 * mirrored, and a full square invites reading twice as much information as
 * there is. Each cell prints its coefficient because colour alone cannot be
 * read to two decimals — the fill is for scanning, the number is the value.
 * Colour is a diverging ramp centred on 0, so sign is legible before
 * magnitude.
 *
 * A coefficient that is not distinguishable from zero (p ≥ 0.05), or that
 * rests on too few pairwise-complete events, is drawn faded with a dashed
 * border: full-strength colour on a coefficient the data cannot support reads
 * as a finding, which is exactly the misreading this chart exists to prevent.
 * The number is still printed — the fade qualifies it, it does not hide it.
 */
export function CorrMatrix({
  data,
  method = "pearson",
  svgRef,
  height = 360,
  onPairClick,
}: CorrMatrixProps) {
  const [hover, setHover] = useState<{ x: number; y: number; body: React.ReactNode } | null>(
    null,
  );
  const ref = useChartRef(svgRef);

  const fields = data.fields;
  const byPair = new Map(data.pairs.map((p) => [`${p.x}\0${p.y}`, p]));
  const pairOf = (a: string, b: string) =>
    byPair.get(`${a}\0${b}`) ?? byPair.get(`${b}\0${a}`) ?? null;

  if (fields.length < 2 || data.pairs.length === 0) {
    return (
      <ChartEmptyState hint="Pick at least two numeric fields to correlate.">
        No field pairs to correlate.
      </ChartEmptyState>
    );
  }

  return (
    <div className="relative flex flex-col gap-2">
      <ChartFrame
        height={height}
        svgRef={ref}
        margin={{ top: 8, right: 16, bottom: 96, left: 128 }}
      >
        {({ innerWidth, innerHeight, margin }) => {
          // Row i is fields[1..n-1]; column j is fields[0..n-2] — the lower
          // triangle, so the diagonal (a field with itself, always 1) is
          // never drawn.
          const cols = fields.length - 1;
          const cell = Math.min(innerWidth / cols, innerHeight / cols);

          return (
            <>
              {fields.slice(1).map((rowField, ri) =>
                fields.slice(0, -1).map((colField, ci) => {
                  if (ci > ri) return null;
                  const pair = pairOf(rowField, colField);
                  const value = pair ? pair[method] : null;
                  const p = pair
                    ? method === "pearson"
                      ? pair.p_pearson
                      : pair.p_spearman
                    : null;
                  const thin = pair != null && pair.n < CORR_MIN_N;
                  const insignificant = p == null || p >= CORR_ALPHA;
                  const qualified = value != null && (thin || insignificant);
                  const x = ci * cell;
                  const y = ri * cell;
                  const clickable = onPairClick != null && pair != null && pair.n > 0;
                  return (
                    <g
                      key={`${rowField}\0${colField}`}
                      style={clickable ? { cursor: "pointer" } : undefined}
                      onClick={clickable ? () => onPairClick(colField, rowField) : undefined}
                      onMouseEnter={() =>
                        setHover({
                          x: x + cell / 2 + margin.left,
                          y: y + margin.top,
                          body: (
                            <>
                              {fieldTokenLabel(colField)} × {fieldTokenLabel(rowField)}
                              <br />
                              Pearson r ={" "}
                              <strong>
                                {pair?.pearson != null ? fmtCoef(pair.pearson) : "n/a"}
                              </strong>{" "}
                              (p = {fmtP(pair?.p_pearson)})
                              <br />
                              Spearman ρ ={" "}
                              <strong>
                                {pair?.spearman != null ? fmtCoef(pair.spearman) : "n/a"}
                              </strong>{" "}
                              (p = {fmtP(pair?.p_spearman)})
                              <br />
                              {fmtInt(pair?.n ?? 0)} events with both values
                              {qualified && (
                                <>
                                  <br />
                                  {thin
                                    ? `fewer than ${CORR_MIN_N} pairs — too thin to lean on`
                                    : "not distinguishable from zero at p < 0.05"}
                                </>
                              )}
                              {clickable && (
                                <>
                                  <br />
                                  click to open as a scatter plot
                                </>
                              )}
                            </>
                          ),
                        })
                      }
                      onMouseLeave={() => setHover(null)}
                    >
                      <rect
                        x={x}
                        y={y}
                        width={Math.max(1, cell - 2)}
                        height={Math.max(1, cell - 2)}
                        rx={2}
                        fill={
                          value != null
                            ? divergingColor(value)
                            : "var(--color-bg-surface)"
                        }
                        fillOpacity={qualified ? 0.3 : 1}
                        stroke={
                          qualified ? "var(--viz-ink-muted)" : "var(--color-border-subtle)"
                        }
                        strokeDasharray={qualified ? "3 2" : undefined}
                      />
                      <text
                        x={x + cell / 2}
                        y={y + cell / 2}
                        textAnchor="middle"
                        dy="0.32em"
                        fontSize={Math.min(12, cell / 3.2)}
                        fill={
                          value == null
                            ? "var(--viz-ink-muted)"
                            : qualified
                              ? "var(--viz-ink-primary)"
                              : divergingInk(value)
                        }
                        pointerEvents="none"
                      >
                        {value != null ? fmtCoef(value) : "—"}
                      </text>
                    </g>
                  );
                }),
              )}
              {/* Row labels (left) and column labels (bottom, rotated). */}
              {fields.slice(1).map((f, ri) => (
                <text
                  key={`r-${f}`}
                  x={-8}
                  y={ri * cell + cell / 2}
                  textAnchor="end"
                  dy="0.32em"
                  fontSize={11}
                  fill="var(--viz-ink-muted)"
                >
                  {fieldTokenLabel(f)}
                </text>
              ))}
              {fields.slice(0, -1).map((f, ci) => (
                <text
                  key={`c-${f}`}
                  transform={`translate(${ci * cell + cell / 2}, ${
                    (fields.length - 1) * cell + 8
                  }) rotate(-40)`}
                  textAnchor="end"
                  fontSize={11}
                  fill="var(--viz-ink-muted)"
                >
                  {fieldTokenLabel(f)}
                </text>
              ))}
            </>
          );
        }}
      </ChartFrame>
      <CorrLegend method={method} />
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover?.body}
      </ChartTooltip>
    </div>
  );
}

/** The −1…+1 colour key, so the fill can be read without hovering. */
function CorrLegend({ method }: { method: CorrMethod }) {
  const stops = [-1, -0.6, -0.2, 0.2, 0.6, 1];
  return (
    <div className="flex items-center gap-2 text-xs text-[var(--color-fg-muted)]">
      <span>{method === "pearson" ? "Pearson r" : "Spearman ρ"}</span>
      <span>−1</span>
      <span className="flex">
        {stops.map((t) => (
          <span
            key={t}
            className="inline-block h-3 w-6"
            style={{ background: divergingColor(t) }}
            aria-hidden
          />
        ))}
      </span>
      <span>+1</span>
      <span>(0 = no relationship of this kind)</span>
      <span className="text-[var(--color-fg-muted)]">
        · faded, dashed = p ≥ {CORR_ALPHA} or fewer than {CORR_MIN_N} pairs
      </span>
    </div>
  );
}
