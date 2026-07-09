import { formatDistanceToNow, parseISO, isValid } from "date-fns";

/** Shared parse-and-format core for every UTC timestamp formatter below —
 * UTC is the application-wide standard (issue #9); every timestamp an
 * analyst reads or types anywhere in the UI means UTC, so two analysts in
 * different timezones always see identical values. Invalid/unparseable
 * input falls back to the raw string rather than throwing. */
function formatUtc(value: string | null | undefined, fmt: (d: Date) => string): string {
  if (!value) return "—";
  try {
    const d = parseISO(value);
    if (!isValid(d)) return value;
    return fmt(d);
  } catch {
    return value;
  }
}

/** Mirrors cli/progress.py's `_fmt_duration` so web ETAs read like the CLI. */
export function fmtDuration(seconds: number): string {
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rs = s % 60;
  if (m < 60) return `${m}m ${String(rs).padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return `${h}h ${String(rm).padStart(2, "0")}m ${String(rs).padStart(2, "0")}s`;
}

/** Format a timestamp string for display in the event grid. */
export function fmtTimestamp(value: string | null | undefined): string {
  return formatUtc(value, (d) => d.toISOString().slice(0, 19).replace("T", " "));
}

/** Format a timestamp with timezone for the detail panel. */
export function fmtTimestampFull(value: string | null | undefined): string {
  return formatUtc(value, (d) => {
    const year = d.getUTCFullYear();
    const month = String(d.getUTCMonth() + 1).padStart(2, "0");
    const day = String(d.getUTCDate()).padStart(2, "0");
    const hours = String(d.getUTCHours()).padStart(2, "0");
    const minutes = String(d.getUTCMinutes()).padStart(2, "0");
    const seconds = String(d.getUTCSeconds()).padStart(2, "0");
    const ms = String(d.getUTCMilliseconds()).padStart(3, "0");
    return `${year}-${month}-${day} ${hours}:${minutes}:${seconds}.${ms} UTC`;
  });
}

const MONTH_ABBR = [
  "Jan", "Feb", "Mar", "Apr", "May", "Jun",
  "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
];

/** Compact UTC timestamp for anomaly-panel finding rows (e.g. "Jul 1, 14:30 UTC"). */
export function fmtTimestampCompactUtc(value: string | null | undefined): string {
  return formatUtc(value, (d) => {
    const month = MONTH_ABBR[d.getUTCMonth()];
    const day = d.getUTCDate();
    const hours = String(d.getUTCHours()).padStart(2, "0");
    const minutes = String(d.getUTCMinutes()).padStart(2, "0");
    return `${month} ${day}, ${hours}:${minutes} UTC`;
  });
}

/** Relative time ago for ingest_time / created_at. */
export function fmtRelative(value: string | null | undefined): string {
  if (!value) return "—";
  try {
    const d = parseISO(value);
    if (!isValid(d)) return value;
    return formatDistanceToNow(d, { addSuffix: true });
  } catch {
    return value;
  }
}

/**
 * Convert a `<input type="datetime-local">` value ("YYYY-MM-DDTHH:mm") to a
 * UTC ISO string, treating the typed wall-clock time as UTC. The widget has
 * no timezone of its own — pairing this with `isoToDatetimeLocalUtc` keeps
 * the whole round trip in UTC (the application-wide standard, issue #9),
 * where `new Date(value)` would have interpreted the input as browser-local
 * and silently shifted it by the local offset.
 */
export function datetimeLocalToUtcIso(value: string): string | undefined {
  if (!value) return undefined;
  const d = new Date(`${value}:00.000Z`);
  return isValid(d) ? d.toISOString() : undefined;
}

/** Render a UTC ISO string as a `datetime-local` widget value
 * ("YYYY-MM-DDTHH:mm"), in UTC — inverse of `datetimeLocalToUtcIso`. */
export function isoToDatetimeLocalUtc(value: string | null | undefined): string {
  if (!value) return "";
  const d = parseISO(value);
  if (!isValid(d)) return "";
  return d.toISOString().slice(0, 16);
}

/** Render a UTC ISO string as a compact `"YYYY-MM-DD HH:MM"` for the
 * `DateTimeField` trigger/text input (UTC, space separator). "" for empty. */
export function fmtDatetimeInputUtc(value: string | null | undefined): string {
  if (!value) return "";
  const d = parseISO(value);
  if (!isValid(d)) return "";
  return d.toISOString().slice(0, 16).replace("T", " ");
}

/** Parse a typed `"YYYY-MM-DD HH:MM"` (or with `T`, and optional seconds),
 * interpreted as UTC (the app-wide standard), into a UTC ISO string. Returns
 * `undefined` for blank or unparseable input so callers can clear the value. */
export function parseDatetimeInputUtc(text: string): string | undefined {
  const t = text.trim();
  if (!t) return undefined;
  const m = t.match(/^(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})(?::(\d{2}))?$/);
  if (!m) return undefined;
  const [, y, mo, day, h, mi, s] = m;
  const d = new Date(`${y}-${mo}-${day}T${h}:${mi}:${s ?? "00"}.000Z`);
  return isValid(d) ? d.toISOString() : undefined;
}

/** Compute the `[start, end]` ISO bounds of a ±`minutes` window around `ts`,
 * for the Explorer's "context query" pivot. Returns `null` for an
 * unparseable `ts` so callers can no-op instead of filtering to "Invalid
 * Date". */
export function contextWindow(ts: string, minutes: number): { start: string; end: string } | null {
  const anchor = new Date(ts).getTime();
  if (Number.isNaN(anchor)) return null;
  return {
    start: new Date(anchor - minutes * 60_000).toISOString(),
    end: new Date(anchor + minutes * 60_000).toISOString(),
  };
}
