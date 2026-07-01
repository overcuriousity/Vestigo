/**
 * TimelineHistogram — compact event-count bar chart above the event grid.
 *
 * Fetches /histogram (respects all active filters so the chart always mirrors
 * the current view).  Click a bar to zoom to that time bucket; drag across
 * bars to select a time span.
 *
 * No chart dependency — hand-rolled div bars (airgap-safe).
 *
 * Brush state uses refs (not React state) so mouseup reliably reads the
 * current selection even when no re-render has occurred since mousedown.
 */
import { useState, useRef, useCallback, useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import { eventsApi } from "@/api/events";
import { Spinner } from "@/components/ui/Spinner";
import type { AnomalyMarker, EventFilters, HistogramBucket } from "@/api/types";
import { cn } from "@/lib/cn";

interface Props {
  caseId: string;
  timelineId: string;
  filters: EventFilters;
  onRangeSelect: (start: string, end: string) => void;
  /** Anomaly finding timestamps overlaid as vertical lines on the chart. */
  markers?: AnomalyMarker[];
  /** Timestamp of the row currently scrolled into view in the event grid. */
  currentPositionTs?: string | null;
  /** Persistent time-window overlay (e.g. a Frequency finding's anomalous window). */
  highlightRange?: { start: string; end: string } | null;
}

/** Where a marker's timestamp falls relative to the rendered bars. */
interface PlottedMarker {
  /** Clamped to [0, 100] so the indicator is always visible. */
  pct: number;
  /** True when the real timestamp falls outside the visible range (pinned to an edge). */
  offscreen: boolean;
}

/**
 * Map a timestamp onto the chart's x-axis using the *same coordinate system
 * as the bars themselves* — bucket index, not a raw time fraction.
 *
 * The bars are equal-width flex items, one per bucket. ClickHouse's
 * `toStartOfInterval` bucketing aligns bucket boundaries to the interval
 * grid, not to `data.min`/`data.max` (the true first/last event timestamps) —
 * so the first bucket can start before `data.min`, and the last bucket can be
 * a partial interval. Positioning a marker by linearly interpolating between
 * `data.min` and `data.max` therefore drifts from the bar it visually belongs
 * to (worse at deeper zooms, where bucket count is small and any drift is a
 * larger fraction of the chart). Interpolating within the marker's actual
 * bucket index instead guarantees exact agreement with the bars.
 */
function plotMarker(
  ts: string,
  buckets: HistogramBucket[],
  intervalSeconds: number,
): PlottedMarker | null {
  if (buckets.length === 0) return null;
  const t = new Date(ts).getTime();
  if (Number.isNaN(t)) return null;

  const n = buckets.length;
  const firstStart = new Date(buckets[0].start).getTime();
  const lastEnd = new Date(buckets[n - 1].start).getTime() + intervalSeconds * 1000;

  // Find the last bucket whose start is <= t (buckets are ordered ascending).
  let idx = -1;
  for (let i = 0; i < n; i++) {
    if (new Date(buckets[i].start).getTime() <= t) idx = i;
    else break;
  }

  if (idx === -1) {
    return { pct: 0, offscreen: t < firstStart };
  }

  const bucketStart = new Date(buckets[idx].start).getTime();
  const fracWithinBucket =
    intervalSeconds > 0 ? Math.min(1, Math.max(0, (t - bucketStart) / (intervalSeconds * 1000))) : 0;
  const rawPct = ((idx + fracWithinBucket) / n) * 100;
  return { pct: Math.max(0, Math.min(100, rawPct)), offscreen: t > lastEnd };
}

/** Add `seconds` to an ISO string and return a UTC ISO string. */
function addSeconds(iso: string, seconds: number): string {
  return new Date(new Date(iso).getTime() + seconds * 1000).toISOString();
}

/** Short, human-readable label for a UTC ISO datetime string. */
function fmtShort(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

export function TimelineHistogram({
  caseId,
  timelineId,
  filters,
  onRangeSelect,
  markers,
  currentPositionTs,
  highlightRange,
}: Props) {
  const { data, isLoading, isFetching } = useQuery({
    queryKey: ["histogram", caseId, timelineId, filters],
    queryFn: () => eventsApi.histogram(caseId, timelineId, filters),
    staleTime: 30_000,
    placeholderData: (prev) => prev,
  });

  // Brush indices are kept in refs so handleMouseUp always reads the latest
  // values synchronously, even before React commits a re-render from mousedown.
  const brushStartRef = useRef<number | null>(null);
  const brushEndRef = useRef<number | null>(null);
  const isDragging = useRef(false);

  // Only the visual highlight and tooltip need state (drives re-renders).
  const [brushRange, setBrushRange] = useState<{ lo: number; hi: number } | null>(null);
  const [tooltip, setTooltip] = useState<{
    x: number;
    text: string;
  } | null>(null);

  const buckets = useMemo(() => data?.buckets ?? [], [data]);
  const maxCount = Math.max(1, ...buckets.map((b: HistogramBucket) => b.count));

  const applyBrush = useCallback(
    (startIdx: number, endIdx: number) => {
      if (!data || buckets.length === 0) return;
      const lo = Math.min(startIdx, endIdx);
      const hi = Math.max(startIdx, endIdx);
      const startBucket = buckets[lo];
      const endBucket = buckets[hi];
      if (!startBucket || !endBucket) return;
      const start = startBucket.start;
      const end = addSeconds(endBucket.start, data.interval_seconds);
      onRangeSelect(start, end);
    },
    [buckets, data, onRangeSelect],
  );

  /** Zoom to a window centered on a marker's timestamp — the click target for anomaly flags. */
  const jumpToMarker = useCallback(
    (ts: string) => {
      // Same staleness guard as handleMouseDown — data.interval_seconds/min/max
      // could still belong to the previous zoom while a refetch is pending.
      if (!data || isFetching) return;
      const minT = data.min ? new Date(data.min).getTime() : null;
      const maxT = data.max ? new Date(data.max).getTime() : null;
      const span = minT !== null && maxT !== null ? maxT - minT : data.interval_seconds * 1000 * 60;
      const padSeconds = Math.max(data.interval_seconds * 5, (span / 1000) * 0.05);
      onRangeSelect(addSeconds(ts, -padSeconds), addSeconds(ts, padSeconds));
    },
    [data, isFetching, onRangeSelect],
  );

  const handleMouseDown = useCallback(
    (idx: number) => {
      // Refuse to start a new brush against bars that may still be rendered
      // from a previous zoom's placeholder data — a fetch is in flight to
      // replace them. Starting here would compute the new range from stale
      // bucket boundaries and jump somewhere unrelated to what's on screen.
      if (isFetching) return;
      isDragging.current = true;
      brushStartRef.current = idx;
      brushEndRef.current = idx;
      setBrushRange({ lo: idx, hi: idx });
    },
    [isFetching],
  );

  const handleMouseEnter = useCallback(
    (idx: number, xOffset: number, bucket: HistogramBucket) => {
      setTooltip({
        x: xOffset,
        text: `${fmtShort(bucket.start)} — ${bucket.count.toLocaleString()} events`,
      });
      if (isDragging.current && brushStartRef.current !== null) {
        brushEndRef.current = idx;
        const lo = Math.min(brushStartRef.current, idx);
        const hi = Math.max(brushStartRef.current, idx);
        setBrushRange({ lo, hi });
      }
    },
    [],
  );

  const handleMouseUp = useCallback(() => {
    if (!isDragging.current || brushStartRef.current === null) return;
    isDragging.current = false;
    const startIdx = brushStartRef.current;
    const endIdx = brushEndRef.current ?? startIdx;
    brushStartRef.current = null;
    brushEndRef.current = null;
    setBrushRange(null);
    applyBrush(startIdx, endIdx);
  }, [applyBrush]);

  const handleContainerMouseLeave = useCallback(() => {
    setTooltip(null);
    if (isDragging.current) {
      // Cancelled drag — commit whatever was selected so far.
      const startIdx = brushStartRef.current;
      const endIdx = brushEndRef.current;
      isDragging.current = false;
      brushStartRef.current = null;
      brushEndRef.current = null;
      setBrushRange(null);
      if (startIdx !== null && endIdx !== null) {
        applyBrush(startIdx, endIdx);
      }
    }
  }, [applyBrush]);

  if (isLoading && !data) {
    return (
      <div className="flex h-16 items-center justify-center border-b border-[var(--color-border)] bg-[var(--color-bg-surface)]">
        <Spinner size={14} />
      </div>
    );
  }

  if (!data || buckets.length === 0) {
    return (
      <div className="flex h-10 items-center border-b border-[var(--color-border)] bg-[var(--color-bg-surface)] px-3">
        <span className="text-xs text-[var(--color-fg-muted)]">No events to display in histogram</span>
      </div>
    );
  }

  return (
    <div
      className="relative shrink-0 border-b border-[var(--color-border)] bg-[var(--color-bg-surface)] select-none"
      onMouseUp={handleMouseUp}
      onMouseLeave={handleContainerMouseLeave}
    >
      {/* Bars — dimmed and non-interactive while a zoom refetch is in flight,
          so clicks can never land against stale placeholder bucket data. */}
      <div
        className={cn(
          "flex h-16 items-end gap-px px-2 pt-2 pb-0 transition-opacity",
          isFetching && "pointer-events-none opacity-50",
        )}
      >
        {buckets.map((bucket: HistogramBucket, idx: number) => {
          const heightPct = Math.max(4, (bucket.count / maxCount) * 100);
          const isInBrush =
            brushRange !== null && idx >= brushRange.lo && idx <= brushRange.hi;

          return (
            <div
              key={bucket.start}
              className="relative flex-1 cursor-crosshair"
              style={{ height: "100%", display: "flex", alignItems: "flex-end" }}
              onMouseDown={() => handleMouseDown(idx)}
              onMouseEnter={(e) => {
                const containerEl = e.currentTarget.closest<HTMLElement>(".relative");
                const containerLeft = containerEl?.getBoundingClientRect().left ?? 0;
                const rect = e.currentTarget.getBoundingClientRect();
                const xOffset = rect.left - containerLeft + rect.width / 2;
                handleMouseEnter(idx, xOffset, bucket);
              }}
            >
              <div
                className={cn(
                  "w-full rounded-t-[1px] transition-colors",
                  isInBrush
                    ? "bg-[var(--color-accent)]"
                    : "bg-[var(--color-accent)] opacity-30 hover:opacity-60",
                )}
                style={{ height: `${heightPct}%` }}
              />
            </div>
          );
        })}
      </div>

      {/* Anomaly markers — a clickable flag in the top margin (never overlaps
          the bars) plus a click-through guide line so bins stay clickable. */}
      {markers && markers.length > 0 && (
        <div className="pointer-events-none absolute inset-x-0 top-0 h-16 px-2">
          <div className="relative h-full w-full">
            {markers.map((m, i) => {
              const plotted = plotMarker(m.ts, buckets, data.interval_seconds);
              if (!plotted) return null;
              return (
                <div
                  key={i}
                  className="absolute top-0 bottom-0"
                  style={{ left: `${plotted.pct}%`, opacity: plotted.offscreen ? 0.4 : 1 }}
                >
                  {/* Guide line — pointer-events-none so it never blocks bin clicks */}
                  <div className="pointer-events-none absolute top-2 bottom-0 w-px -translate-x-1/2 bg-[var(--color-anomaly)]" />
                  {/* Flag — the only clickable/hoverable part, sits in the top margin above the bars */}
                  <button
                    type="button"
                    title={
                      plotted.offscreen
                        ? `${m.label} (outside current view — click to jump)`
                        : `${m.label} — click to zoom in`
                    }
                    onClick={(e) => {
                      e.stopPropagation();
                      jumpToMarker(m.ts);
                    }}
                    disabled={isFetching}
                    className={cn(
                      "absolute top-0 h-2 w-2 -translate-x-1/2 rounded-full border border-[var(--color-bg-surface)] bg-[var(--color-anomaly)] transition-transform",
                      isFetching
                        ? "pointer-events-none opacity-50"
                        : "pointer-events-auto cursor-pointer hover:scale-125",
                    )}
                  />
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Persistent window highlight (e.g. a Frequency finding's anomalous
          window) — visually distinct from the transient brush-drag rectangle,
          which is cleared on mouseup. */}
      {highlightRange && (() => {
        const startPlotted = plotMarker(highlightRange.start, buckets, data.interval_seconds);
        const endPlotted = plotMarker(highlightRange.end, buckets, data.interval_seconds);
        if (!startPlotted || !endPlotted) return null;
        const left = Math.min(startPlotted.pct, endPlotted.pct);
        const width = Math.max(0.5, Math.abs(endPlotted.pct - startPlotted.pct));
        return (
          <div
            className="pointer-events-none absolute inset-x-0 top-0 h-16 px-2"
            title="Anomalous window"
          >
            <div
              className="absolute top-0 bottom-0 border-x border-dashed border-[var(--color-accent)]/70 bg-[var(--color-accent)]/10"
              style={{ left: `${left}%`, width: `${width}%` }}
            />
          </div>
        );
      })()}

      {/* Current scroll position — where the event grid is currently scrolled to */}
      {currentPositionTs && (() => {
        const plotted = plotMarker(currentPositionTs, buckets, data.interval_seconds);
        if (!plotted) return null;
        return (
          <div
            className="pointer-events-none absolute inset-x-0 top-0 h-16 px-2"
            title="Current scroll position"
          >
            <div
              className="absolute top-0 bottom-0 w-px -translate-x-1/2 bg-[var(--color-info)]"
              style={{ left: `${plotted.pct}%`, opacity: plotted.offscreen ? 0.4 : 0.9 }}
            />
          </div>
        );
      })()}

      {/* X-axis labels */}
      <div className="flex justify-between px-2 pb-1 text-[11px] text-[var(--color-fg-muted)]">
        <span>{data.min ? fmtShort(data.min) : ""}</span>
        <span>
          {buckets[Math.floor(buckets.length / 2)]
            ? fmtShort(buckets[Math.floor(buckets.length / 2)].start)
            : ""}
        </span>
        <span>{data.max ? fmtShort(data.max) : ""}</span>
      </div>

      {/* Tooltip */}
      {tooltip && (
        <div
          className="pointer-events-none absolute bottom-full mb-1 -translate-x-1/2 rounded bg-[var(--color-bg-elevated)] border border-[var(--color-border)] px-2 py-1 text-[11px] text-[var(--color-fg-primary)] whitespace-nowrap shadow"
          style={{ left: tooltip.x + 8 }}
        >
          {tooltip.text}
        </div>
      )}
    </div>
  );
}
