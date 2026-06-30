import { useState } from "react";
import { Search, Clock, PlusCircle, MinusCircle, BookmarkCheck, PanelLeftClose, X } from "lucide-react";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Tooltip } from "@/components/ui/Tooltip";
import type { EventFilters, View } from "@/api/types";
import { fmtRelative } from "@/lib/time";
import { viewPayloadToFilters } from "@/lib/queryParams";

interface Props {
  filters: EventFilters;
  onChange: (f: EventFilters) => void;
  views: View[];
  onApplyView: (f: EventFilters) => void;
  onSaveView: () => void;
  onClose?: () => void;
}

export function FilterRail({ filters, onChange, views, onApplyView, onSaveView, onClose }: Props) {
  const [fieldKey, setFieldKey] = useState("");
  const [fieldVal, setFieldVal] = useState("");
  const [excludeKey, setExcludeKey] = useState("");
  const [excludeVal, setExcludeVal] = useState("");

  const addFilter = () => {
    if (!fieldKey.trim() || !fieldVal.trim()) return;
    onChange({
      ...filters,
      filters: { ...(filters.filters ?? {}), [fieldKey.trim()]: fieldVal.trim() },
    });
    setFieldKey("");
    setFieldVal("");
  };

  const addExclusion = () => {
    if (!excludeKey.trim() || !excludeVal.trim()) return;
    onChange({
      ...filters,
      exclusions: {
        ...(filters.exclusions ?? {}) as Record<string, string[]>,
        [excludeKey.trim()]: [...(filters.exclusions?.[excludeKey.trim()] ?? []), excludeVal.trim()],
      },
    });
    setExcludeKey("");
    setExcludeVal("");
  };

  const hasFilters = Object.values(filters).some((v) =>
    v && (typeof v === "string" ? v.length > 0 : Object.keys(v).length > 0),
  );

  return (
    <aside className="flex h-full w-64 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-bg-surface)]">
      {/* Rail header */}
      <div className="flex items-center justify-between border-b border-[var(--color-border)] px-2.5 py-1.5 shrink-0">
        <span className="text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-muted)]">
          Filters
        </span>
        <div className="flex items-center gap-1">
          {hasFilters && (
            <Tooltip content="Clear all filters">
              <button
                onClick={() => onChange({})}
                className="rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-danger)] transition-base"
              >
                <X size={13} />
              </button>
            </Tooltip>
          )}
          {onClose && (
            <Tooltip content="Hide filter panel">
              <button
                onClick={onClose}
                className="rounded p-0.5 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-base"
              >
                <PanelLeftClose size={13} />
              </button>
            </Tooltip>
          )}
        </div>
      </div>

      <div className="flex-1 overflow-y-auto">
      <div className="space-y-2.5 p-2.5">
        {/* Full-text search */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Search size={11} /> Search
          </label>
          <Input
            placeholder="keyword in message…"
            value={filters.q ?? ""}
            onChange={(e) =>
              onChange({ ...filters, q: e.target.value || undefined })
            }
          />
        </div>

        {/* Time range */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Clock size={11} /> Time Range
          </label>
          <div className="space-y-1.5">
            <Input
              type="datetime-local"
              placeholder="From"
              value={filters.start ? filters.start.slice(0, 16) : ""}
              onChange={(e) =>
                onChange({
                  ...filters,
                  start: e.target.value
                    ? new Date(e.target.value).toISOString()
                    : undefined,
                })
              }
              className="text-xs"
            />
            <Input
              type="datetime-local"
              placeholder="To"
              value={filters.end ? filters.end.slice(0, 16) : ""}
              onChange={(e) =>
                onChange({
                  ...filters,
                  end: e.target.value
                    ? new Date(e.target.value).toISOString()
                    : undefined,
                })
              }
              className="text-xs"
            />
          </div>
        </div>

        {/* Artifact */}
        <div>
          <label className="mb-1.5 block text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            Artifact
          </label>
          <Input
            placeholder="artifact name…"
            value={filters.artifact ?? ""}
            onChange={(e) =>
              onChange({ ...filters, artifact: e.target.value || undefined })
            }
          />
        </div>

        {/* Source ID */}
        <div>
          <label className="mb-1.5 block text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            Source ID
          </label>
          <Input
            placeholder="filter by source id…"
            value={filters.sourceId ?? ""}
            onChange={(e) =>
              onChange({ ...filters, sourceId: e.target.value || undefined })
            }
          />
        </div>

        {/* Tag (event.tags) */}
        <div>
          <label className="mb-1.5 block text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            Parser Tag
          </label>
          <Input
            placeholder="tag value…"
            value={filters.tag ?? ""}
            onChange={(e) =>
              onChange({ ...filters, tag: e.target.value || undefined })
            }
          />
        </div>

        {/* Field include */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-info)] uppercase tracking-wide">
            <PlusCircle size={11} /> Field = Value
          </label>
          <div className="flex gap-1">
            <Input
              placeholder="field"
              value={fieldKey}
              onChange={(e) => setFieldKey(e.target.value)}
              className="w-24 text-xs"
            />
            <Input
              placeholder="value"
              value={fieldVal}
              onChange={(e) => setFieldVal(e.target.value)}
              className="flex-1 text-xs"
              onKeyDown={(e) => e.key === "Enter" && addFilter()}
            />
            <Button size="icon" variant="outline" onClick={addFilter}>
              <PlusCircle size={13} />
            </Button>
          </div>
        </div>

        {/* Field exclude */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-danger)] uppercase tracking-wide">
            <MinusCircle size={11} /> Field ≠ Value
          </label>
          <div className="flex gap-1">
            <Input
              placeholder="field"
              value={excludeKey}
              onChange={(e) => setExcludeKey(e.target.value)}
              className="w-24 text-xs"
            />
            <Input
              placeholder="value"
              value={excludeVal}
              onChange={(e) => setExcludeVal(e.target.value)}
              className="flex-1 text-xs"
              onKeyDown={(e) => e.key === "Enter" && addExclusion()}
            />
            <Button size="icon" variant="outline" onClick={addExclusion}>
              <MinusCircle size={13} />
            </Button>
          </div>
        </div>

        {/* Saved views */}
        {views.length > 0 && (
          <div>
            <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
              <BookmarkCheck size={11} /> Saved Views
            </label>
            <div className="space-y-1">
              {views.map((v) => (
                <button
                  key={v.id}
                  className="w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2.5 py-1.5 text-left text-xs text-[var(--color-fg-secondary)] hover:border-[var(--color-accent)] hover:text-[var(--color-fg-primary)] transition-base"
                  onClick={() =>
                    onApplyView(viewPayloadToFilters(v.filter as Record<string, unknown>))
                  }
                >
                  <div className="truncate font-medium">{v.name}</div>
                  <div className="text-[var(--color-fg-muted)]">
                    {fmtRelative(v.created_at)}
                  </div>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
      </div>

      {/* Save current view */}
      <div className="border-t border-[var(--color-border)] p-2.5 shrink-0">
        <Button variant="outline" size="sm" className="w-full" onClick={onSaveView}>
          <BookmarkCheck size={13} /> Save Current View
        </Button>
      </div>
    </aside>
  );
}
