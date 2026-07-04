import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Search, Clock, PlusCircle, MinusCircle, BookmarkCheck, PanelLeftClose, X, Tag, ShieldAlert, FileText, Database, Regex } from "lucide-react";
import { Input } from "@/components/ui/Input";
import { Button } from "@/components/ui/Button";
import { Tooltip } from "@/components/ui/Tooltip";
import { Spinner } from "@/components/ui/Spinner";
import { TagInput } from "@/components/explorer/TagInput";
import { TagFacetPanel } from "@/components/explorer/TagFacetPanel";
import { vizApi } from "@/api/viz";
import { useDebouncedValue } from "@/hooks/useDebouncedValue";
import { cn } from "@/lib/cn";
import type { EventFilters, FieldMatchMode, View } from "@/api/types";
import { datetimeLocalToUtcIso, fmtRelative, isoToDatetimeLocalUtc } from "@/lib/time";
import { viewPayloadToFilters } from "@/lib/queryParams";

/** Debounced top-N distinct values of `fieldKey`, for value autocomplete.
 * Deliberately unfiltered (empty EventFilters): suggestions describe the
 * whole timeline, not the currently narrowed view, and don't refetch on
 * every filter change. */
function useFieldValueSuggestions(
  caseId: string,
  timelineId: string,
  fieldKey: string,
): string[] {
  const debouncedKey = useDebouncedValue(fieldKey.trim(), 300);
  const { data } = useQuery({
    queryKey: ["field-value-suggest", caseId, timelineId, debouncedKey],
    queryFn: () => vizApi.fieldTerms(caseId, timelineId, debouncedKey, {}, 50),
    enabled: !!(caseId && timelineId && debouncedKey),
    staleTime: 60_000,
  });
  return useMemo(() => (data?.values ?? []).map((v) => v.value).filter(Boolean), [data]);
}

type RowMatchMode = "exact" | FieldMatchMode;

const MATCH_MODE_OPTIONS: { mode: RowMatchMode; label: string; tooltip: string }[] = [
  { mode: "exact", label: "=", tooltip: "Exact value match (case-sensitive)" },
  {
    mode: "wildcard",
    label: "*",
    tooltip: "Wildcard: * = any run, ? = one char — case-insensitive. e.g. 10.0.*",
  },
  {
    mode: "regex",
    label: ".*",
    tooltip: "RE2 regular expression — case-sensitive, prefix (?i) for case-insensitive",
  },
];

const MODE_PLACEHOLDER: Record<RowMatchMode, string> = {
  exact: "value",
  wildcard: "e.g. 10.0.*",
  regex: "RE2 pattern — (?i) for case-insensitive",
};

/** 3-state exact/wildcard/regex selector for one field-filter entry row. */
function MatchModeControl({
  mode,
  onChange,
}: {
  mode: RowMatchMode;
  onChange: (m: RowMatchMode) => void;
}) {
  return (
    <div className="flex overflow-hidden rounded border border-[var(--color-border-strong)] text-xs">
      {MATCH_MODE_OPTIONS.map((opt) => (
        <Tooltip key={opt.mode} content={opt.tooltip}>
          <button
            type="button"
            onClick={() => onChange(opt.mode)}
            className={cn(
              "px-2 py-0.5 font-mono transition-base",
              mode === opt.mode
                ? "bg-[var(--color-accent)] text-white"
                : "bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]",
            )}
          >
            {opt.label}
          </button>
        </Tooltip>
      ))}
    </div>
  );
}

/** Per-row hints: the exact-mode glob trap, and regex pre-validation.
 * Both non-authoritative — the server-side 400 stays the source of truth. */
function matchModeHint(mode: RowMatchMode, value: string): string | undefined {
  const v = value.trim();
  if (!v) return undefined;
  if (mode === "exact" && /[*?]/.test(v)) {
    return "* matched literally in Exact mode — switch to * (Wildcard) for pattern matching";
  }
  if (mode === "regex") {
    try {
      new RegExp(v);
    } catch (e) {
      return e instanceof Error ? e.message : "invalid regular expression";
    }
  }
  return undefined;
}

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
  /** Field names (top-level + attribute keys) across this timeline's sources,
   * for the Field=Value / Field≠Value key autocomplete. */
  fieldSuggestions?: string[];
  /** Whether any source in this timeline has embeddings — gates the Semantic
   * search mode. */
  hasVectors?: boolean;
  caseId: string;
  timelineId: string;
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
  fieldSuggestions = [],
  hasVectors = false,
  caseId,
  timelineId,
  onSearchSubmit,
  searchStatus,
  searchPending,
}: Props) {
  const [searchInput, setSearchInput] = useState(filters.q ?? "");
  const [artifactInput, setArtifactInput] = useState("");
  const [fieldKey, setFieldKey] = useState("");
  const [fieldVal, setFieldVal] = useState("");
  const [fieldMode, setFieldMode] = useState<RowMatchMode>("exact");
  const [excludeKey, setExcludeKey] = useState("");
  const [excludeVal, setExcludeVal] = useState("");
  const [excludeMode, setExcludeMode] = useState<RowMatchMode>("exact");

  const fieldValueSuggestions = useFieldValueSuggestions(caseId, timelineId, fieldKey);
  const excludeValueSuggestions = useFieldValueSuggestions(caseId, timelineId, excludeKey);

  const semanticMode = filters.qMode === "semantic";
  const setSearchMode = (mode: "keyword" | "semantic") => {
    if ((mode === "semantic") === semanticMode) return;
    const f = { ...filters };
    if (mode === "semantic") {
      f.qMode = "semantic";
      delete f.qRegex; // regex only applies to the server-side keyword search
    } else {
      delete f.qMode;
    }
    onChange(f);
  };
  const toggleRegex = () => {
    const f = { ...filters };
    if (f.qRegex) delete f.qRegex;
    else f.qRegex = true;
    onChange(f);
  };
  // Non-authoritative early hint: JS RegExp and ClickHouse RE2 dialects
  // differ, so the server-side 400 stays the source of truth.
  const regexHint = useMemo(() => {
    if (semanticMode || !filters.qRegex || !searchInput.trim()) return undefined;
    try {
      new RegExp(searchInput);
      return undefined;
    } catch (e) {
      return e instanceof Error ? e.message : "invalid regular expression";
    }
  }, [semanticMode, filters.qRegex, searchInput]);

  const addFilter = (value?: string) => {
    const v = (value ?? fieldVal).trim();
    const key = fieldKey.trim();
    if (!key || !v) return;
    // "exact" is never stored — absence means exact (legacy compatibility).
    const modes = { ...(filters.filterModes ?? {}) };
    if (fieldMode === "exact") delete modes[key];
    else modes[key] = fieldMode;
    onChange({
      ...filters,
      filters: { ...(filters.filters ?? {}), [key]: v },
      filterModes: Object.keys(modes).length > 0 ? modes : undefined,
    });
    setFieldKey("");
    setFieldVal("");
    setFieldMode("exact");
  };

  const addExclusion = (value?: string) => {
    const v = (value ?? excludeVal).trim();
    const key = excludeKey.trim();
    if (!key || !v) return;
    // Mode-per-key: the mode chosen here becomes the key's mode for ALL its
    // excluded values — every chip of the key shows the (updated) badge, so
    // a semantics change is visible, never silent.
    const modes = { ...(filters.exclusionModes ?? {}) };
    if (excludeMode === "exact") delete modes[key];
    else modes[key] = excludeMode;
    onChange({
      ...filters,
      exclusions: {
        ...(filters.exclusions ?? {}) as Record<string, string[]>,
        [key]: [...(filters.exclusions?.[key] ?? []), v],
      },
      exclusionModes: Object.keys(modes).length > 0 ? modes : undefined,
    });
    setExcludeKey("");
    setExcludeVal("");
    setExcludeMode("exact");
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
    <aside className="flex h-full w-72 shrink-0 flex-col border-r border-[var(--color-border)] bg-[var(--color-bg-surface)]">
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
          <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Search size={13} /> Search
          </label>
          <form
            className="flex gap-1"
            onSubmit={(e) => {
              e.preventDefault();
              onSearchSubmit(searchInput.trim());
            }}
          >
            <Input
              placeholder={
                semanticMode
                  ? "describe events to find…"
                  : filters.qRegex
                    ? "RE2 pattern — (?i) for case-insensitive"
                    : "keyword, phrase, or event id…"
              }
              value={searchInput}
              onChange={(e) => setSearchInput(e.target.value)}
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
          {/* Search-mode control: keyword (default) vs semantic is a deliberate
              analyst choice — never auto-switched. */}
          <div className="mt-1.5 flex items-center gap-1">
            <div className="flex overflow-hidden rounded border border-[var(--color-border-strong)] text-xs">
              <button
                type="button"
                onClick={() => setSearchMode("keyword")}
                className={cn(
                  "px-2 py-1 transition-base",
                  !semanticMode
                    ? "bg-[var(--color-accent)] text-white"
                    : "bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]",
                )}
              >
                Keyword
              </button>
              <Tooltip
                content={
                  hasVectors
                    ? "Embedding-based similarity search"
                    : "No embeddings for this timeline yet"
                }
              >
                <button
                  type="button"
                  disabled={!hasVectors}
                  onClick={() => setSearchMode("semantic")}
                  className={cn(
                    "px-2 py-1 transition-base",
                    semanticMode
                      ? "bg-[var(--color-accent)] text-white"
                      : "bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] disabled:opacity-40 disabled:hover:text-[var(--color-fg-muted)]",
                  )}
                >
                  Semantic
                </button>
              </Tooltip>
            </div>
            {!semanticMode && (
              <Tooltip content="Treat search as RE2 regular expression">
                <button
                  type="button"
                  aria-label="Treat search as RE2 regular expression"
                  onClick={toggleRegex}
                  className={cn(
                    "rounded border px-1.5 py-1 transition-base",
                    filters.qRegex
                      ? "border-[var(--color-accent)] bg-[var(--color-accent)]/15 text-[var(--color-accent)]"
                      : "border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]",
                  )}
                >
                  <Regex size={13} />
                </button>
              </Tooltip>
            )}
          </div>
          {regexHint && (
            <div className="mt-1 text-xs text-[var(--color-danger)]">{regexHint}</div>
          )}
          {(searchPending || searchStatus) && (
            <div className="mt-1 flex items-center gap-1 text-xs text-[var(--color-fg-muted)]">
              {searchPending && <Spinner size={10} />}
              {searchStatus && <span>{searchStatus}</span>}
            </div>
          )}
        </div>

        {/* Time range */}
        <div>
          <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Clock size={13} /> Time Range (UTC)
          </label>
          <div className="space-y-1.5">
            <Input
              type="datetime-local"
              placeholder="From"
              value={isoToDatetimeLocalUtc(filters.start)}
              onChange={(e) =>
                onChange({
                  ...filters,
                  start: datetimeLocalToUtcIso(e.target.value),
                })
              }
            />
            <Input
              type="datetime-local"
              placeholder="To"
              value={isoToDatetimeLocalUtc(filters.end)}
              onChange={(e) =>
                onChange({
                  ...filters,
                  end: datetimeLocalToUtcIso(e.target.value),
                })
              }
            />
          </div>
        </div>

        {/* Flagged — annotation tag / anomaly filter */}
        <div>
          <label className="mb-2 block text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            Flagged
          </label>
          <div className="space-y-1.5">
            <label className="flex items-center gap-1.5 text-xs text-[var(--color-fg-secondary)] cursor-pointer">
              <input
                type="checkbox"
                checked={annotated.includes("tag")}
                onChange={() => toggleAnnotated("tag")}
              />
              <Tag size={13} /> Tagged
            </label>
            <label className="flex items-center gap-1.5 text-xs text-[var(--color-fg-secondary)] cursor-pointer">
              <input
                type="checkbox"
                checked={annotated.includes("anomaly")}
                onChange={() => toggleAnnotated("anomaly")}
              />
              <ShieldAlert size={13} /> Anomaly
            </label>
          </div>
        </div>

        {/* Tags — unified autocomplete across annotation + parser tags */}
        <div>
          <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Tag size={13} /> Tags
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
          <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <FileText size={13} /> Artifact
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
          />
        </div>

        {/* Source ID — filter to events from one ingested source */}
        <div>
          <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
            <Database size={13} /> Source ID
          </label>
          <div className="flex gap-1">
            <Input
              placeholder="source_id…"
              value={filters.sourceId ?? ""}
              onChange={(e) =>
                onChange({ ...filters, sourceId: e.target.value.trim() || undefined })
              }
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
          <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-info)] uppercase tracking-wide">
            <PlusCircle size={13} /> Field = Value
          </label>
          <div className="flex gap-1">
            <TagInput
              placeholder="field"
              openOnFocus
              value={fieldKey}
              onChange={setFieldKey}
              onSubmit={setFieldKey}
              onCancel={() => setFieldKey("")}
              suggestions={fieldSuggestions}
              className="w-24"
            />
            <TagInput
              placeholder={MODE_PLACEHOLDER[fieldMode]}
              openOnFocus
              value={fieldVal}
              onChange={setFieldVal}
              onSubmit={addFilter}
              onCancel={() => setFieldVal("")}
              suggestions={fieldValueSuggestions}
              className="flex-1"
            />
            <Button size="icon" variant="outline" onClick={() => addFilter()}>
              <PlusCircle size={13} />
            </Button>
          </div>
          <div className="mt-1.5">
            <MatchModeControl mode={fieldMode} onChange={setFieldMode} />
          </div>
          {matchModeHint(fieldMode, fieldVal) && (
            <div className="mt-1 text-xs text-[var(--color-warning)]">
              {matchModeHint(fieldMode, fieldVal)}
            </div>
          )}
        </div>

        {/* Field exclude */}
        <div>
          <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-danger)] uppercase tracking-wide">
            <MinusCircle size={13} /> Field ≠ Value
          </label>
          <div className="flex gap-1">
            <TagInput
              placeholder="field"
              openOnFocus
              value={excludeKey}
              onChange={setExcludeKey}
              onSubmit={setExcludeKey}
              onCancel={() => setExcludeKey("")}
              suggestions={fieldSuggestions}
              className="w-24"
            />
            <TagInput
              placeholder={MODE_PLACEHOLDER[excludeMode]}
              openOnFocus
              value={excludeVal}
              onChange={setExcludeVal}
              onSubmit={addExclusion}
              onCancel={() => setExcludeVal("")}
              suggestions={excludeValueSuggestions}
              className="flex-1"
            />
            <Button size="icon" variant="outline" onClick={() => addExclusion()}>
              <MinusCircle size={13} />
            </Button>
          </div>
          <div className="mt-1.5">
            <MatchModeControl mode={excludeMode} onChange={setExcludeMode} />
          </div>
          {matchModeHint(excludeMode, excludeVal) && (
            <div className="mt-1 text-xs text-[var(--color-warning)]">
              {matchModeHint(excludeMode, excludeVal)}
            </div>
          )}
        </div>

        {/* Saved views */}
        {views.length > 0 && (
          <div>
            <label className="mb-2 flex items-center gap-2 text-xs font-medium text-[var(--color-fg-muted)] uppercase tracking-wide">
              <BookmarkCheck size={13} /> Saved Views
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
