/**
 * One small glyph per figure for the rail's gallery. Presentation only —
 * `currentColor` throughout, so a greyed entry is greyed by its text colour.
 * Coordinates are a 40×24 box.
 */
import type { JSX } from "react";
import type { ChartType } from "../lib/chartConfig";

const bars = (hs: number[], w = 5, gap = 2) =>
  hs.map((h, i) => (
    <rect key={i} x={i * (w + gap)} y={24 - h} width={w} height={h} fill="currentColor" />
  ));

const HEAT = [0.2, 0.9, 0.5, 0.3, 0.6, 0.2, 0.8, 0.4, 0.3, 0.7, 0.2, 0.9];
const PUNCH = [1, 3, 2, 1, 2, 4, 3, 1, 1, 2, 1, 3];
const PIVOT = [0.9, 0.3, 0.2, 0.3, 0.8, 0.4, 0.2, 0.4, 0.9];
const DOTS: [number, number][] = [
  [3, 20],
  [8, 15],
  [12, 17],
  [17, 10],
  [22, 12],
  [27, 6],
  [31, 9],
  [37, 3],
];

export const THUMBNAILS: Record<ChartType, () => JSX.Element> = {
  time: () => <>{bars([6, 9, 5, 20, 14, 7])}</>,
  bar: () => <>{bars([22, 15, 10, 6, 3])}</>,
  pie: () => (
    <>
      <circle cx="20" cy="12" r="10" fill="none" stroke="currentColor" strokeWidth="2" />
      <path d="M20 12 L20 2 A10 10 0 0 1 29.5 15 Z" fill="currentColor" />
    </>
  ),
  waffle: () => (
    <>
      {Array.from({ length: 16 }, (_, i) => (
        <rect
          key={i}
          x={(i % 4) * 6 + 8}
          y={Math.floor(i / 4) * 6}
          width="5"
          height="5"
          fill="currentColor"
          opacity={i < 9 ? 1 : 0.35}
        />
      ))}
    </>
  ),
  heatmap: () => (
    <>
      {HEAT.map((o, i) => (
        <rect
          key={i}
          x={(i % 4) * 10}
          y={Math.floor(i / 4) * 8}
          width="9"
          height="7"
          fill="currentColor"
          opacity={o}
        />
      ))}
    </>
  ),
  line: () => (
    <path
      d="M0 18 L8 12 L16 15 L24 4 L32 9 L40 6"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
    />
  ),
  histogram: () => <>{bars([4, 10, 20, 16, 8, 3])}</>,
  box: () => (
    <>
      <line x1="4" y1="12" x2="36" y2="12" stroke="currentColor" strokeWidth="1.5" />
      <rect x="12" y="6" width="16" height="12" fill="none" stroke="currentColor" strokeWidth="2" />
      <line x1="19" y1="6" x2="19" y2="18" stroke="currentColor" strokeWidth="2" />
    </>
  ),
  violin: () => <path d="M20 1 C12 6 12 18 20 23 C28 18 28 6 20 1 Z" fill="currentColor" />,
  ecdf: () => (
    <path
      d="M0 23 L8 23 L8 18 L14 18 L14 10 L22 10 L22 4 L32 4 L32 1 L40 1"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
    />
  ),
  punchcard: () => (
    <>
      {PUNCH.map((r, i) => (
        <circle
          key={i}
          cx={(i % 4) * 10 + 5}
          cy={Math.floor(i / 4) * 8 + 4}
          r={r}
          fill="currentColor"
        />
      ))}
    </>
  ),
  pivot: () => (
    <>
      {PIVOT.map((o, i) => (
        <rect
          key={i}
          x={(i % 3) * 13}
          y={Math.floor(i / 3) * 8}
          width="12"
          height="7"
          fill="currentColor"
          opacity={o}
        />
      ))}
    </>
  ),
  sankey: () => (
    <>
      <path
        d="M0 3 L12 3 C24 3 16 15 40 15 L40 21 C16 21 24 9 12 9 L0 9 Z"
        fill="currentColor"
        opacity="0.8"
      />
      <path
        d="M0 14 L12 14 C24 14 16 3 40 3 L40 7 C16 7 24 20 12 20 L0 20 Z"
        fill="currentColor"
        opacity="0.4"
      />
    </>
  ),
  scatter: () => (
    <>
      {DOTS.map(([x, y], i) => (
        <circle key={i} cx={x} cy={y} r="2" fill="currentColor" />
      ))}
    </>
  ),
  corr: () => (
    <>
      {Array.from({ length: 9 }, (_, i) => (
        <rect
          key={i}
          x={(i % 3) * 13}
          y={Math.floor(i / 3) * 8}
          width="12"
          height="7"
          fill="currentColor"
          opacity={i % 4 === 0 ? 1 : 0.3}
        />
      ))}
    </>
  ),
};

export function FigureThumbnail({
  chartType,
  className,
}: {
  chartType: ChartType;
  className?: string;
}) {
  const Glyph = THUMBNAILS[chartType];
  return (
    <svg viewBox="0 0 40 24" width="40" height="24" aria-hidden="true" className={className}>
      <Glyph />
    </svg>
  );
}
