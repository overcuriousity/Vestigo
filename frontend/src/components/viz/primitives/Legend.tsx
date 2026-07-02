/**
 * Series legend — always present for >= 2 series (per the dataviz skill,
 * identity is never color-alone). A single series needs no legend box (its
 * name is the chart title), so callers simply don't render this for one.
 */
interface LegendEntry {
  label: string;
  color: string;
  muted?: boolean;
}

export function Legend({ entries }: { entries: LegendEntry[] }) {
  if (entries.length < 2) return null;
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 px-1 text-xs text-[var(--color-fg-secondary)]">
      {entries.map((e) => (
        <div key={e.label} className="flex items-center gap-1.5">
          <span
            className="inline-block h-2.5 w-2.5 shrink-0 rounded-sm"
            style={{ backgroundColor: e.color, opacity: e.muted ? 0.5 : 1 }}
          />
          <span className={e.muted ? "text-[var(--color-fg-muted)]" : undefined}>
            {e.label}
          </span>
        </div>
      ))}
    </div>
  );
}
