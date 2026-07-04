import { X } from "lucide-react";
import { Tooltip } from "@/components/ui/Tooltip";
import type { EventFilters, FieldMatchMode } from "@/api/types";

interface Props {
  filters: EventFilters;
  onRemove: (key: keyof EventFilters | string, fieldKey?: string, value?: string) => void;
}

interface Chip {
  label: string;
  value: string;
  onRemove: () => void;
  variant?: "include" | "exclude" | "neutral";
  /** Non-exact match mode of a field filter/exclusion — rendered as a badge. */
  mode?: FieldMatchMode;
}

const MODE_BADGE: Record<FieldMatchMode, { label: string; tooltip: string }> = {
  wildcard: { label: "*", tooltip: "Wildcard match: * = any run, ? = one char (case-insensitive)" },
  regex: { label: ".*", tooltip: "RE2 regular expression (case-sensitive)" },
};

export function FilterChips({ filters, onRemove }: Props) {
  const chips: Chip[] = [];

  if (filters.q)
    chips.push({
      label: "search",
      value: filters.q,
      onRemove: () => onRemove("q"),
      variant: "neutral",
    });
  if (filters.artifact)
    chips.push({
      label: "artifact",
      value: filters.artifact,
      onRemove: () => onRemove("artifact"),
      variant: "include",
    });
  for (const a of filters.artifacts ?? []) {
    chips.push({
      label: "artifact",
      value: a,
      onRemove: () => onRemove("artifacts", undefined, a),
      variant: "include",
    });
  }
  if (filters.sourceId)
    chips.push({
      label: "sourceId",
      value: filters.sourceId,
      onRemove: () => onRemove("sourceId"),
      variant: "include",
    });
  if (filters.tag)
    chips.push({
      label: "tag",
      value: filters.tag,
      onRemove: () => onRemove("tag"),
      variant: "include",
    });
  for (const t of filters.tagsInclude ?? []) {
    chips.push({
      label: "tag",
      value: t,
      onRemove: () => onRemove("tagsInclude", undefined, t),
      variant: "include",
    });
  }
  for (const t of filters.tagsExclude ?? []) {
    chips.push({
      label: "!tag",
      value: t,
      onRemove: () => onRemove("tagsExclude", undefined, t),
      variant: "exclude",
    });
  }
  for (const t of filters.annotated ?? []) {
    chips.push({
      label: "flagged",
      value: t === "tag" && filters.annotationTagValue ? `tag:${filters.annotationTagValue}` : t,
      onRemove: () => onRemove("annotated", undefined, t),
      variant: "include",
    });
  }
  if (filters.start)
    chips.push({
      label: "from",
      value: filters.start.replace("T", " ").replace(/\.\d+Z$/, "Z"),
      onRemove: () => onRemove("start"),
      variant: "neutral",
    });
  if (filters.end)
    chips.push({
      label: "to",
      value: filters.end.replace("T", " ").replace(/\.\d+Z$/, "Z"),
      onRemove: () => onRemove("end"),
      variant: "neutral",
    });

  for (const [k, v] of Object.entries(filters.filters ?? {})) {
    chips.push({
      label: k,
      value: v,
      onRemove: () => onRemove("filters", k),
      variant: "include",
      mode: filters.filterModes?.[k],
    });
  }
  for (const [k, vs] of Object.entries(filters.exclusions ?? {})) {
    for (const v of vs) {
      chips.push({
        label: `!${k}`,
        value: v,
        onRemove: () => onRemove("exclusions", k, v),
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
          <button
            onClick={chip.onRemove}
            className="ml-0.5 rounded-full p-0.5 opacity-60 hover:opacity-100 transition-base"
          >
            <X size={10} />
          </button>
        </span>
      ))}
    </div>
  );
}
