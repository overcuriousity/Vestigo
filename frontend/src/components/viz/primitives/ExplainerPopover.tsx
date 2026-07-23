import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { HelpCircle } from "lucide-react";
import { EXPLAINERS, type ExplainerId } from "@/components/viz/lib/explainers";

interface ExplainerPopoverProps {
  id: ExplainerId;
  /** Optional visible label next to the ?-badge (default: badge only). */
  label?: string;
}

/** Popover width in px — must match the `w-72` (18rem) class below. */
const POPOVER_WIDTH = 288;
/** Gap between the badge and the popover, and the viewport-edge margin. */
const GAP = 6;

interface Coords {
  left: number;
  top: number;
}

/**
 * Teaching popover for a statistic or chart concept — a small ?-badge that
 * opens the centralized what/how-to-read/when-to-distrust copy from
 * `lib/explainers.ts`. Dismissal follows `ChartActionPopover`'s pattern
 * (outside click + Escape). Deliberately a popover, not a tooltip: the copy
 * is several sentences and should stay open while read.
 *
 * The panel is portaled to `document.body` with `position: fixed`, the same
 * escape `ChartActionPopover` uses: badges sit inside chart cards and stat
 * strips that clip their overflow and open their own stacking contexts, so an
 * `absolute` panel was getting cut off or painted behind neighbouring marks.
 * Fixed + portal + viewport clamping keeps it whole and on top wherever the
 * badge lives.
 */
export function ExplainerPopover({ id, label }: ExplainerPopoverProps) {
  const [open, setOpen] = useState(false);
  const [coords, setCoords] = useState<Coords | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const explainer = EXPLAINERS[id];

  // Position the panel against the badge, flipping above when there is no room
  // below and clamping to the viewport so it never renders off-screen. Runs
  // after layout (so the panel's real height is known) and again on any
  // scroll/resize while open.
  useLayoutEffect(() => {
    if (!open) return;
    const place = () => {
      const trigger = buttonRef.current?.getBoundingClientRect();
      if (!trigger) return;
      const panelHeight = dialogRef.current?.offsetHeight ?? 0;
      const left = Math.max(
        GAP,
        Math.min(trigger.left, window.innerWidth - POPOVER_WIDTH - GAP),
      );
      const below = trigger.bottom + GAP;
      const fitsBelow = below + panelHeight <= window.innerHeight - GAP;
      const top = fitsBelow
        ? below
        : Math.max(GAP, trigger.top - GAP - panelHeight);
      setCoords({ left, top });
    };
    place();
    window.addEventListener("scroll", place, true);
    window.addEventListener("resize", place);
    return () => {
      window.removeEventListener("scroll", place, true);
      window.removeEventListener("resize", place);
    };
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      const target = e.target as Node;
      if (buttonRef.current?.contains(target)) return;
      if (dialogRef.current?.contains(target)) return;
      setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  return (
    <span className="relative inline-flex items-center gap-1 align-middle">
      <button
        ref={buttonRef}
        type="button"
        aria-label={`Explain: ${explainer.title}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 rounded text-[var(--color-fg-muted)] hover:text-[var(--color-accent)] focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--color-accent)]"
      >
        <HelpCircle size={12} aria-hidden />
        {label && <span className="text-xs normal-case tracking-normal">{label}</span>}
      </button>
      {open &&
        createPortal(
          <div
            ref={dialogRef}
            role="dialog"
            aria-label={explainer.title}
            style={{ left: coords?.left ?? 0, top: coords?.top ?? 0, width: POPOVER_WIDTH }}
            className={`fixed z-50 max-w-[calc(100vw-1rem)] rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 text-left shadow-lg ${
              coords ? "" : "invisible"
            }`}
          >
            <div className="mb-1.5 text-xs font-semibold normal-case tracking-normal text-[var(--color-fg-primary)]">
              {explainer.title}
            </div>
            <dl className="flex flex-col gap-1.5 text-xs font-normal normal-case tracking-normal text-[var(--color-fg-secondary)]">
              <div>
                <dt className="font-medium text-[var(--color-fg-primary)]">What it is</dt>
                <dd className="break-words">{explainer.what}</dd>
              </div>
              <div>
                <dt className="font-medium text-[var(--color-fg-primary)]">How to read it</dt>
                <dd className="break-words">{explainer.howToRead}</dd>
              </div>
              <div>
                <dt className="font-medium text-[var(--color-fg-primary)]">When to distrust it</dt>
                <dd className="break-words">{explainer.distrust}</dd>
              </div>
              {explainer.formula && (
                <div>
                  <dt className="font-medium text-[var(--color-fg-primary)]">Formula</dt>
                  <dd className="whitespace-pre-wrap break-words font-mono text-[11px]">
                    {explainer.formula}
                  </dd>
                </div>
              )}
            </dl>
          </div>,
          document.body,
        )}
    </span>
  );
}
