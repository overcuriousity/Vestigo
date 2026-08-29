/**
 * Marks — numbering, layout and caption lines for the resolved marks a
 * time-axis figure draws (`docs/VISUALIZE.md` §"Marks"). Pure: the overlay
 * (`primitives/MarksOverlay.tsx`) and the caption (`lib/caption.ts`) both
 * read this so the number beside a rule is the number in the caption.
 */
import type {
  ResolvedInstantMark,
  ResolvedMark,
  ResolvedMarkSource,
  ResolvedMarksResponse,
  ResolvedRangeMark,
} from "@/api/types";

export interface NumberedInstant {
  mark: ResolvedInstantMark;
  n: number;
}

/** Instants in time order, numbered 1.. across every source. */
export function numberInstants(marks: ResolvedMark[]): NumberedInstant[] {
  return marks
    .filter((m): m is ResolvedInstantMark => m.kind === "instant")
    .map((mark, i) => ({ mark, i }))
    .sort((a, b) => Date.parse(a.mark.at) - Date.parse(b.mark.at) || a.i - b.i)
    .map(({ mark }, idx) => ({ mark, n: idx + 1 }));
}

/** Instants closer than this (px) alternate their label tier. */
export const LABEL_CROWD_PX = 48;

export interface InstantLayout extends NumberedInstant {
  px: number;
  tier: 0 | 1;
}
export interface RangeLayout {
  mark: ResolvedRangeMark;
  x0: number;
  x1: number;
  tier: number;
}
export interface MarksLayout {
  instants: InstantLayout[];
  ranges: RangeLayout[];
  /** Instants outside the drawn axis — disclosed, not silently dropped. */
  offscreen: number;
}

export function layoutMarks(
  marks: ResolvedMark[],
  x: (d: Date) => number,
  innerWidth: number,
): MarksLayout {
  const instants: InstantLayout[] = [];
  let offscreen = 0;
  let prevPx = Number.NEGATIVE_INFINITY;
  let prevTier: 0 | 1 = 1;
  for (const numbered of numberInstants(marks)) {
    const px = x(new Date(numbered.mark.at));
    if (!Number.isFinite(px) || px < 0 || px > innerWidth) {
      offscreen += 1;
      continue;
    }
    const tier: 0 | 1 = px - prevPx < LABEL_CROWD_PX ? (prevTier === 0 ? 1 : 0) : 0;
    instants.push({ ...numbered, px, tier });
    prevPx = px;
    prevTier = tier;
  }

  const ranges: RangeLayout[] = [];
  const tierEnds: number[] = [];
  const sorted = marks
    .filter((m): m is ResolvedRangeMark => m.kind === "range")
    .sort((a, b) => Date.parse(a.start) - Date.parse(b.start));
  for (const mark of sorted) {
    const x0 = Math.max(0, x(new Date(mark.start)));
    const x1 = Math.min(innerWidth, x(new Date(mark.end)));
    if (!(x1 > x0)) continue;
    let tier = tierEnds.findIndex((end) => end <= x0);
    if (tier === -1) tier = tierEnds.length;
    tierEnds[tier] = x1;
    ranges.push({ mark, x0, x1, tier });
  }
  return { instants, ranges, offscreen };
}

export const fmtMarkTime = (iso: string) => `${iso.slice(0, 19).replace("T", " ")}Z`;

function numbersFor(source: number, numbered: NumberedInstant[]): string {
  const ns = numbered.filter((i) => i.mark.source === source).map((i) => i.n);
  if (ns.length === 0) return "";
  if (ns.length <= 5) return ns.map((n) => `#${n}`).join(", ");
  return `#${ns[0]}–#${ns[ns.length - 1]}`;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/** One caption line per source, in source order — provenance, numbers, cap. */
export function markCaptionLines(resp: ResolvedMarksResponse): string[] {
  const numbered = numberInstants(resp.marks);
  const bySource = new Map<number, ResolvedMark[]>();
  for (const m of resp.marks) bySource.set(m.source, [...(bySource.get(m.source) ?? []), m]);
  return resp.sources.map((s: ResolvedMarkSource) => {
    const own = bySource.get(s.index) ?? [];
    switch (s.kind) {
      case "instant": {
        const m = own[0] as ResolvedInstantMark | undefined;
        return `mark ${numbersFor(s.index, numbered)}: "${s.label}" at ${m ? fmtMarkTime(m.at) : "?"} — analyst-placed`;
      }
      case "range": {
        const m = own[0] as ResolvedRangeMark | undefined;
        return `mark: "${s.label}" ${m ? `${fmtMarkTime(m.start)} → ${fmtMarkTime(m.end)}` : ""} — analyst-placed`;
      }
      case "baseline":
        return `marks: baseline "${s.label}" — its baseline window and ${plural(Math.max(0, s.count - 1), "suspect window")}, as declared`;
      default: {
        const origin = s.kind === "view" ? "of saved view" : "matching a filter";
        let line = `marks ${numbersFor(s.index, numbered)}: "${s.label}" — ${plural(s.count, "event")} ${origin}`;
        if (s.overflow)
          line += `; the earliest ${s.shown} drawn (cap ${resp.cap}), ${s.count - s.shown} not drawn`;
        if (s.undated > 0) line += `; ${plural(s.undated, "undated event")} not drawn`;
        return line;
      }
    }
  });
}
