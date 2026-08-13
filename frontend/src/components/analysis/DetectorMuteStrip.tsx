/**
 * DetectorMuteStrip — the rail's top control for taking a method out of the
 * sweep.
 *
 * Some defects are properties of the evidence rather than of the behavior it
 * records: a capture whose clocks disagree makes `timestamp_order` fire on
 * millions of rows, and every one of them is true and none of them is the
 * investigation. Reading past that noise is not triage, so the analyst gets to
 * silence the method rather than dismiss its findings one at a time.
 *
 * Two rules make that safe to offer:
 *
 *   1. The collapsed strip *always* names the count when anything is muted,
 *      and it is the first thing in the rail. An empty findings list must
 *      never be readable as "clear" when it is really "muted".
 *   2. A mute is presentation only. The analysis plan does not consult it, and
 *      the method still runs from the sheet when asked for by name — the same
 *      contract the gate itself is held to (`docs/ANOMALY_DETECTION.md`).
 *
 * The mute is shared, audited server state on the Timeline, so it is spelled
 * out as somebody's decision rather than as a browser quirk the next analyst
 * would have no way to discover.
 */
import { useState } from "react";
import { BellOff, ChevronDown, ChevronRight } from "lucide-react";
import { METHODS } from "./method-registry";
import type { MutedMethods } from "@/hooks/useMutedMethods";
import { cn } from "@/lib/cn";

export function DetectorMuteStrip({ mute }: { mute: MutedMethods }) {
  const [open, setOpen] = useState(false);
  const count = mute.muted.size;

  return (
    <div
      data-testid="detector-mute-strip"
      className={cn(
        "rounded border text-[11px]",
        count > 0
          ? "border-[var(--color-warning)] bg-[var(--color-warning-dim)]"
          : "border-[var(--color-border)]",
      )}
    >
      <button
        data-testid="detector-mute-toggle"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        className="flex w-full items-center gap-1.5 px-2 py-1 text-left text-[var(--color-fg-secondary)] transition-base hover:text-[var(--color-fg-primary)]"
      >
        {open ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
        <span className="flex-1">
          {METHODS.length} detectors
          {count > 0 && (
            <>
              {" · "}
              <b
                data-testid="detector-mute-count"
                className="font-semibold text-[var(--color-warning)]"
              >
                {count} muted
              </b>
            </>
          )}
        </span>
        {count > 0 && <BellOff size={11} className="text-[var(--color-warning)]" />}
      </button>

      {open && (
        <div className="border-t border-[var(--color-border)] px-2 py-1.5">
          <div className="flex flex-wrap gap-1">
            {METHODS.map((m) => {
              const muted = mute.isMuted(m.id);
              return (
                <button
                  key={m.id}
                  data-testid={`mute-chip-${m.id}`}
                  onClick={() => mute.toggle(m.id)}
                  disabled={!mute.canEdit || mute.isSaving}
                  aria-pressed={muted}
                  title={
                    mute.canEdit
                      ? muted
                        ? `${m.label} is muted — click to put it back in the sweep`
                        : `Mute ${m.label}: its findings leave the feed and the histogram. It still runs from Tools.`
                      : "Read-only access — muting changes shared case state"
                  }
                  className={cn(
                    "flex items-center gap-1 rounded-full border px-1.5 py-0.5 text-[10px] transition-base disabled:opacity-50",
                    muted
                      ? "border-[var(--color-warning)] text-[var(--color-fg-muted)] line-through"
                      : "border-[var(--color-border)] text-[var(--color-fg-secondary)] hover:border-[var(--color-border-strong)]",
                  )}
                >
                  <m.icon size={9} />
                  {m.label}
                </button>
              );
            })}
          </div>
          <p className="mt-1.5 text-[10px] text-[var(--color-fg-muted)]">
            A muted detector leaves the feed, the histogram and the grid marks. It is not disabled:
            the gate still considers it, Tools still lists it, and it still runs when asked for by
            name. Muting is shared with everyone on the case and recorded in the audit trail.
          </p>
          {count > 0 && mute.canEdit && (
            <button
              data-testid="unmute-all"
              onClick={mute.unmuteAll}
              disabled={mute.isSaving}
              className="mt-1 rounded border border-[var(--color-border)] px-1.5 py-0.5 text-[10px] text-[var(--color-fg-secondary)] transition-base hover:border-[var(--color-border-strong)] disabled:opacity-50"
            >
              Unmute all
            </button>
          )}
        </div>
      )}
    </div>
  );
}
