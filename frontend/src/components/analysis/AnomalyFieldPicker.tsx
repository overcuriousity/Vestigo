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
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { Hash, Check, Settings2 } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  Popover,
  PopoverTrigger,
  PopoverContent,
} from "@/components/ui/Popover";
import { cn } from "@/lib/cn";
import type { NoveltyFieldInfo } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  /** Currently selected field tokens. null = "use backend smart default". */
  selected: string[] | null;
  onChange: (tokens: string[] | null) => void;
}

/** Friendly display label for a field token. */
function tokenLabel(token: string): string {
  if (token.startsWith("attr:")) return token.slice(5);
  const LABELS: Record<string, string> = {
    artifact: "Artifact",
    timestamp_desc: "Event category",
    display_name: "Display name",
    parser_name: "Parser",
    message: "Message",
    source_file: "Source file",
  };
  return LABELS[token] ?? token;
}

const KIND_HINT: Record<string, string> = {
  constant: "constant",
  identifier: "identifier/hash",
  sparse: "sparse",
};

function FieldChip({
  info,
  checked,
  onToggle,
}: {
  info: NoveltyFieldInfo;
  checked: boolean;
  onToggle: () => void;
}) {
  const skippedHint = !info.recommended ? KIND_HINT[info.kind] : null;

  const chip = (
    <button
      type="button"
      onClick={onToggle}
      className={cn(
        "flex items-center gap-1 rounded border px-2 py-0.5 text-xs transition-colors",
        checked
          ? "border-[var(--color-accent)] bg-[var(--color-accent)]/15 text-[var(--color-fg-primary)]"
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

  const hint = skippedHint
    ? `${info.kind} — ${(info.coverage * 100).toFixed(0)}% coverage, ${info.distinct} distinct values`
    : `${(info.coverage * 100).toFixed(0)}% coverage · ${info.distinct} distinct values`;

  return <Tooltip content={hint}>{chip}</Tooltip>;
}

export function AnomalyFieldPicker({ caseId, timelineId, selected, onChange }: Props) {
  const { data, isLoading } = useQuery({
    queryKey: ["anomaly-fields", caseId, timelineId],
    queryFn: () => anomaliesApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });

  const allFields = useMemo(() => data?.fields ?? [], [data]);

  // Standard fields = top-level columns (no "attr:" prefix).
  // Dynamic fields = attribute keys.
  const { standard, dynamic } = useMemo(() => {
    return {
      standard: allFields.filter((f) => !f.token.startsWith("attr:")),
      dynamic: allFields.filter((f) => f.token.startsWith("attr:")),
    };
  }, [allFields]);

  // Effective selection: when null, use recommended defaults (shown as checked).
  const effectiveSelected = useMemo(() => {
    if (selected !== null) return new Set(selected);
    return new Set(allFields.filter((f) => f.recommended).map((f) => f.token));
  }, [selected, allFields]);

  const toggle = (token: string) => {
    // Materialise the current effective set and toggle one token.
    const next = new Set(effectiveSelected);
    if (next.has(token)) next.delete(token);
    else next.add(token);
    onChange(Array.from(next));
  };

  const resetToAuto = () => onChange(null);

  const isAuto = selected === null;
  const activeCount = effectiveSelected.size;

  return (
    <Popover>
      <PopoverTrigger asChild>
        <Button variant="outline" size="sm" className="gap-1 text-xs h-6 px-2">
          <Hash size={11} />
          Fields
          {activeCount > 0 && (
            <span className="ml-0.5 rounded bg-[var(--color-accent-dim)] px-1 text-xs font-semibold text-[var(--color-accent)]">
              {isAuto ? "auto" : activeCount}
            </span>
          )}
        </Button>
      </PopoverTrigger>

      <PopoverContent className="w-72 p-0" align="end">
        <div className="border-b border-[var(--color-border)] px-3 py-2">
          <p className="text-xs font-semibold text-[var(--color-fg-primary)]">
            Fields to scan
          </p>
          <p className="text-xs text-[var(--color-fg-muted)] mt-0.5">
            Recommended fields are pre-selected based on cardinality.
          </p>
        </div>

        <div className="max-h-72 overflow-y-auto px-3 py-2 space-y-3">
          {isLoading ? (
            <div className="flex justify-center py-4">
              <Spinner size={16} />
            </div>
          ) : allFields.length === 0 ? (
            <p className="py-3 text-xs text-[var(--color-fg-muted)]">No fields found.</p>
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
            {isAuto ? "Auto (active)" : "Reset to auto"}
          </button>
          <span className="text-xs text-[var(--color-fg-muted)]">
            {activeCount} field{activeCount !== 1 ? "s" : ""} selected
          </span>
        </div>
      </PopoverContent>
    </Popover>
  );
}
