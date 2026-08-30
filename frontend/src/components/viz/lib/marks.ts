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

/** Narrowest a range band is ever drawn, so a zero-length one is still visible. */
const MIN_RANGE_PX = 2;

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
  /** Instants outside the drawn axis. Disclosed, not silently dropped: the
   * caption says so per source via `markCaptionLines(resp, domain)`, which
   * reaches the same verdict from the axis domain (`lib/timeDomain.ts`). */
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
    if (!Number.isFinite(x0) || !Number.isFinite(x1) || x1 < x0) continue;
    // A zero-length range — or one whose whole span is sub-pixel at this
    // width — used to be dropped by `x1 > x0` while `outsideDomain` still
    // called it drawn (it only reports a range fully outside the domain), so
    // the caption described a band that was not on the canvas. Give it the
    // minimum width instead: the band is real, and one pixel is a smaller lie
    // about its extent than not drawing it at all.
    const end = Math.min(innerWidth, Math.max(x1, x0 + MIN_RANGE_PX));
    // Only a range starting at the very right edge survives that clamp with
    // no width — and `outsideDomain` already reports it as off-axis.
    if (end <= x0) continue;
    let tier = tierEnds.findIndex((e) => e <= x0);
    if (tier === -1) tier = tierEnds.length;
    tierEnds[tier] = end;
    ranges.push({ mark, x0, x1: end, tier });
  }
  return { instants, ranges, offscreen };
}

export const fmtMarkTime = (iso: string) => `${iso.slice(0, 19).replace("T", " ")}Z`;

function numbersFor(source: number, numbered: NumberedInstant[]): string {
  const ns = numbered.filter((i) => i.mark.source === source).map((i) => i.n);
  if (ns.length === 0) return "";
  if (ns.length <= 5) return ns.map((n) => `#${n}`).join(", ");
  // Numbering runs across every source in time order, so a source's numbers
  // need not be contiguous — two interleaved sources give #1,#3,#5,… A bare
  // `#1–#11` would name five rules that belong to the other source, so the
  // count leads and the span is stated as the span it is.
  const span = ns[ns.length - 1] - ns[0] + 1;
  if (span === ns.length) return `#${ns[0]}–#${ns[ns.length - 1]}`;
  return `${ns.length} of #${ns[0]}–#${ns[ns.length - 1]}`;
}

/** The interval a time-axis figure actually draws (`lib/timeDomain.ts`). */
export type MarksDomain = readonly [Date, Date];

/** Whether a mark falls entirely outside *domain*, and so is never drawn.
 * Mirrors `layoutMarks`: an instant is placed only inside the axis, and a
 * range is drawn only where its clipped width is positive. */
function outsideDomain(mark: ResolvedMark, domain: MarksDomain): boolean {
  const [lo, hi] = [domain[0].getTime(), domain[1].getTime()];
  if (mark.kind === "instant") {
    const t = Date.parse(mark.at);
    return !Number.isFinite(t) || t < lo || t > hi;
  }
  const start = Date.parse(mark.start);
  const end = Date.parse(mark.end);
  if (!Number.isFinite(start) || !Number.isFinite(end)) return true;
  return end <= lo || start >= hi;
}

/** The "not drawn" clause for one source's marks, empty when all are drawn. */
function offAxisClause(own: ResolvedMark[], domain: MarksDomain | null | undefined): string {
  if (!domain || own.length === 0) return "";
  const n = own.filter((m) => outsideDomain(m, domain)).length;
  if (n === 0) return "";
  return own.length === 1
    ? "; outside the drawn time axis, not drawn"
    : `; ${n} outside the drawn time axis, not drawn`;
}

const plural = (n: number, word: string) => `${n} ${word}${n === 1 ? "" : "s"}`;

/**
 * One caption line per source, in source order — provenance, numbers, cap,
 * and (given the figure's *domain*) how many of the source's marks fall
 * outside the drawn axis. Without that last clause a chart windowed away
 * from its marks captions rules it never draws.
 */
export function markCaptionLines(
  resp: ResolvedMarksResponse,
  domain?: MarksDomain | null,
): string[] {
  const numbered = numberInstants(resp.marks);
  const bySource = new Map<number, ResolvedMark[]>();
  for (const m of resp.marks) bySource.set(m.source, [...(bySource.get(m.source) ?? []), m]);
  return resp.sources.map((s: ResolvedMarkSource) => {
    const own = bySource.get(s.index) ?? [];
    const offAxis = offAxisClause(own, domain);
    switch (s.kind) {
      case "instant": {
        const m = own[0] as ResolvedInstantMark | undefined;
        return `mark ${numbersFor(s.index, numbered)}: "${s.label}" at ${m ? fmtMarkTime(m.at) : "?"} — analyst-placed${offAxis}`;
      }
      case "range": {
        const m = own[0] as ResolvedRangeMark | undefined;
        return `mark: "${s.label}" ${m ? `${fmtMarkTime(m.start)} → ${fmtMarkTime(m.end)}` : ""} — analyst-placed${offAxis}`;
      }
      case "baseline":
        return `marks: baseline "${s.label}" — its baseline window and ${plural(Math.max(0, s.count - 1), "suspect window")}, as declared${offAxis}`;
      default: {
        const origin = s.kind === "view" ? "of saved view" : "matching a filter";
        let line = `marks ${numbersFor(s.index, numbered)}: "${s.label}" — ${plural(s.count, "event")} ${origin}`;
        if (s.overflow)
          line += `; the earliest ${s.shown} drawn (cap ${resp.cap}), ${s.count - s.shown} not drawn`;
        if (s.undated > 0) line += `; ${plural(s.undated, "undated event")} not drawn`;
        return line + offAxis;
      }
    }
  });
}
