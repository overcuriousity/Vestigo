import { useMemo, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { BarChart2, Filter, FilterX } from "lucide-react";
import { Dialog, DialogContent } from "@/components/ui/Dialog";
import { Spinner } from "@/components/ui/Spinner";
import { Tooltip } from "@/components/ui/Tooltip";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/Select";
import { eventsApi } from "@/api/events";
import { vizApi } from "@/api/viz";
import { TimeHistogram } from "@/components/viz/charts/TimeHistogram";
import { ExportControls } from "@/components/viz/ExportControls";
import { buildCaptionLines } from "@/components/viz/lib/caption";
import type { EventFilters } from "@/api/types";

const BUCKET_OPTIONS = [30, 60, 100, 150] as const;

interface Props {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  caseId: string;
  timelineId: string;
  /** The Explorer's currently-effective filters — the histogram/top-list
   * are computed within this same filtered view. */
  filters: EventFilters;
  fieldKey: string;
  value: string;
  onAddFilter?: (fieldKey: string, value: string, include: boolean) => void;
}

/**
 * Per-value histogram modal — opened from a field row's histogram button in
 * the event detail panel. Shows (a) a time histogram of how often
 * `fieldKey = <focused value>` occurs across the currently-filtered range,
 * dimmed-overlaid against the field's total volume, and (b) a scrollable
 * top-values list for `fieldKey` in the same range. Clicking a list row
 * re-focuses the histogram on that value.
 */
export function FieldHistogramModal({
  open,
  onOpenChange,
  caseId,
  timelineId,
  filters,
  fieldKey,
  value,
  onAddFilter,
}: Props) {
  const [activeValue, setActiveValue] = useState(value);
  const [buckets, setBuckets] = useState<(typeof BUCKET_OPTIONS)[number]>(60);
  const svgRef = useRef<SVGSVGElement | null>(null);

  // Reset the focused value whenever the modal is opened for a new field/value.
  const resetKey = `${fieldKey}:${value}`;
  const lastResetKey = useRef(resetKey);
  if (lastResetKey.current !== resetKey) {
    lastResetKey.current = resetKey;
    setActiveValue(value);
  }

  const totalHistogramQuery = useQuery({
    queryKey: ["field-histogram-total", caseId, timelineId, filters, buckets],
    queryFn: () => eventsApi.histogram(caseId, timelineId, filters, buckets),
    enabled: open,
  });

  // The scoped (blue) and total (grey) histograms must share an identical
  // time range and bucket count, or each independently picks its own
  // min/max — a focused value that doesn't span the full filtered range
  // then gets a *different* bin width than the total series, and the two
  // overlays visibly misalign ("distort") instead of lining up. Deriving
  // the scoped range from the already-fetched total range (falling back to
  // any explicit range the Explorer already had set) guarantees both
  // queries compute the same interval.
  const rangeStart = filters.start ?? totalHistogramQuery.data?.min ?? undefined;
  const rangeEnd = filters.end ?? totalHistogramQuery.data?.max ?? undefined;

  const scopedFilters = useMemo<EventFilters>(
    () => ({
      ...filters,
      start: rangeStart,
      end: rangeEnd,
      filters: { ...(filters.filters ?? {}), [fieldKey]: [activeValue] },
    }),
    [filters, fieldKey, activeValue, rangeStart, rangeEnd],
  );

  const histogramQuery = useQuery({
    queryKey: ["field-histogram", caseId, timelineId, scopedFilters, buckets],
    queryFn: () => eventsApi.histogram(caseId, timelineId, scopedFilters, buckets),
    // Wait for the shared range from `totalHistogramQuery` before firing —
    // otherwise the first paint would briefly show a self-ranged (and thus
    // misaligned) scoped histogram before this refetches.
    enabled: open && rangeStart != null && rangeEnd != null,
  });

  const termsQuery = useQuery({
    queryKey: ["field-terms", caseId, timelineId, fieldKey, filters],
    queryFn: () => vizApi.fieldTerms(caseId, timelineId, fieldKey, filters, 50),
    enabled: open,
  });

  const maxTermCount = Math.max(1, ...(termsQuery.data?.values.map((v) => v.count) ?? [1]));
  const captionLines = buildCaptionLines({
    caseId,
    timelineId,
    chartLabel: "Field histogram",
    headerLabel: "field histogram",
    config: {
      v: 1,
      field: fieldKey,
      fieldY: null,
      scale: "nominal",
      chartType: "time",
      metric: "count",
      compare: { mode: "off" },
      options: {},
    },
    filters,
    facts: {
      focusedValue: activeValue,
      intervalSeconds: histogramQuery.data?.interval_seconds,
    },
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={`Histogram: ${fieldKey}`}
        description={`Blue: "${activeValue}" occurrences per bin. Grey: total events per bin, for scale.`}
        className="max-w-6xl w-[92vw]"
      >
        <div className="flex items-center justify-between gap-3 pb-2">
          <div className="flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]">
            <span>Bins:</span>
            <Select
              value={String(buckets)}
              onValueChange={(v) => setBuckets(Number(v) as (typeof BUCKET_OPTIONS)[number])}
            >
              <SelectTrigger className="h-8 w-20 text-xs">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {BUCKET_OPTIONS.map((b) => (
                  <SelectItem key={b} value={String(b)}>
                    {b}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
          <ExportControls
            svgRef={svgRef}
            filename={`${fieldKey}_${activeValue}_histogram`}
            captionLines={captionLines}
          />
        </div>

        <div className="grid grid-cols-[1fr_280px] gap-4">
          <div>
            {histogramQuery.isLoading || totalHistogramQuery.isLoading ? (
              <div className="flex h-[320px] items-center justify-center">
                <Spinner size={20} />
              </div>
            ) : (
              <>
                <TimeHistogram
                  svgRef={svgRef}
                  height={320}
                  buckets={histogramQuery.data?.buckets ?? []}
                  contextBuckets={totalHistogramQuery.data?.buckets}
                />
                <div className="mt-1.5 flex items-center gap-4 text-xs text-[var(--color-fg-muted)]">
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block h-2.5 w-2.5 rounded-sm bg-[var(--color-accent)]" />
                    {fieldKey} = {activeValue}
                  </span>
                  <span className="flex items-center gap-1.5">
                    <span className="inline-block h-2.5 w-2.5 rounded-sm bg-[var(--color-fg-disabled)] opacity-60" />
                    All events (any {fieldKey})
                  </span>
                </div>
              </>
            )}
          </div>

          <div className="flex flex-col">
            <p className="mb-1.5 text-xs font-medium uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Top values
              {termsQuery.data && (
                <span className="ml-1.5 normal-case font-normal text-[var(--color-fg-muted)]">
                  ({termsQuery.data.distinct} distinct)
                </span>
              )}
            </p>
            <div className="max-h-[360px] flex-1 overflow-y-auto pr-1">
              {termsQuery.isLoading ? (
                <div className="flex justify-center py-4">
                  <Spinner size={16} />
                </div>
              ) : (
                <ul className="space-y-0.5">
                  {(termsQuery.data?.values ?? []).map((v) => (
                    <li
                      key={v.value}
                      className={`group flex items-center gap-1.5 rounded px-1 py-0.5 text-xs ${
                        v.value === activeValue
                          ? "bg-[var(--color-accent-dim)]"
                          : "hover:bg-[var(--color-bg-hover)]"
                      }`}
                    >
                      <button
                        className="flex min-w-0 flex-1 items-center gap-1.5 text-left"
                        onClick={() => setActiveValue(v.value)}
                        title="Focus histogram on this value"
                      >
                        <span className="min-w-0 flex-1 truncate" title={v.value}>
                          {v.value}
                        </span>
                        <span className="shrink-0 text-[var(--color-fg-muted)]">{v.count}</span>
                      </button>
                      <div
                        className="h-1.5 w-8 shrink-0 rounded-sm bg-[var(--color-accent)]"
                        style={{ opacity: Math.max(0.15, v.count / maxTermCount) }}
                      />
                      <Tooltip content="Focus histogram" side="top">
                        <button
                          className="shrink-0 rounded p-0.5 opacity-0 group-hover:opacity-100 text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)]"
                          onClick={() => setActiveValue(v.value)}
                        >
                          <BarChart2 size={11} />
                        </button>
                      </Tooltip>
                      {onAddFilter && (
                        <>
                          <Tooltip content={`Filter IN: ${fieldKey} = ${v.value}`} side="top">
                            <button
                              className="shrink-0 rounded p-0.5 opacity-0 group-hover:opacity-100 text-[var(--color-info)] hover:bg-[var(--color-info-dim)]"
                              onClick={() => onAddFilter(fieldKey, v.value, true)}
                            >
                              <Filter size={11} />
                            </button>
                          </Tooltip>
                          <Tooltip content={`Filter OUT: ${fieldKey} ≠ ${v.value}`} side="top">
                            <button
                              className="shrink-0 rounded p-0.5 opacity-0 group-hover:opacity-100 text-[var(--color-danger)] hover:bg-[var(--color-danger-dim)]"
                              onClick={() => onAddFilter(fieldKey, v.value, false)}
                            >
                              <FilterX size={11} />
                            </button>
                          </Tooltip>
                        </>
                      )}
                    </li>
                  ))}
                  {termsQuery.data && termsQuery.data.other_count > 0 && (
                    <li className="px-1 py-1 text-[var(--color-fg-muted)]">
                      + {termsQuery.data.other_count.toLocaleString()} more in other values
                    </li>
                  )}
                </ul>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
