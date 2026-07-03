import { useState, type ReactNode } from "react";
import { ChevronDown, ChevronRight, Compass } from "lucide-react";

const STORAGE_PREFIX = "tsig-guidance-";

function readCollapsed(id: string): boolean {
  try {
    return localStorage.getItem(STORAGE_PREFIX + id) === "collapsed";
  } catch {
    return false;
  }
}

function writeCollapsed(id: string, collapsed: boolean): void {
  try {
    if (collapsed) localStorage.setItem(STORAGE_PREFIX + id, "collapsed");
    else localStorage.removeItem(STORAGE_PREFIX + id);
  } catch {
    // localStorage unavailable (private mode) — collapse state just won't persist.
  }
}

interface Props {
  /** Stable identifier; the collapsed state persists per id in localStorage. */
  id: string;
  title: string;
  children: ReactNode;
}

/**
 * Muted, collapsible guidance side-content (issue #11). Deliberately
 * low-contrast and never modal or blocking — a hint in the margins that an
 * analyst can fold away permanently.
 */
export function GuidancePanel({ id, title, children }: Props) {
  const [collapsed, setCollapsed] = useState(() => readCollapsed(id));

  const toggle = () => {
    setCollapsed((c) => {
      writeCollapsed(id, !c);
      return !c;
    });
  };

  return (
    <div
      data-testid="guidance-panel"
      className="rounded-lg border border-dashed border-[var(--color-border)] bg-transparent px-4 py-3"
    >
      <button
        type="button"
        onClick={toggle}
        aria-expanded={!collapsed}
        className="flex w-full items-center gap-2 text-left text-xs font-medium uppercase tracking-wider text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)] transition-base"
      >
        <Compass size={12} className="shrink-0 opacity-60" />
        <span className="flex-1">{title}</span>
        {collapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
      </button>
      {!collapsed && (
        <div className="mt-2 text-xs leading-relaxed text-[var(--color-fg-muted)]">
          {children}
        </div>
      )}
    </div>
  );
}
