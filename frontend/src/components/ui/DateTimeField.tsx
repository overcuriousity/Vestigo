import { useEffect, useMemo, useState } from "react";
import { parseISO, isValid } from "date-fns";
import { CalendarDays, ChevronLeft, ChevronRight, X } from "lucide-react";
import { Popover, PopoverContent, PopoverTrigger } from "./Popover";
import { fmtDatetimeInputUtc, parseDatetimeInputUtc } from "@/lib/time";
import { cn } from "@/lib/cn";

interface Props {
  /** UTC ISO string, or "" / undefined when unset. */
  value: string | null | undefined;
  /** Fires with a UTC ISO string, or `undefined` when cleared. */
  onChange: (iso: string | undefined) => void;
  placeholder?: string;
  className?: string;
  ariaLabel?: string;
}

const WEEKDAYS = ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"];
const MONTHS = [
  "January", "February", "March", "April", "May", "June",
  "July", "August", "September", "October", "November", "December",
];

/** Monday-based weekday index (0 = Monday) of a UTC date. */
function mondayIndex(utcDay: number): number {
  return (utcDay + 6) % 7;
}

function partsOf(iso: string | null | undefined): {
  year: number;
  month: number; // 0-11
  day: number;
  hh: string;
  mm: string;
} | null {
  if (!iso) return null;
  const d = parseISO(iso);
  if (!isValid(d)) return null;
  return {
    year: d.getUTCFullYear(),
    month: d.getUTCMonth(),
    day: d.getUTCDate(),
    hh: String(d.getUTCHours()).padStart(2, "0"),
    mm: String(d.getUTCMinutes()).padStart(2, "0"),
  };
}

/**
 * A UTC-only date/time picker replacing the raw native `datetime-local` widget
 * (which renders locale placeholders like "tt.mm.jjjj" and interprets input as
 * browser-local). Value in/out is a UTC ISO string — the whole app is UTC
 * (issue #9). Type `YYYY-MM-DD HH:MM` directly, or pick from the calendar.
 */
export function DateTimeField({ value, onChange, placeholder = "—", className, ariaLabel }: Props) {
  const [open, setOpen] = useState(false);
  const [text, setText] = useState(() => fmtDatetimeInputUtc(value));

  // Keep the text field in sync when the value changes from outside (e.g. a
  // histogram brush fills the row) and the popover isn't being edited.
  useEffect(() => {
    if (!open) setText(fmtDatetimeInputUtc(value));
  }, [value, open]);

  const parts = partsOf(value);
  const now = useMemo(() => {
    const d = new Date();
    return { year: d.getUTCFullYear(), month: d.getUTCMonth(), day: d.getUTCDate() };
  }, []);

  const [view, setView] = useState(() => ({
    year: parts?.year ?? now.year,
    month: parts?.month ?? now.month,
  }));

  // Re-centre the calendar on the selected month when the popover opens.
  useEffect(() => {
    if (open) {
      const p = partsOf(value);
      setView({ year: p?.year ?? now.year, month: p?.month ?? now.month });
    }
  }, [open]); // eslint-disable-line react-hooks/exhaustive-deps

  const hh = parts?.hh ?? "00";
  const mm = parts?.mm ?? "00";

  function emit(y: number, mo: number, day: number, h: string, mi: string) {
    const iso = new Date(Date.UTC(y, mo, day, Number(h), Number(mi))).toISOString();
    onChange(iso);
    setText(fmtDatetimeInputUtc(iso));
  }

  function pickDay(day: number) {
    emit(view.year, view.month, day, hh, mm);
  }

  function setTime(nextHh: string, nextMm: string) {
    const p = parts ?? { year: view.year, month: view.month, day: now.day };
    emit(p.year, p.month, p.day, nextHh, nextMm);
  }

  function commitText() {
    const iso = parseDatetimeInputUtc(text);
    if (iso) {
      onChange(iso);
      setText(fmtDatetimeInputUtc(iso));
    } else if (!text.trim()) {
      onChange(undefined);
    } else {
      // Unparseable — revert to the last good value.
      setText(fmtDatetimeInputUtc(value));
    }
  }

  function stepMonth(delta: number) {
    setView((v) => {
      const m = v.month + delta;
      const year = v.year + Math.floor(m / 12);
      const month = ((m % 12) + 12) % 12;
      return { year, month };
    });
  }

  // Build the 6×7 calendar grid (leading blanks for alignment).
  const firstDayIdx = mondayIndex(new Date(Date.UTC(view.year, view.month, 1)).getUTCDay());
  const daysInMonth = new Date(Date.UTC(view.year, view.month + 1, 0)).getUTCDate();
  const cells: (number | null)[] = [
    ...Array(firstDayIdx).fill(null),
    ...Array.from({ length: daysInMonth }, (_, i) => i + 1),
  ];
  while (cells.length % 7 !== 0) cells.push(null);

  const display = fmtDatetimeInputUtc(value);

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          type="button"
          aria-label={ariaLabel}
          className={cn(
            "flex h-8 w-full items-center gap-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 text-left text-xs transition-colors hover:border-[var(--color-border-strong)] focus:outline-none focus:ring-1 focus:ring-[var(--color-accent)]",
            className,
          )}
        >
          <CalendarDays size={12} className="shrink-0 text-[var(--color-fg-muted)]" />
          <span
            className={cn(
              "min-w-0 flex-1 truncate font-mono tabular-nums",
              display ? "text-[var(--color-fg-primary)]" : "text-[var(--color-fg-muted)]",
            )}
          >
            {display || placeholder}
          </span>
          {display && (
            <span
              role="button"
              tabIndex={-1}
              aria-label="Clear"
              className="shrink-0 rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-danger)]"
              onClick={(e) => {
                e.stopPropagation();
                onChange(undefined);
                setText("");
              }}
            >
              <X size={11} />
            </span>
          )}
          <span className="shrink-0 text-[9px] uppercase tracking-wide text-[var(--color-fg-muted)]">
            utc
          </span>
        </button>
      </PopoverTrigger>

      <PopoverContent className="w-64 p-2.5" align="start">
        {/* Typed input */}
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={commitText}
          onKeyDown={(e) => {
            if (e.key === "Enter") {
              commitText();
              setOpen(false);
            }
          }}
          placeholder="YYYY-MM-DD HH:MM"
          className="mb-2 h-7 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 font-mono text-xs tabular-nums"
        />

        {/* Month header */}
        <div className="mb-1.5 flex items-center justify-between">
          <button
            type="button"
            onClick={() => stepMonth(-1)}
            className="rounded p-1 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
            aria-label="Previous month"
          >
            <ChevronLeft size={14} />
          </button>
          <span className="text-xs font-medium text-[var(--color-fg-primary)]">
            {MONTHS[view.month]} {view.year}
          </span>
          <button
            type="button"
            onClick={() => stepMonth(1)}
            className="rounded p-1 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
            aria-label="Next month"
          >
            <ChevronRight size={14} />
          </button>
        </div>

        {/* Weekday labels */}
        <div className="grid grid-cols-7 gap-0.5 text-center text-[10px] text-[var(--color-fg-muted)]">
          {WEEKDAYS.map((w) => (
            <div key={w} className="py-0.5">{w}</div>
          ))}
        </div>

        {/* Day grid */}
        <div className="grid grid-cols-7 gap-0.5">
          {cells.map((day, i) => {
            if (day === null) return <div key={i} />;
            const selected =
              parts?.year === view.year && parts?.month === view.month && parts?.day === day;
            const isToday =
              now.year === view.year && now.month === view.month && now.day === day;
            return (
              <button
                key={i}
                type="button"
                onClick={() => pickDay(day)}
                className={cn(
                  "h-7 rounded text-xs tabular-nums transition-colors",
                  selected
                    ? "bg-[var(--color-accent)] text-white"
                    : "text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)]",
                  isToday && !selected && "ring-1 ring-inset ring-[var(--color-border-strong)]",
                )}
              >
                {day}
              </button>
            );
          })}
        </div>

        {/* Time + shortcuts */}
        <div className="mt-2 flex items-center gap-1.5 border-t border-[var(--color-border)] pt-2">
          <input
            type="number"
            min={0}
            max={23}
            value={hh}
            onChange={(e) => setTime(String(Math.min(23, Math.max(0, Number(e.target.value) || 0))).padStart(2, "0"), mm)}
            className="h-7 w-11 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1 text-center font-mono text-xs tabular-nums"
            aria-label="Hour (UTC)"
          />
          <span className="text-xs text-[var(--color-fg-muted)]">:</span>
          <input
            type="number"
            min={0}
            max={59}
            value={mm}
            onChange={(e) => setTime(hh, String(Math.min(59, Math.max(0, Number(e.target.value) || 0))).padStart(2, "0"))}
            className="h-7 w-11 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-1 text-center font-mono text-xs tabular-nums"
            aria-label="Minute (UTC)"
          />
          <span className="text-[9px] uppercase tracking-wide text-[var(--color-fg-muted)]">utc</span>
          <span className="flex-1" />
          <button
            type="button"
            onClick={() => {
              onChange(undefined);
              setText("");
            }}
            className="rounded px-1.5 py-1 text-[10px] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]"
          >
            Clear
          </button>
        </div>
      </PopoverContent>
    </Popover>
  );
}
