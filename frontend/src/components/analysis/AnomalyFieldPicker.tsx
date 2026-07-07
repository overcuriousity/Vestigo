/**
 * AnomalyFieldPicker — popover for selecting which fields the rare-values
 * detector should scan.
 *
 * Fetches candidate fields from GET /anomalies/fields (cardinality-based
 * recommendation, works for any timeseries type).  Fields are grouped into
 * Standard (top-level columns) and Dynamic (attributes.*) and pre-selected
 * based on the backend's recommendation.
 *
 * The chip design mirrors the EmbedWizard field selector so the UX is
 * consistent across the anomaly and embedding workflows.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Hash, Check, Search, Settings2 } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  Popover,
  PopoverTrigger,
  PopoverContent,
} from "@/components/ui/Popover";
import { selectAutoScanTokens } from "./detector-shared";
import { cn } from "@/lib/cn";
import { anomalyFieldLabel as tokenLabel } from "@/lib/format";
import type {
  NoveltyFieldInfo,
  NoveltyFieldsResponse,
  NumericFieldsResponse,
} from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  /** Currently selected field tokens. null = "use backend smart default". */
  selected: string[] | null;
  onChange: (tokens: string[] | null) => void;
  /**
   * Minimum number of fields an explicit selection must keep (value_combo
   * needs ≥ 2). Below this, unchecking is blocked and the picker warns.
   */
  minSelected?: number;
  /** Maximum number of fields an explicit selection may hold (value_combo caps at 4). */
  maxSelected?: number;
  /**
   * How many recommended fields the backend's auto mode actually scans
   * (value_combo uses the top 2). When set, the auto default shows only that
   * many fields as checked so the picker mirrors what really runs.
   */
  autoCount?: number;
  /**
   * Auto mode also scans identifier-kind fields, not just recommended
   * (categorical) ones — the charset/entropy detectors' target. When set, the
   * auto preview mirrors the backend's categorical+identifier selection
   * (selectAutoScanTokens) so the checked set matches what actually runs.
   */
  autoIncludesIdentifiers?: boolean;
  /** Label for the "reset to backend default" action ("auto" by default). */
  autoLabel?: string;
  /**
   * Numeric mode — fetch numeric-parseable candidates (`/anomalies/numeric-fields`)
   * instead of the cardinality inventory. Chips show each field's numeric parse
   * ratio; the numeric-range detector uses this.
   */
  numeric?: boolean;
}

// Pipeline-synthesized fields (normalization metadata, not raw log content) —
// hidden from the picker entirely; mirrors _SYNTHETIC_FIELDS in
// db/anomaly_stats.py, which stops the backend from auto-recommending them.
const SYNTHETIC_TOKENS = new Set([
  "artifact",
  "display_name",
  "parser_name",
  "parser_version",
  "source_file",
]);

const KIND_HINT: Record<string, string> = {
  constant: "constant",
  identifier: "identifier/hash",
  sparse: "sparse",
};

function FieldChip({
  info,
  checked,
  onToggle,
  disabled = false,
  numericRatio,
}: {
  info: NoveltyFieldInfo;
  checked: boolean;
  onToggle: () => void;
  disabled?: boolean;
  /** When set (numeric mode), overrides the classification hint with the parse ratio. */
  numericRatio?: number;
}) {
  const skippedHint =
    numericRatio !== undefined
      ? !info.recommended
        ? `${Math.round(numericRatio * 100)}% numeric`
        : null
      : !info.recommended
        ? KIND_HINT[info.kind]
        : null;

  const chip = (
    <button
      type="button"
      onClick={onToggle}
      disabled={disabled}
      className={cn(
        "flex items-center gap-1 rounded border px-2 py-0.5 text-xs transition-colors",
        checked
          ? "border-[var(--color-accent)] bg-[var(--color-accent)]/15 text-[var(--color-fg-primary)]"
          : disabled
            ? "border-[var(--color-border)] text-[var(--color-fg-muted)] opacity-40 cursor-not-allowed"
            : "border-[var(--color-border)] text-[var(--color-fg-muted)] hover:border-[var(--color-fg-muted)]",
      )}
    >
      {checked && <Check size={9} />}
      {tokenLabel(info.token)}
      {skippedHint && (
        <span className="text-[9px] opacity-50">· {skippedHint}</span>
      )}
    </button>
  );

  const ratioText =
    numericRatio !== undefined ? ` · ${Math.round(numericRatio * 100)}% numeric` : "";
  const hint = skippedHint
    ? `${skippedHint} — ${(info.coverage * 100).toFixed(0)}% coverage, ${info.distinct} distinct values`
    : `${(info.coverage * 100).toFixed(0)}% coverage · ${info.distinct} distinct values${ratioText}`;

  return <Tooltip content={hint}>{chip}</Tooltip>;
}

export function AnomalyFieldPicker({
  caseId,
  timelineId,
  selected,
  onChange,
  minSelected,
  maxSelected,
  autoCount,
  autoIncludesIdentifiers = false,
  autoLabel = "auto",
  numeric = false,
}: Props) {
  const [query, setQuery] = useState("");
  const { data, isLoading } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, numeric ? "numeric-fields" : "fields"],
    queryFn: (): Promise<NoveltyFieldsResponse | NumericFieldsResponse> =>
      numeric
        ? anomaliesApi.numericFields(caseId, timelineId)
        : anomaliesApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });

  // Numeric candidates lack a cardinality `kind`; synthesise one so the chip
  // list can share a single NoveltyFieldInfo shape, and keep the parse ratio
  // aside for the chip hint.
  const numericRatios = useMemo(() => {
    const m = new Map<string, number>();
    if (numeric && data) {
      for (const f of data.fields as { token: string; numeric_ratio?: number }[]) {
        if (f.numeric_ratio !== undefined) m.set(f.token, f.numeric_ratio);
      }
    }
    return m;
  }, [numeric, data]);

  const allFields: NoveltyFieldInfo[] = useMemo(() => {
    let fields: NoveltyFieldInfo[];
    if (!data) return [];
    if (!numeric) {
      fields = data.fields as NoveltyFieldInfo[];
    } else {
      fields = (data.fields as { token: string; distinct: number; coverage: number; recommended: boolean }[]).map(
        (f) => ({
          token: f.token,
          distinct: f.distinct,
          coverage: f.coverage,
          kind: f.recommended ? "categorical" : "identifier",
          recommended: f.recommended,
        }),
      );
    }
    return fields.filter((f) => !SYNTHETIC_TOKENS.has(f.token));
  }, [data, numeric]);

  // Substring search across the display label and the raw token.
  const visibleFields = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return allFields;
    return allFields.filter(
      (f) =>
        f.token.toLowerCase().includes(q) || tokenLabel(f.token).toLowerCase().includes(q),
    );
  }, [allFields, query]);

  // Standard fields = top-level columns (no "attr:" prefix).
  // Dynamic fields = attribute keys.
  const { standard, dynamic } = useMemo(() => {
    return {
      standard: visibleFields.filter((f) => !f.token.startsWith("attr:")),
      dynamic: visibleFields.filter((f) => f.token.startsWith("attr:")),
    };
  }, [visibleFields]);

  // Effective selection: when null, use recommended defaults (shown as
  // checked). autoCount mirrors backends that only scan the top N of the
  // recommended set (value_combo scans the top 2) — the list is already
  // sorted recommended-first / coverage-descending by the backend.
  const effectiveSelected = useMemo(() => {
    if (selected !== null) return new Set(selected);
    const rec = allFields.filter((f) => f.recommended).map((f) => f.token);
    if (autoIncludesIdentifiers) {
      const ids = allFields.filter((f) => f.kind === "identifier").map((f) => f.token);
      return new Set(selectAutoScanTokens(rec, ids));
    }
    return new Set(autoCount !== undefined ? rec.slice(0, autoCount) : rec);
  }, [selected, allFields, autoCount, autoIncludesIdentifiers]);

  const toggle = (token: string) => {
    // Materialise the current effective set and toggle one token. Dropping
    // below minSelected is allowed (the caller disables the query and warns)
    // so a selection can be rebuilt from scratch.
    const next = new Set(effectiveSelected);
    if (next.has(token)) {
      next.delete(token);
    } else {
      if (maxSelected !== undefined && next.size >= maxSelected) return; // ceiling
      next.add(token);
    }
    onChange(Array.from(next));
  };

  // All/none act on the currently visible (search-filtered) fields.
  const selectAllVisible = () => {
    const next = new Set(effectiveSelected);
    for (const f of visibleFields) {
      if (maxSelected !== undefined && next.size >= maxSelected) break;
      next.add(f.token);
    }
    onChange(Array.from(next));
  };

  const selectNoneVisible = () => {
    const next = new Set(effectiveSelected);
    for (const f of visibleFields) next.delete(f.token);
    onChange(Array.from(next));
  };

  const resetToAuto = () => onChange(null);

  const isAuto = selected === null;
  const activeCount = effectiveSelected.size;
  const belowMin = minSelected !== undefined && !isAuto && activeCount < minSelected;
  const atMax = maxSelected !== undefined && activeCount >= maxSelected;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1 text-xs h-6 px-2">
          <Hash size={11} />
          Fields
          {activeCount > 0 && (
            <span className="ml-0.5 rounded bg-[var(--color-accent-dim)] px-1 text-xs font-semibold text-[var(--color-accent)]">
              {isAuto ? autoLabel : activeCount}
            </span>
          )}
        </Button>
      </PopoverTrigger>

      <PopoverContent className="w-96 p-0" align="end">
        <div className="border-b border-[var(--color-border)] px-3 py-2 space-y-2">
          <div>
            <p className="text-xs font-semibold text-[var(--color-fg-primary)]">
              Fields to scan
            </p>
            <p className="text-xs text-[var(--color-fg-muted)] mt-0.5">
              {minSelected !== undefined
                ? `Pick ${minSelected}${maxSelected ? `–${maxSelected}` : "+"} fields to combine.`
                : "Recommended fields are pre-selected based on cardinality."}
            </p>
          </div>
          <div className="relative">
            <Search
              size={12}
              className="absolute left-2 top-1/2 -translate-y-1/2 text-[var(--color-fg-muted)]"
            />
            <Input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Search fields…"
              className="h-7 pl-7 text-xs"
            />
          </div>
          <div className="flex items-center gap-3 text-xs">
            {maxSelected === undefined && (
              <button
                type="button"
                onClick={selectAllVisible}
                className="text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-colors"
              >
                Select all{query.trim() ? " matching" : ""}
              </button>
            )}
            <button
              type="button"
              onClick={selectNoneVisible}
              className="text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] transition-colors"
            >
              Clear{query.trim() ? " matching" : " all"}
            </button>
          </div>
        </div>

        <div className="max-h-[26rem] overflow-y-auto px-3 py-2 space-y-3">
          {isLoading ? (
            <div className="flex justify-center py-4">
              <Spinner size={16} />
            </div>
          ) : allFields.length === 0 ? (
            <p className="py-3 text-xs text-[var(--color-fg-muted)]">No fields found.</p>
          ) : visibleFields.length === 0 ? (
            <p className="py-3 text-xs text-[var(--color-fg-muted)]">
              No fields match “{query.trim()}”.
            </p>
          ) : (
            <>
              {standard.length > 0 && (
                <div>
                  <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)]">
                    Standard
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {standard.map((f) => (
                      <FieldChip
                        key={f.token}
                        info={f}
                        checked={effectiveSelected.has(f.token)}
                        disabled={atMax && !effectiveSelected.has(f.token)}
                        numericRatio={numericRatios.get(f.token)}
                        onToggle={() => toggle(f.token)}
                      />
                    ))}
                  </div>
                </div>
              )}
              {dynamic.length > 0 && (
                <div className={standard.length > 0 ? "border-t border-[var(--color-border-subtle)] pt-2" : ""}>
                  <p className="mb-1.5 text-xs font-semibold uppercase tracking-wider text-[var(--color-fg-secondary)]">
                    Dynamic fields
                  </p>
                  <div className="flex flex-wrap gap-1.5">
                    {dynamic.map((f) => (
                      <FieldChip
                        key={f.token}
                        info={f}
                        checked={effectiveSelected.has(f.token)}
                        disabled={atMax && !effectiveSelected.has(f.token)}
                        numericRatio={numericRatios.get(f.token)}
                        onToggle={() => toggle(f.token)}
                      />
                    ))}
                  </div>
                </div>
              )}
            </>
          )}
        </div>

        <div className="border-t border-[var(--color-border)] px-3 py-2 flex items-center justify-between">
          <button
            type="button"
            disabled={isAuto}
            onClick={resetToAuto}
            className={cn(
              "flex items-center gap-1 text-xs transition-colors",
              isAuto
                ? "text-[var(--color-accent)] font-medium"
                : "text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]",
            )}
          >
            <Settings2 size={10} />
            {isAuto ? `Auto (${autoLabel})` : `Reset to ${autoLabel}`}
          </button>
          <span
            className={cn(
              "text-xs",
              belowMin ? "text-[var(--color-warning)]" : "text-[var(--color-fg-muted)]",
            )}
          >
            {belowMin
              ? `Pick at least ${minSelected}`
              : `${activeCount} field${activeCount !== 1 ? "s" : ""} selected`}
          </span>
        </div>
      </PopoverContent>
    </Popover>
  );
}
