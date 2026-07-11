/**
 * Series legend — always present for >= 2 series (per the dataviz skill,
 * identity is never color-alone). A single series needs no legend box (its
 * name is the chart title), so callers simply don't render this for one.
 */
interface LegendEntry {
  label: string;
  color: string;
  muted?: boolean;
  /** Stable identifier reported to `onEntryClick` (defaults to `label`) —
   * lets callers keep display labels decorated (counts, "Other") while
   * click handlers receive the raw value. */
  key?: string;
}

export function Legend({
  entries,
  onEntryClick,
}: {
  entries: LegendEntry[];
  /** When set, entries render as buttons and clicks report the entry key —
   * used by the Visualize page's click-to-filter. */
  onEntryClick?: (key: string, e: React.MouseEvent) => void;
}) {
  if (entries.length < 2) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-xs text-[var(--color-fg-secondary)]">
      {entries.map((e) => {
        const body = (
          <>
            <span
              className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm"
              style={{ backgroundColor: e.color, opacity: e.muted ? 0.5 : 1 }}
            />
            <span className={e.muted ? "text-[var(--color-fg-muted)]" : undefined}>
              {e.label}
            </span>
          </>
        );
        return onEntryClick ? (
          <button
            key={e.label}
            type="button"
            className="flex cursor-pointer items-center gap-1.5 hover:text-[var(--color-fg-primary)]"
            onClick={(evt) => onEntryClick(e.key ?? e.label, evt)}
          >
            {body}
          </button>
        ) : (
          <div key={e.label} className="flex items-center gap-1.5">
            {body}
          </div>
        );
      })}
    </div>
  );
}
