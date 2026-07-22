import { useEffect, useRef, useState } from "react";
import { HelpCircle } from "lucide-react";
import { EXPLAINERS, type ExplainerId } from "@/components/viz/lib/explainers";

interface ExplainerPopoverProps {
  id: ExplainerId;
  /** Optional visible label next to the ?-badge (default: badge only). */
  label?: string;
}

/**
 * Teaching popover for a statistic or chart concept — a small ?-badge that
 * opens the centralized what/how-to-read/when-to-distrust copy from
 * `lib/explainers.ts`. Dismissal follows `ChartActionPopover`'s pattern
 * (outside click + Escape). Deliberately a popover, not a tooltip: the copy
 * is several sentences and should stay open while read.
 */
export function ExplainerPopover({ id, label }: ExplainerPopoverProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLSpanElement>(null);
  const explainer = EXPLAINERS[id];

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) setOpen(false);
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
    <span ref={rootRef} className="relative inline-flex items-center gap-1 align-middle">
      <button
        type="button"
        aria-label={`Explain: ${explainer.title}`}
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
        className="inline-flex items-center gap-1 rounded text-[var(--color-fg-muted)] hover:text-[var(--color-accent)] focus-visible:outline focus-visible:outline-1 focus-visible:outline-[var(--color-accent)]"
      >
        <HelpCircle size={12} aria-hidden />
        {label && <span className="text-xs normal-case tracking-normal">{label}</span>}
      </button>
      {open && (
        <div
          role="dialog"
          aria-label={explainer.title}
          className="absolute left-0 top-full z-50 mt-1 w-72 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-3 text-left shadow-lg"
        >
          <div className="mb-1.5 text-xs font-semibold normal-case tracking-normal text-[var(--color-fg-primary)]">
            {explainer.title}
          </div>
          <dl className="flex flex-col gap-1.5 text-xs font-normal normal-case tracking-normal text-[var(--color-fg-secondary)]">
            <div>
              <dt className="font-medium text-[var(--color-fg-primary)]">What it is</dt>
              <dd>{explainer.what}</dd>
            </div>
            <div>
              <dt className="font-medium text-[var(--color-fg-primary)]">How to read it</dt>
              <dd>{explainer.howToRead}</dd>
            </div>
            <div>
              <dt className="font-medium text-[var(--color-fg-primary)]">When to distrust it</dt>
              <dd>{explainer.distrust}</dd>
            </div>
            {explainer.formula && (
              <div>
                <dt className="font-medium text-[var(--color-fg-primary)]">Formula</dt>
                <dd className="font-mono text-[11px]">{explainer.formula}</dd>
              </div>
            )}
          </dl>
        </div>
      )}
    </span>
  );
}
