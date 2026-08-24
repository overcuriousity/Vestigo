/**
 * FindingEvidence — the picture of the finding, where the payload contains one.
 *
 * The rule this file is built on: **draw only numbers the response carries.**
 * The design mockup this surface came from showed an evenly-spaced cadence
 * chart for interval findings, plotted from per-event timestamps the API does
 * not return. In a forensic tool a plausible-looking chart assembled from
 * numbers nobody measured is worse than no chart — so the shapes without real
 * comparands (rare values, value combos, log templates) render nothing at all,
 * deliberately.
 *
 * Visual grammar, applied to every case:
 *
 * - Two marks, one scale. A reference mark in a recessive neutral, the observed
 *   one in the anomaly accent. No dual scales, ever — the whole claim is that
 *   two comparable numbers differ.
 * - Both marks are directly labeled, so no legend box is needed and nothing is
 *   carried by color alone.
 * - Labels and values wear text tokens; the mark carries the identity. Every
 *   color is a theme token, so light and dark are the same code.
 */
import type { MethodResult } from "@/api/analysis";
import { isTemplateRow } from "@/api/analysis";
import { truncate } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

const REFERENCE = "var(--color-border-strong)";
const OBSERVED = "var(--color-anomaly,var(--color-warning))";

/** One labeled horizontal bar. Rounded data end, 2px gap between rows. */
function Bar({
  label,
  value,
  display,
  max,
  color,
}: {
  label: string;
  value: number;
  display: string;
  max: number;
  color: string;
}) {
  // A zero-width bar is invisible and reads as "no data" rather than "zero", so
  // a real zero still gets the rounded cap's worth of ink.
  const width = max > 0 ? Math.max((value / max) * 100, value > 0 ? 1.5 : 0) : 0;
  return (
    <div className="flex items-center gap-2">
      <span className="w-24 shrink-0 truncate text-xs text-[var(--color-fg-muted)]" title={label}>
        {label}
      </span>
      <span className="h-2 min-w-0 flex-1 overflow-hidden rounded-sm bg-[var(--color-bg-base)]">
        <span
          className="block h-full rounded-sm"
          style={{ width: `${width}%`, background: color }}
        />
      </span>
      <span className="w-20 shrink-0 text-right font-mono text-xs text-[var(--color-fg-secondary)]">
        {display}
      </span>
    </div>
  );
}

function TwoBars({
  reference,
  observed,
}: {
  reference: { label: string; value: number; display: string };
  observed: { label: string; value: number; display: string };
}) {
  const max = Math.max(reference.value, observed.value, 0);
  return (
    <div className="space-y-0.5">
      <Bar {...reference} max={max} color={REFERENCE} />
      <Bar {...observed} max={max} color={OBSERVED} />
    </div>
  );
}

/**
 * A learned band with the out-of-band value marked.
 *
 * The axis is widened past whichever side the value falls on, so the marker is
 * always inside the drawing and its distance from the band edge is to scale —
 * clamping it to the edge would show an in-band value.
 */
function BandStrip({
  lower,
  upper,
  value,
  unit,
}: {
  lower: number;
  upper: number;
  value: number;
  unit?: string;
}) {
  const lo = Math.min(lower, value);
  const hi = Math.max(upper, value);
  const pad = (hi - lo) * 0.12 || 1;
  const min = lo - pad;
  const span = hi + pad - min || 1;
  const x = (n: number) => ((n - min) / span) * 100;
  const fmt = (n: number) => `${Number.isInteger(n) ? n : n.toFixed(2)}${unit ? ` ${unit}` : ""}`;
  // Kept off the very edge so the centered label cannot be clipped by the
  // panel. The axis is padded by 12% either side, so a real marker never
  // reaches this clamp — it exists for degenerate bands, not for the drawing.
  const labelX = Math.min(Math.max(x(value), 8), 92);
  const caption = `Learned band ${fmt(lower)} to ${fmt(upper)}; this value ${fmt(value)}`;

  // Laid out in CSS rather than SVG. The marker's label has to sit *at* the
  // marker: in a `justify-between` row it was pinned to the middle of the
  // track regardless, so an out-of-band value at 10% was captioned at 50% —
  // right about where the band starts, which reads as in-band and is the exact
  // misread the to-scale marker exists to prevent. It also made the row read
  // as an axis running 1.97 → 0.72 → 3.18, which is not an ordering. Drawing
  // in CSS additionally keeps the marker round: a `preserveAspectRatio="none"`
  // SVG stretched it into a smear the width of the panel.
  return (
    <div role="img" aria-label={caption} title={caption}>
      <div className="relative h-4">
        <span
          className="absolute -translate-x-1/2 whitespace-nowrap font-mono text-xs text-[var(--color-anomaly,var(--color-warning))]"
          style={{ left: `${labelX}%` }}
        >
          {fmt(value)}
        </span>
      </div>
      <div className="relative h-5">
        <div className="absolute inset-x-0 top-1/2 h-px -translate-y-1/2 bg-[var(--color-border)]" />
        <div
          className="absolute top-1/2 h-2 -translate-y-1/2 rounded-sm"
          style={{
            left: `${x(lower)}%`,
            width: `${Math.max(x(upper) - x(lower), 0.5)}%`,
            background: REFERENCE,
          }}
        />
        <div
          className="absolute top-1/2 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full"
          style={{ left: `${x(value)}%`, background: OBSERVED }}
        />
      </div>
      <div className="flex justify-between font-mono text-xs text-[var(--color-fg-muted)]">
        <span>band {fmt(lower)}</span>
        <span>{fmt(upper)}</span>
      </div>
    </div>
  );
}

/** The value with its never-before-seen characters picked out. */
function NovelChars({ value, novel }: { value: string; novel: string[] }) {
  const set = new Set(novel);
  return (
    <p className="break-all font-mono text-xs text-[var(--color-fg-secondary)]">
      {[...truncate(value, 160)].map((ch, i) =>
        set.has(ch) ? (
          <mark
            key={i}
            className="rounded-sm bg-[var(--color-anomaly-dim)] px-0.5 text-[var(--color-anomaly,var(--color-warning))]"
          >
            {ch}
          </mark>
        ) : (
          <span key={i}>{ch}</span>
        ),
      )}
    </p>
  );
}

/** The n-gram, oldest → newest, so the *order* is what the eye reads. */
function Ngram({ values }: { values: string[] }) {
  return (
    <div className="flex flex-wrap items-center gap-1">
      {values.map((v, i) => (
        <span key={i} className="flex items-center gap-1">
          {i > 0 && <span className="text-xs text-[var(--color-fg-muted)]">→</span>}
          <span className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-primary)]">
            {truncate(v, 40)}
          </span>
        </span>
      ))}
    </div>
  );
}

/**
 * The evidence figure for one finding, or `null` when the payload has nothing
 * real to draw. Callers render the "Evidence" heading only when this returns
 * something — see `hasEvidence`.
 */
export function FindingEvidence({ finding }: { finding: MethodResult }) {
  if (isTemplateRow(finding)) return null;

  switch (finding.type) {
    case "frequency":
      return (
        <TwoBars
          reference={{
            label: "expected",
            value: finding.expected,
            display: finding.expected.toFixed(1),
          }}
          observed={{
            label: "observed",
            value: finding.observed,
            display: String(finding.observed),
          }}
        />
      );
    case "proportion_shift":
      return (
        <TwoBars
          reference={{
            label: "baseline share",
            value: finding.baseline_rate,
            display: `${(finding.baseline_rate * 100).toFixed(2)}%`,
          }}
          observed={{
            label: "suspect share",
            value: finding.window_rate,
            display: `${(finding.window_rate * 100).toFixed(2)}%`,
          }}
        />
      );
    case "value_distribution_drift":
      // Event counts on each side of the test — the population the statistic
      // was computed over, which is the checkable part of a whole-field claim.
      return (
        <TwoBars
          reference={{
            label: "baseline events",
            value: finding.baseline_n,
            display: String(finding.baseline_n),
          }}
          observed={{
            label: "suspect events",
            value: finding.window_n,
            display: String(finding.window_n),
          }}
        />
      );
    case "interval_periodicity": {
      const base = finding.baseline_median_interval;
      const window = finding.window_median_interval;
      // Below two occurrences there is no interval to take a median of, and the
      // API says so with null. Fall back to the occurrence counts, which are
      // always real, rather than drawing a gap of zero.
      if (base === null || window === null) {
        return (
          <TwoBars
            reference={{
              label: "baseline count",
              value: finding.baseline_count,
              display: String(finding.baseline_count),
            }}
            observed={{
              label: "suspect count",
              value: finding.count,
              display: String(finding.count),
            }}
          />
        );
      }
      return (
        <TwoBars
          reference={{
            label: "baseline gap",
            value: base,
            display: `${base.toFixed(1)}s`,
          }}
          observed={{
            label: "suspect gap",
            value: window,
            display: `${window.toFixed(1)}s`,
          }}
        />
      );
    }
    case "numeric_range":
      return (
        <BandStrip lower={finding.lower} upper={finding.upper} value={finding.value} />
      );
    case "entropy":
      return (
        <BandStrip
          lower={finding.lower}
          upper={finding.upper}
          value={finding.entropy}
          unit="bits"
        />
      );
    case "charset":
      return <NovelChars value={finding.value} novel={finding.novel_chars} />;
    case "sequence_novelty":
    case "sequence_motif":
      return <Ngram values={finding.values} />;
    case "timestamp_order":
      return (
        <div className="flex flex-wrap items-center gap-2 font-mono text-xs">
          <span className="text-[var(--color-fg-muted)]">{fmtTs(finding.prev_timestamp)}</span>
          <span className="text-[var(--color-fg-muted)]">→</span>
          <span className="text-[var(--color-anomaly,var(--color-warning))]">
            {fmtTs(finding.timestamp)}
          </span>
          <span className="text-[var(--color-fg-secondary)]">
            ({finding.skew_seconds.toFixed(1)}s backwards, line {finding.line_number})
          </span>
        </div>
      );
    // Deliberately no figure: a rare value's rarity IS its score, and a combo's
    // claim is the co-occurrence itself. Neither carries a second number to
    // compare against, and inventing one would be the anti-pattern this whole
    // module is written around.
    case "value_novelty":
    case "value_combo":
      return null;
  }
}
