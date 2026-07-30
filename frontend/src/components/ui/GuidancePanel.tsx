import { ChevronDown, ChevronRight, Compass } from "lucide-react";
import { guidance, type GuidanceId } from "@/lib/guidance";
import { useUiStore } from "@/stores/ui";

interface Props {
  /** Registry key in `lib/guidance.tsx`, which supplies both title and body. */
  id: GuidanceId;
}

/**
 * Muted, collapsible guidance side-content (issue #11). Deliberately
 * low-contrast and never modal or blocking — a hint in the margins.
 *
 * The panel owns no copy: it takes an id and reads the registry, so guidance
 * wording cannot be inlined at a call site without a type error. Collapse state
 * lives in the UI store, which makes it restorable — "Show guidance again" in
 * Settings clears it and every mounted panel re-expands.
 */
export function GuidancePanel({ id }: Props) {
  const { title, body } = guidance[id];
  const collapsed = useUiStore((s) => s.collapsedGuidance[id] ?? false);
  const setCollapsed = useUiStore((s) => s.setGuidanceCollapsed);

  return (
    <div
      data-testid="guidance-panel"
      className="rounded-lg border border-dashed border-[var(--color-border)] bg-transparent px-4 py-3"
    >
      <button
        type="button"
        onClick={() => setCollapsed(id, !collapsed)}
        aria-expanded={!collapsed}
        className="flex w-full items-center gap-2 text-left text-xs font-medium uppercase tracking-wider text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)] transition-base"
      >
        <Compass size={12} className="shrink-0 opacity-60" />
        <span className="flex-1">{title}</span>
        {collapsed ? <ChevronRight size={12} /> : <ChevronDown size={12} />}
      </button>
      {!collapsed && (
        <div className="mt-2 text-xs leading-relaxed text-[var(--color-fg-muted)]">{body}</div>
      )}
    </div>
  );
}
