import { useState } from "react";
import { format as formatNum } from "d3-format";
import { ChartEmptyState } from "@/components/viz/primitives/ChartEmptyState";
import { ChartFrame } from "@/components/viz/primitives/ChartFrame";
import { ChartTooltip } from "@/components/viz/primitives/ChartTooltip";
import { Legend } from "@/components/viz/primitives/Legend";
import { useChartRef } from "@/components/viz/primitives/useChartRef";
import { buildSeriesColorMap, OTHER_KEY, OTHER_LABEL } from "@/components/viz/lib/colors";
import { fieldValueLabel } from "@/components/viz/lib/fieldDisplay";
import type { ChartValueClickHandler } from "@/components/viz/lib/interaction";
import { allocateWaffleCells, type WaffleRow } from "@/components/viz/lib/waffle";
import type { FieldTermsResponse } from "@/api/types";

const fmtCount = formatNum(",d");
const fmtPct = formatNum(".1%");
const GRID = 10;

interface WaffleChartProps {
  terms: FieldTermsResponse;
  svgRef?: React.RefObject<SVGSVGElement | null>;
  height?: number;
  onValueClick?: ChartValueClickHandler;
}

/**
 * Waffle chart — parts of a whole as a 10×10 grid of countable cells. The
 * lecture's recommended replacement for a pie once there are five or more
 * categories: counting squares beats judging angles, and unlike a pie the
 * "one cell = one percent" reading is exact.
 */
export function WaffleChart({ terms, svgRef, height = 280, onValueClick }: WaffleChartProps) {
  const [hover, setHover] = useState<{ x: number; y: number; label: string; count: number } | null>(
    null,
  );
  const ref = useChartRef(svgRef);

  const rows = terms.values.map((v) => ({
    key: v.value,
    label: fieldValueLabel(terms.field, v.value),
    count: v.count,
  }));
  if (terms.other_count > 0) {
    rows.push({ key: OTHER_KEY, label: OTHER_LABEL, count: terms.other_count });
  }
  const total = rows.reduce((s, r) => s + r.count, 0);
  const allocated = allocateWaffleCells(rows);

  if (allocated.length === 0 || total === 0) {
    return (
      <ChartEmptyState size="sm" hint="Pick a different field, or clear the active filters.">
        No values for this field in range.
      </ChartEmptyState>
    );
  }

  const colorMap = buildSeriesColorMap(
    allocated.map((r) => ({ key: r.key, isOther: r.key === OTHER_KEY })),
  );
  // Cell index -> owning row, filled row-major from the bottom left.
  const owners: WaffleRow[] = [];
  for (const row of allocated) {
    for (let i = 0; i < row.cells; i++) owners.push(row);
  }

  return (
    <div className="relative flex flex-col gap-2">
      <ChartFrame height={height} svgRef={ref} margin={{ top: 8, right: 8, bottom: 8, left: 8 }}>
        {({ innerWidth, innerHeight, margin }) => {
          const size = Math.min(innerWidth, innerHeight);
          const cell = size / GRID;
          const gap = Math.max(1, cell * 0.08);
          const originX = (innerWidth - size) / 2;
          const originY = (innerHeight - size) / 2;

          return (
            <>
              {owners.map((row, i) => {
                const col = i % GRID;
                // Fill from the bottom row upward — a rising stack reads as
                // "share of the whole" more naturally than a top-down one.
                const rowIndex = GRID - 1 - Math.floor(i / GRID);
                const x = originX + col * cell;
                const y = originY + rowIndex * cell;
                const clickable = onValueClick != null && row.key !== OTHER_KEY;
                return (
                  <rect
                    key={i}
                    x={x}
                    y={y}
                    width={Math.max(1, cell - gap)}
                    height={Math.max(1, cell - gap)}
                    rx={1}
                    fill={colorMap.get(row.key) ?? "var(--color-accent)"}
                    style={clickable ? { cursor: "pointer" } : undefined}
                    onClick={
                      clickable
                        ? (e) =>
                            onValueClick({
                              entries: [[terms.field, row.key]],
                              clientX: e.clientX,
                              clientY: e.clientY,
                            })
                        : undefined
                    }
                    onMouseEnter={() =>
                      setHover({
                        x: x + margin.left + cell / 2,
                        y: y + margin.top,
                        label: row.label,
                        count: row.count,
                      })
                    }
                    onMouseLeave={() => setHover(null)}
                  />
                );
              })}
            </>
          );
        }}
      </ChartFrame>
      <Legend
        entries={allocated.map((r) => ({
          label: `${r.label} — ${fmtCount(r.count)} (${r.cells} ${r.cells === 1 ? "cell" : "cells"})`,
          color: colorMap.get(r.key) ?? "var(--color-accent)",
          key: r.key,
        }))}
        onEntryClick={
          onValueClick
            ? (key, e) => {
                if (key === OTHER_KEY) return;
                onValueClick({ entries: [[terms.field, key]], clientX: e.clientX, clientY: e.clientY });
              }
            : undefined
        }
      />
      <ChartTooltip x={hover?.x ?? 0} y={hover?.y ?? 0} visible={hover != null}>
        {hover && (
          <>
            {hover.label}: <strong>{fmtCount(hover.count)}</strong> ({fmtPct(hover.count / total)})
          </>
        )}
      </ChartTooltip>
    </div>
  );
}
