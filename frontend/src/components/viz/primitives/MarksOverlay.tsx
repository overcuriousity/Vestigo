/**
 * Marks drawn over a time axis: instants as numbered dashed rules, ranges as
 * tinted bands. One accent for instants and one tint for ranges — never a
 * Compare layer colour, so a mark can never be mistaken for a data series.
 * Layout and numbering come from `lib/marks.ts`, which the caption also
 * reads, so #3 on the figure is #3 in the caption.
 */
import type { JSX } from "react";
import type { ResolvedMark } from "@/api/types";
import { layoutMarks } from "@/components/viz/lib/marks";

interface Props {
  marks: ResolvedMark[];
  x: (d: Date) => number;
  innerWidth: number;
  innerHeight: number;
}

export function MarksOverlay({ marks, x, innerWidth, innerHeight }: Props): JSX.Element | null {
  if (marks.length === 0) return null;
  const layout = layoutMarks(marks, x, innerWidth);
  if (layout.instants.length === 0 && layout.ranges.length === 0) return null;
  return (
    <g data-marks pointerEvents="none">
      {layout.ranges.map((r) => (
        <g key={`${r.mark.source}-${r.mark.start}-${r.mark.end}`}>
          <rect
            data-mark-range
            x={r.x0}
            y={0}
            width={r.x1 - r.x0}
            height={innerHeight}
            fill="var(--color-warning-dim)"
          />
          <text
            x={r.x0 + 3}
            y={10 + r.tier * 12}
            fontSize={10}
            fill="var(--color-warning)"
            fontWeight={600}
          >
            {r.mark.label}
          </text>
        </g>
      ))}
      {layout.instants.map((i) => (
        <g key={`${i.mark.source}-${i.n}`}>
          <title>{i.mark.label}</title>
          <line
            data-mark-instant
            x1={i.px}
            x2={i.px}
            y1={0}
            y2={innerHeight}
            stroke="var(--color-warning)"
            strokeWidth={1}
            strokeDasharray="3 2"
          />
          <text
            data-mark-n
            x={i.px + 2}
            y={i.tier === 0 ? 10 : 22}
            fontSize={10}
            fill="var(--color-warning)"
            fontWeight={600}
          >
            #{i.n}
          </text>
        </g>
      ))}
    </g>
  );
}
