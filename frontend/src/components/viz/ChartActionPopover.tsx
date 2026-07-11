import { useEffect, useRef } from "react";
import { Link } from "react-router-dom";
import { Filter, FilterX, ExternalLink } from "lucide-react";
import type { ChartValueClick } from "@/components/viz/lib/interaction";
import { mapFieldTokenToFilterKey } from "@/lib/fieldFilters";

interface ChartActionPopoverProps {
  click: ChartValueClick;
  /** Explorer URL with the clicked value(s) applied as include filters. */
  explorerHref: string;
  onFilter: (include: boolean) => void;
  onClose: () => void;
}

/**
 * Action popover for a clicked chart mark — filter in / filter out / open in
 * Explorer. A deliberate two-step (click, then choose) rather than instant
 * filter mutation: chart marks are small, and a misclick that silently
 * rewrites the analyst's filter set is worse than one extra click.
 *
 * Filter-out is offered only for single-value clicks: excluding both halves
 * of a pivot/sankey pair would exclude each value everywhere (exclusions AND
 * per key), which is almost never what "not this cell" means.
 */
export function ChartActionPopover({ click, explorerHref, onFilter, onClose }: ChartActionPopoverProps) {
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const onPointerDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) onClose();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    document.addEventListener("mousedown", onPointerDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onPointerDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [onClose]);

  // Clamp near the viewport edges so the popover never renders off-screen.
  const left = Math.min(click.clientX, window.innerWidth - 240);
  const top = Math.min(click.clientY + 4, window.innerHeight - 140);

  const itemClass =
    "flex w-full items-center gap-2 rounded px-2 py-1.5 text-left text-xs text-[var(--color-fg-primary)] hover:bg-[var(--color-bg-hover)]";

  return (
    <div
      ref={rootRef}
      className="fixed z-50 w-56 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-1.5 shadow-lg"
      style={{ left, top }}
      role="menu"
    >
      <div className="mb-1 border-b border-[var(--color-border)] px-2 pb-1.5 text-xs text-[var(--color-fg-muted)]">
        {click.entries.map(([field, value]) => (
          <div key={field} className="truncate">
            <span className="font-medium text-[var(--color-fg-secondary)]">
              {mapFieldTokenToFilterKey(field)}
            </span>{" "}
            = {value}
          </div>
        ))}
      </div>
      <button type="button" className={itemClass} onClick={() => onFilter(true)}>
        <Filter size={12} /> Filter in
      </button>
      {click.entries.length === 1 && (
        <button type="button" className={itemClass} onClick={() => onFilter(false)}>
          <FilterX size={12} /> Filter out
        </button>
      )}
      <Link to={explorerHref} className={itemClass} onClick={onClose}>
        <ExternalLink size={12} /> Open in Explorer
      </Link>
    </div>
  );
}
