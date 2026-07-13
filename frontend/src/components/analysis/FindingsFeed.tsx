/**
 * FindingsFeed — the default Anomalies view: one cross-detector ranked inbox
 * built from the detector sweep (the same fetch that feeds the accordion's
 * count badges — nothing extra is paid for this view).
 *
 * Ranking is per-detector rank interleave (see lib/finding-normalize.ts):
 * detector scores are incomparable, so every detector's best finding surfaces
 * first and each row shows its raw score with a unit label. Detector chips
 * filter the feed; the per-detector views (with their field pickers and
 * knobs) live under the "Advanced" expander and stay authoritative.
 */
import { useMemo, useState } from "react";
import { Info } from "lucide-react";
import { Spinner } from "@/components/ui/Spinner";
import { DETECTORS, type DetectorId } from "./detector-registry";
import { SWEEP_LIMIT, useAnomalyMarkers, useCappedFindings, useDetectorSweep, useOpenEvent } from "./detector-hooks";
import { FindingRowActions, FindingShell, NeedsBaselinePrompt, RefreshButton, ResultsBar } from "./detector-shared";
import { interleaveByRank, normalizeFinding, type FeedItem } from "@/lib/finding-normalize";
import { useTriageCoverage } from "@/hooks/useTriageCoverage";
import { cn } from "@/lib/cn";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";
import type { AnomalyMarker, Event } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
  /** Publish the feed's findings as histogram/grid markers (see useAnomalyMarkers). */
  onAnomalyMarkers?: (markers: AnomalyMarker[]) => void;
}

function FeedRow({
  caseId,
  timelineId,
  item,
  onSelectEvent,
  onJumpToTime,
}: Props & { item: FeedItem }) {
  const openEvent = useOpenEvent(caseId, timelineId, item.eventId, onSelectEvent);
  const Icon = item.icon;
  return (
    <FindingShell
      dismissed={item.raw.dismissed}
      details={item.raw.details}
      onClick={() => {
        if (item.eventId) openEvent.mutate();
      }}
      actions={
        <FindingRowActions
          ts={item.ts}
          eventId={item.eventId}
          onJumpToTime={onJumpToTime}
          disposition={{
            caseId,
            timelineId,
            detector: item.detector,
            details: item.raw.details,
            sourceId: item.sourceId,
          }}
        />
      }
    >
      <div className="flex flex-wrap items-center gap-1.5">
        <span
          className="flex shrink-0 items-center gap-1 rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-fg-muted)]"
          title={item.detectorLabel}
        >
          <Icon size={10} />
          {item.detectorLabel}
        </span>
        <span className="min-w-0 break-all font-mono text-xs font-medium text-[var(--color-fg-primary)]">
          {item.title}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span className="min-w-0 break-all">{item.subtitle}</span>
        <span className="shrink-0">
          <strong className="text-[var(--color-fg-secondary)]">{item.scoreRaw.toFixed(2)}</strong>{" "}
          {item.scoreUnit}
        </span>
        {item.ts && <span className="shrink-0">{fmtTs(item.ts)}</span>}
      </div>
    </FindingShell>
  );
}

export function FindingsFeed({ caseId, timelineId, onSelectEvent, onJumpToTime, onAnomalyMarkers }: Props) {
  const sweep = useDetectorSweep(caseId, timelineId);
  const { summary } = useTriageCoverage(caseId, timelineId);
  const [activeChips, setActiveChips] = useState<Set<DetectorId>>(new Set());

  const perDetector = useMemo(() => {
    if (!sweep.data) return [];
    return DETECTORS.map((meta) => {
      const response = sweep.data![meta.id];
      const items = (response?.results ?? []).map((f, rank) => normalizeFinding(meta, f, rank));
      return {
        meta,
        error: response === null,
        items,
        // Post-suppression finding count before the sweep's limit cap — what
        // the chip badge shows; `items.length` is only the fetched slice.
        total: response?.total_findings ?? items.length,
      };
    });
  }, [sweep.data]);

  const chipFiltered = useMemo(
    () => perDetector.filter((d) => activeChips.size === 0 || activeChips.has(d.meta.id)),
    [perDetector, activeChips],
  );

  const feed = useMemo(
    () => interleaveByRank(chipFiltered.map((d) => d.items)),
    [chipFiltered],
  );
  const serverTotal = useMemo(
    () => chipFiltered.reduce((sum, d) => sum + d.total, 0),
    [chipFiltered],
  );

  const cap = useCappedFindings(feed, 30);

  // Default marker publisher: every fetched finding with a timestamp lands on
  // the histogram/grid, across all detectors (chip filters intentionally do
  // not narrow the overlay — they're a reading aid, not a scope change).
  const allItems = useMemo(() => perDetector.flatMap((d) => d.items), [perDetector]);
  useAnomalyMarkers(
    allItems,
    (item) =>
      item.ts
        ? {
            ts: item.ts,
            label: item.title,
            detail: `${item.detectorLabel}: ${item.title} — ${item.subtitle}`,
            eventId: item.eventId,
            sourceId: item.sourceId,
            detector: item.detector as AnomalyMarker["detector"],
            rawDetails: item.raw.details,
            windowEnd: item.raw.type === "frequency" ? item.raw.window_end : undefined,
          }
        : null,
    onAnomalyMarkers,
  );

  if (sweep.needsBaseline) return <NeedsBaselinePrompt />;

  const toggleChip = (id: DetectorId) =>
    setActiveChips((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  return (
    <div className="space-y-2">
      {/* Triage coverage — reviewed vs. the current finding population.
          "≥" marks truncated sweeps (coverage checked on fetched slices only). */}
      {summary.denominator > 0 && (
        <p className="text-[11px] text-[var(--color-fg-muted)]">
          <span className="font-mono font-semibold text-[var(--color-fg-secondary)]">
            {summary.anyTruncated ? "≥" : ""}
            {summary.reviewed}/{summary.denominator}
          </span>{" "}
          findings reviewed
          {summary.anyTruncated && (
            <span> (coverage checked against the top {SWEEP_LIMIT} findings per detector)</span>
          )}
        </p>
      )}

      {/* Detector chips — count per detector, toggling filters the feed. */}
      <div className="flex flex-wrap items-center gap-1">
        {perDetector.map(({ meta, total, error }) => {
          const active = activeChips.size === 0 || activeChips.has(meta.id);
          return (
            <button
              key={meta.id}
              onClick={() => toggleChip(meta.id)}
              title={`${meta.hint}${activeChips.has(meta.id) ? " — click to remove filter" : " — click to filter the feed"}`}
              className={cn(
                "flex items-center gap-1 rounded-full border px-2 py-0.5 text-[10px] font-medium transition-colors",
                activeChips.has(meta.id)
                  ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-accent)]"
                  : active
                    ? "border-[var(--color-border)] text-[var(--color-fg-secondary)] hover:border-[var(--color-border-focus)]"
                    : "border-[var(--color-border)] text-[var(--color-fg-muted)]",
              )}
            >
              <meta.icon size={10} />
              {meta.label}
              {error ? (
                <span className="text-[var(--color-warning)]">err</span>
              ) : (
                <span
                  className={cn(
                    "font-mono",
                    total > 0 ? "text-[var(--color-anomaly)]" : "text-[var(--color-fg-muted)]",
                  )}
                >
                  {total}
                </span>
              )}
            </button>
          );
        })}
        <RefreshButton isFetching={sweep.isFetching} onClick={() => sweep.refetch()} />
      </div>

      {sweep.isLoading && (
        <div className="flex justify-center py-6">
          <Spinner size={18} />
        </div>
      )}

      {!sweep.isLoading && feed.length === 0 && (
        <div className="flex items-center gap-2 py-4 text-xs text-[var(--color-fg-muted)]">
          <Info size={13} />
          <span>No findings under the current frame{activeChips.size > 0 ? " and chip filter" : ""}.</span>
        </div>
      )}

      {feed.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar
            total={cap.total}
            shownCount={cap.shown.length}
            hasMore={cap.hasMore}
            expanded={cap.expanded}
            onToggle={cap.toggle}
            // "N of M findings" when the sweep's per-detector limit truncated —
            // the per-detector Advanced views can raise their own limits.
            serverTotal={serverTotal}
          />
          {cap.shown.map((item, i) => (
            <FeedRow
              key={`${item.detectorId}:${item.rank}:${i}`}
              caseId={caseId}
              timelineId={timelineId}
              item={item}
              onSelectEvent={onSelectEvent}
              onJumpToTime={onJumpToTime}
            />
          ))}
        </div>
      )}

      <p className="flex items-start gap-1.5 pt-1 text-[11px] text-[var(--color-fg-muted)]">
        <Info size={10} className="mt-0.5 shrink-0" />
        <span>
          Best finding of every detector first (scores are not comparable across
          detectors — each shows its own unit). Use <strong>Advanced</strong> below
          for a detector's full view with field selection and tuning.
        </span>
      </p>
    </div>
  );
}
