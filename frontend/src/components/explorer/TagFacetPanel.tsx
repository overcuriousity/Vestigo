import { Ban, Check } from "lucide-react";

interface Props {
  /** All known tag values for this timeline (from the merged tags endpoint). */
  tags: string[];
  include: string[];
  exclude: string[];
  onChange: (include: string[], exclude: string[]) => void;
}

/**
 * Clickable tag facet panel — cycles a tag through none → include → exclude → none
 * on each click. Include and exclude are independent sets so any number of tags
 * can be in either state simultaneously.
 */
export function TagFacetPanel({ tags, include, exclude, onChange }: Props) {
  const cycle = (tag: string) => {
    const isIncluded = include.includes(tag);
    const isExcluded = exclude.includes(tag);
    if (!isIncluded && !isExcluded) {
      onChange([...include, tag], exclude);
    } else if (isIncluded) {
      onChange(include.filter((t) => t !== tag), [...exclude, tag]);
    } else {
      onChange(include, exclude.filter((t) => t !== tag));
    }
  };

  if (tags.length === 0) {
    return (
      <p className="text-xs text-[var(--color-fg-muted)]">No tags in this timeline.</p>
    );
  }

  return (
    <div className="flex max-h-40 flex-wrap gap-1 overflow-y-auto">
      {tags.map((tag) => {
        const isIncluded = include.includes(tag);
        const isExcluded = exclude.includes(tag);
        return (
          <button
            key={tag}
            type="button"
            onClick={() => cycle(tag)}
            title={
              isIncluded
                ? "Included — click to exclude"
                : isExcluded
                  ? "Excluded — click to clear"
                  : "Click to include"
            }
            className={`inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs transition-base ${
              isIncluded
                ? "border-[var(--color-info)]/30 bg-[var(--color-info-dim)] text-[var(--color-info)]"
                : isExcluded
                  ? "border-[var(--color-danger)]/30 bg-[var(--color-danger-dim)] text-[var(--color-danger)]"
                  : "border-[var(--color-border)] bg-[var(--color-bg-active)] text-[var(--color-fg-secondary)] hover:border-[var(--color-fg-muted)]"
            }`}
          >
            {isIncluded && <Check size={10} />}
            {isExcluded && <Ban size={10} />}
            {tag}
          </button>
        );
      })}
    </div>
  );
}
