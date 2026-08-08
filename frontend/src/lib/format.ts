/** Formatting helpers for forensic data. */

/** Truncate a hash/UUID for compact display. */
export function truncateHash(value: string | null | undefined, len = 12): string {
  if (!value) return "—";
  return value.length > len ? value.slice(0, len) + "…" : value;
}

/** Format a byte size as a human-readable string. */
export function fmtBytes(bytes: number | null | undefined): string {
  if (bytes == null) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

/** Whole-percent progress, or null when there is no usable denominator —
 * work whose size is not (yet) known, which the UI shows as an indeterminate
 * bar rather than as 0%. */
export function progressPct(
  processed: number | null | undefined,
  total: number | null | undefined,
): number | null {
  if (processed == null || total == null || total <= 0) return null;
  return Math.round((processed / total) * 100);
}

/** Format a transfer rate. Decimal MB/s, matching how transfer speeds are
 * quoted everywhere else — deliberately not `fmtBytes`'s binary MB. */
export function fmtRate(bytesPerSecond: number | null | undefined): string | null {
  if (bytesPerSecond == null || bytesPerSecond <= 0) return null;
  return `${(bytesPerSecond / 1e6).toFixed(1)} MB/s`;
}

/** Format a number with thousands separators. */
export function fmtNum(n: number | null | undefined): string {
  if (n == null) return "—";
  return n.toLocaleString();
}

/** Format a percentage to 1 decimal. */
export function fmtPct(ratio: number | null | undefined): string {
  if (ratio == null) return "—";
  return `${(ratio * 100).toFixed(1)}%`;
}

/**
 * The routine-collapse toolbar stat: "N routine events hidden (X% of
 * timeline)". Percent omitted when the timeline total is unknown; small (but
 * real) shares must never display as a flat 0%.
 */
export function formatRoutineStat(count: number, timelineTotal: number): string {
  const events = `${fmtNum(count)} routine event${count === 1 ? "" : "s"} hidden`;
  if (timelineTotal <= 0) return events;
  const pct = (count / timelineTotal) * 100;
  let pctText: string;
  if (pct > 0 && pct < 0.1) pctText = "<0.1";
  else if (pct < 1) pctText = pct.toFixed(1);
  else pctText = Math.round(pct).toString();
  return `${events} (${pctText}% of timeline)`;
}

/** Percent with adaptive precision: whole percents from 10% up, one decimal below. */
export function fmtPctAdaptive(ratio: number): string {
  const v = ratio * 100;
  return `${v.toFixed(v >= 10 ? 0 : 1)}%`;
}

export function truncate(value: string, max = 120): string {
  if (value.length <= max) return value;
  return value.slice(0, max) + "…";
}

/** Turn a parser name slug into a readable label. */
export function fmtParserName(name: string | null | undefined): string {
  if (!name) return "—";
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

/** Format a cosine distance score to 4 decimal places. */
export function fmtScore(score: number | null | undefined): string {
  if (score == null) return "—";
  return score.toFixed(4);
}

const ANOMALY_FIELD_LABELS: Record<string, string> = {
  artifact: "Artifact",
  timestamp_desc: "Event category",
  display_name: "Display name",
  parser_name: "Parser",
  message: "Message",
  source_file: "Source file",
};

/** Render label for a "Tag N findings" mutation result (tagged count, plus
 * a note for any findings whose representative event no longer exists).
 * Shared between FrequencyView and ValueNoveltyView. */
export function tagResultLabel(
  data: { tagged: number; skipped_unresolved: number } | undefined,
): string {
  if (!data) return "";
  const skipped = data.skipped_unresolved
    ? ` (${data.skipped_unresolved} skipped — event no longer exists)`
    : "";
  return `✓ ${data.tagged} tagged${skipped}`;
}

/** Friendly display label for an anomaly-detector field token (e.g.
 * "attr:user_agent" -> "user_agent", "parser_name" -> "Parser"). Shared
 * across every surface that names a field, so the same token reads
 * identically wherever it's shown. */
export function anomalyFieldLabel(token: string): string {
  if (token.startsWith("attr:")) return token.slice(5);
  return ANOMALY_FIELD_LABELS[token] ?? token;
}
