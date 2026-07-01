import { useState } from "react";
import { Search, Clock, PlusCircle, MinusCircle, BookmarkCheck, PanelLeftClose, X, Tag, ShieldAlert, FileText, Database } from "lucide-react";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Tooltip } from "@/components/ui/Tooltip";
import { Spinner } from "@/components/ui/Spinner";
import { TagInput } from "@/components/explorer/TagInput";
import { TagFacetPanel } from "@/components/explorer/TagFacetPanel";
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
  /** Merged (annotation + parser) tag values, for the unified Tags filter. */
  mergedTagSuggestions?: string[];
  /** Distinct artifact values in this timeline, for the Artifact filter. */
  artifactSuggestions?: string[];
  /**
   * Submits the search box's free text. The caller decides whether it's an
   * event_id lookup (jump directly) or a keyword/semantic query (narrows the
   * grid) — this component just forwards raw input.
   */
  onSearchSubmit: (query: string) => void;
  /** Status line shown under the search box (e.g. "searching…", "no match"). */
  searchStatus?: string;
  searchPending?: boolean;
}

export function FilterRail({
  filters,
  onChange,
  views,
  onApplyView,
  onSaveView,
  onClose,
  mergedTagSuggestions = [],
  artifactSuggestions = [],
  onSearchSubmit,
  searchStatus,
  searchPending,
}: Props) {
  const [searchInput, setSearchInput] = useState(filters.q ?? "");
  const [artifactInput, setArtifactInput] = useState("");
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

  const addArtifact = (value: string) => {
    const v = value.trim();
    if (!v) return;
    const prev = filters.artifacts ?? [];
    if (!prev.includes(v)) {
      onChange({ ...filters, artifacts: [...prev, v] });
    }
    setArtifactInput("");
  };

  const removeArtifact = (value: string) => {
    const remaining = (filters.artifacts ?? []).filter((a) => a !== value);
    const f = { ...filters };
    if (remaining.length > 0) f.artifacts = remaining;
    else delete f.artifacts;
    onChange(f);
  };

  const setTagSelection = (include: string[], exclude: string[]) => {
    const f = { ...filters };
    if (include.length > 0) f.tagsInclude = include;
    else delete f.tagsInclude;
    if (exclude.length > 0) f.tagsExclude = exclude;
    else delete f.tagsExclude;
    onChange(f);
  };

  const annotated = filters.annotated ?? [];
  const toggleAnnotated = (type: "tag" | "anomaly") => {
    const next = annotated.includes(type)
      ? annotated.filter((t) => t !== type)
      : [...annotated, type];
    const f = { ...filters };
    if (next.length > 0) {
      f.annotated = next;
    } else {
      delete f.annotated;
      delete f.annotationTagValue;
    }
    onChange(f);
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
        {/* Unified search — keyword/semantic across all fields, or an event_id to jump to it */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Search size={11} /> Search
          </label>
          <form
            className="flex gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              onSearchSubmit(searchInput.trim());
            }}
          >
            <Input
              placeholder="keyword, phrase, or event id…"
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
              className="text-xs"
            />
            {searchInput && (
              <Button
                size="icon"
                variant="outline"
                type="button"
                onClick={() => {
                  setSearchInput("");
                  onSearchSubmit("");
                }}
              >
                <X size={12} />
              </Button>
            )}
          </form>
          {(searchPending || searchStatus) && (
            <div className="mt-1 flex items-center gap-1 text-[11px] text-[var(--color-fg-muted)]">
              {searchPending && <Spinner size={10} />}
              {searchStatus && <span>{searchStatus}</span>}
            </div>
          )}
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

        {/* Flagged — annotation tag / anomaly filter */}
        <div>
          <label className="mb-1.5 block text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            Flagged
          </label>
          <div className="space-y-1.5">
            <label className="flex items-center gap-1.5 text-xs text-[var(--color-fg-secondary)] cursor-pointer">
              <input
                type="checkbox"
                checked={annotated.includes("tag")}
                onChange={() => toggleAnnotated("tag")}
              />
              <Tag size={11} /> Tagged
            </label>
            <label className="flex items-center gap-1.5 text-xs text-[var(--color-fg-secondary)] cursor-pointer">
              <input
                type="checkbox"
                checked={annotated.includes("anomaly")}
                onChange={() => toggleAnnotated("anomaly")}
              />
              <ShieldAlert size={11} /> Anomaly
            </label>
          </div>
        </div>

        {/* Tags — unified autocomplete across annotation + parser tags */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Tag size={11} /> Tags
          </label>
          <TagFacetPanel
            tags={Array.from(
              new Set([
                ...mergedTagSuggestions,
                ...(filters.tagsInclude ?? []),
                ...(filters.tagsExclude ?? []),
              ]),
            ).sort()}
            include={filters.tagsInclude ?? []}
            exclude={filters.tagsExclude ?? []}
            onChange={setTagSelection}
          />
        </div>

        {/* Artifact — multi-select autocomplete */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <FileText size={11} /> Artifact
          </label>
          {(filters.artifacts ?? []).length > 0 && (
            <div className="mb-1.5 flex flex-wrap gap-1">
              {(filters.artifacts ?? []).map((a) => (
                <span
                  key={a}
                  className="inline-flex items-center gap-1 rounded border border-[var(--color-info)]/30 bg-[var(--color-info-dim)] px-1.5 py-0.5 text-xs text-[var(--color-info)]"
                >
                  {a}
                  <button onClick={() => removeArtifact(a)} className="opacity-60 hover:opacity-100">
                    <X size={10} />
                  </button>
                </span>
              ))}
            </div>
          )}
          <TagInput
            value={artifactInput}
            onChange={setArtifactInput}
            onSubmit={addArtifact}
            onCancel={() => setArtifactInput("")}
            suggestions={artifactSuggestions.filter((a) => !(filters.artifacts ?? []).includes(a))}
            placeholder="add artifact…"
            className="text-xs"
          />
        </div>

        {/* Source ID — filter to events from one ingested source */}
        <div>
          <label className="mb-1.5 flex items-center gap-1.5 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Database size={11} /> Source ID
          </label>
          <div className="flex gap-1">
            <Input
              placeholder="source_id…"
              value={filters.sourceId ?? ""}
              onChange={(e) =>
                onChange({ ...filters, sourceId: e.target.value.trim() || undefined })
              }
              className="text-xs"
            />
            {filters.sourceId && (
              <Button
                size="icon"
                variant="outline"
                type="button"
                onClick={() => onChange({ ...filters, sourceId: undefined })}
              >
                <X size={12} />
              </Button>
            )}
          </div>
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
