import { X } from "lucide-react";
import { Tooltip } from "@/components/ui/Tooltip";
import type { EventFilters, FieldMatchMode } from "@/api/types";

interface Props {
  filters: EventFilters;
  /** Omit for a read-only chip set (no remove buttons) — e.g. the agent
   * panel's inherited-filters bar, where editing stays in the Explorer. */
  onRemove?: (key: keyof EventFilters | string, fieldKey?: string, value?: string) => void;
}

interface Chip {
  label: string;
  value: string;
  onRemove?: () => void;
  variant?: "include" | "exclude" | "neutral";
  /** Non-exact match mode of a field filter/exclusion — rendered as a badge. */
  mode?: FieldMatchMode;
}

const MODE_BADGE: Record<FieldMatchMode, { label: string; tooltip: string }> = {
  wildcard: { label: "*", tooltip: "Wildcard match: * = any run, ? = one char (case-insensitive)" },
  regex: { label: ".*", tooltip: "RE2 regular expression (case-sensitive)" },
  // Never rendered: an empty-mode chip states its meaning in words instead,
  // because there is no value for a badge to qualify. Present so the record
  // stays exhaustive over FieldMatchMode.
  empty: { label: "∅", tooltip: "Presence filter — no value at all" },
};

export function FilterChips({ filters, onRemove }: Props) {
  const chips: Chip[] = [];

  /** A chip's remove handler, or undefined in the read-only chip set. */
  const remove = (key: keyof EventFilters | string, fieldKey?: string, value?: string) =>
    onRemove ? () => onRemove(key, fieldKey, value) : undefined;

  if (filters.q)
    chips.push({
      label: "search",
      value: filters.q,
      onRemove: remove("q"),
      variant: "neutral",
    });
  if (filters.artifact)
    chips.push({
      label: "artifact",
      value: filters.artifact,
      onRemove: remove("artifact"),
      variant: "include",
    });
  for (const a of filters.artifacts ?? []) {
    chips.push({
      label: "artifact",
      value: a,
      onRemove: remove("artifacts", undefined, a),
      variant: "include",
    });
  }
  if (filters.sourceId)
    chips.push({
      label: "sourceId",
      value: filters.sourceId,
      onRemove: remove("sourceId"),
      variant: "include",
    });
  if (filters.tag)
    chips.push({
      label: "tag",
      value: filters.tag,
      onRemove: remove("tag"),
      variant: "include",
    });
  for (const t of filters.tagsInclude ?? []) {
    chips.push({
      label: "tag",
      value: t,
      onRemove: remove("tagsInclude", undefined, t),
      variant: "include",
    });
  }
  for (const t of filters.tagsExclude ?? []) {
    chips.push({
      label: "!tag",
      value: t,
      onRemove: remove("tagsExclude", undefined, t),
      variant: "exclude",
    });
  }
  for (const t of filters.annotated ?? []) {
    chips.push({
      label: "flagged",
      value: t === "tag" && filters.annotationTagValue ? `tag:${filters.annotationTagValue}` : t,
      onRemove: remove("annotated", undefined, t),
      variant: "include",
    });
  }
  if (filters.start)
    chips.push({
      label: "from",
      value: filters.start.replace("T", " ").replace(/\.\d+Z$/, "Z"),
      onRemove: remove("start"),
      variant: "neutral",
    });
  if (filters.end)
    chips.push({
      label: "to",
      value: filters.end.replace("T", " ").replace(/\.\d+Z$/, "Z"),
      onRemove: remove("end"),
      variant: "neutral",
    });

  for (const [k, vs] of Object.entries(filters.filters ?? {})) {
    // A presence filter has no value to show — its wire value is a placeholder
    // — so the chip says what it does instead of rendering an empty string.
    if (filters.filterModes?.[k] === "empty") {
      chips.push({
        label: k,
        value: "is empty",
        onRemove: remove("filters", k, vs[0] ?? ""),
        variant: "include",
      });
      continue;
    }
    for (const v of vs) {
      chips.push({
        label: k,
        value: v,
        onRemove: remove("filters", k, v),
        variant: "include",
        mode: filters.filterModes?.[k],
      });
    }
  }
  for (const [k, vs] of Object.entries(filters.exclusions ?? {})) {
    if (filters.exclusionModes?.[k] === "empty") {
      // No `!` prefix here: "!user_agent has a value" would read as the
      // opposite of what this filter does.
      chips.push({
        label: k,
        value: "has a value",
        onRemove: remove("exclusions", k, vs[0] ?? ""),
        variant: "exclude",
      });
      continue;
    }
    for (const v of vs) {
      chips.push({
        label: `!${k}`,
        value: v,
        onRemove: remove("exclusions", k, v),
        variant: "exclude",
        mode: filters.exclusionModes?.[k],
      });
    }
  }

  if (chips.length === 0) return null;

  return (
    <div className="flex flex-wrap gap-1.5">
      {chips.map((chip) => (
        <span
          key={`${chip.label}:${chip.value}`}
          className={`inline-flex items-center gap-1 rounded px-2 py-0.5 text-xs font-mono leading-none border ${
            chip.variant === "include"
              ? "bg-[var(--color-info-dim)] text-[var(--color-info)] border-[var(--color-info)]/30"
              : chip.variant === "exclude"
                ? "bg-[var(--color-danger-dim)] text-[var(--color-danger)] border-[var(--color-danger)]/30"
                : "bg-[var(--color-bg-active)] text-[var(--color-fg-secondary)] border-[var(--color-border)]"
          }`}
        >
          {/* `~` separator + badge when a non-exact match mode is set, so the
              chip's semantics are visible at a glance. */}
          <span className="opacity-60">{chip.label}{chip.mode ? "~" : "="}</span>
          {chip.mode && (
            <Tooltip content={MODE_BADGE[chip.mode].tooltip}>
              <span className="rounded bg-current/15 px-1 font-bold">
                {MODE_BADGE[chip.mode].label}
              </span>
            </Tooltip>
          )}
          <span className="max-w-[160px] truncate">{chip.value}</span>
          {onRemove && (
            <button
              onClick={chip.onRemove}
              className="ml-0.5 rounded-full p-0.5 opacity-60 hover:opacity-100 transition-base"
            >
              <X size={10} />
            </button>
          )}
        </span>
      ))}
    </div>
  );
}
