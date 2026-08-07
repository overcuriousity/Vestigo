/**
 * FindingGroup — one evidence-weight group of the rail: its label, the note
 * saying what kind of claim these are, and the interleaved rows.
 *
 * The note is not decoration. "Odd, not necessarily bad" is the single most
 * important thing the old panel never said, and it belongs next to the rows it
 * qualifies rather than in a methodology tab.
 *
 * Rows reuse FindingShell / FindingRowActions from detector-shared: those
 * carry the disposition controls and their optimistic-update contract with
 * useDisposition, and a second implementation would fork the verdict
 * semantics — the one thing in this subsystem that must stay single-sourced.
 */
import { DETECTORS } from "./detector-registry";
import { FindingRowActions, FindingShell } from "./detector-shared";
import type { EvidenceClass, MethodId, MethodMeta } from "./method-registry";
import type { MethodState } from "@/hooks/useMethodFindings";
import { interleaveByRank, normalizeFinding, type FeedItem } from "@/lib/finding-normalize";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";
import { truncate } from "@/lib/format";
import { isTemplateRow, type LogTemplateRow } from "@/api/analysis";
import type { AnomalyFinding, Event } from "@/api/types";

/**
 * Method ids are the *API* detector keys ("value_novelty"), while
 * `DETECTORS_BY_ID` is keyed by the older UI slug ("novelty"). Index on the
 * `detector` field so the two registries line up — keying on the slug silently
 * yields undefined and renders an empty group.
 */
const DETECTOR_BY_API_KEY = Object.fromEntries(DETECTORS.map((d) => [d.detector, d]));

interface Props {
  evidenceClass: { id: EvidenceClass; label: string; note: string };
  methods: MethodMeta[];
  byMethod: Record<MethodId, MethodState>;
  caseId: string;
  timelineId: string;
  onSelectFinding: (method: MethodId, rank: number) => void;
  onSelectEvent: (event: Event) => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
}

function TemplateRows({
  rows,
  onSelect,
}: {
  rows: LogTemplateRow[];
  onSelect: (rank: number) => void;
}) {
  return (
    <>
      {rows.map((row, i) => (
        <FindingShell key={row.template_hash ?? i} details={{}} onClick={() => onSelect(i)}>
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="shrink-0 rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-[10px] font-medium text-[var(--color-fg-muted)]">
              Log templates
            </span>
            <span className="min-w-0 break-all font-mono text-xs font-medium text-[var(--color-fg-primary)]">
              {truncate(row.template, 90)}
            </span>
          </div>
          <div className="text-xs text-[var(--color-fg-muted)]">×{row.count} events</div>
        </FindingShell>
      ))}
    </>
  );
}

function ScoredRow({
  item,
  caseId,
  timelineId,
  onSelect,
  onJumpToTime,
}: {
  item: FeedItem;
  caseId: string;
  timelineId: string;
  onSelect: () => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
}) {
  const Icon = item.icon;
  return (
    <FindingShell
      dismissed={item.raw.dismissed}
      confirmed={item.raw.confirmed}
      details={item.raw.details}
      onClick={onSelect}
      actions={
        <FindingRowActions
          ts={item.ts}
          eventId={item.eventId}
          onJumpToTime={
            onJumpToTime
              ? (ts, eventId) =>
                  onJumpToTime(
                    ts,
                    eventId,
                    item.raw.type === "frequency" ? item.raw.window_end : undefined,
                  )
              : undefined
          }
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

export function FindingGroup({
  evidenceClass,
  methods,
  byMethod,
  caseId,
  timelineId,
  onSelectFinding,
  onJumpToTime,
}: Props) {
  const scored = methods.filter((m) => m.id !== "log_template");
  const templates = methods.find((m) => m.id === "log_template");

  // Interleave by per-detector rank: scores are incomparable across methods
  // (surprise, |z|, G, −log₁₀ p, seconds of skew), so every method's best
  // finding comes first rather than a fabricated common scale.
  const lists = scored.map((meta) => {
    const detectorMeta = DETECTOR_BY_API_KEY[meta.id];
    if (!detectorMeta) return [];
    return (byMethod[meta.id]?.findings ?? [])
      .filter((f): f is AnomalyFinding => !isTemplateRow(f))
      .map((f, rank) => normalizeFinding(detectorMeta, f, rank));
  });
  const items = interleaveByRank(lists);
  // Log templates are shapes to read, not scored findings, so they bypass
  // `normalizeFinding`'s exhaustive scored-finding union entirely.
  const templateRows = (templates ? (byMethod[templates.id]?.findings ?? []) : []).filter(
    isTemplateRow,
  );

  if (items.length === 0 && templateRows.length === 0) return null;

  return (
    <section className="mt-3 first:mt-0">
      <h4
        data-testid="evidence-group"
        className="mb-1.5 flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]"
      >
        {evidenceClass.label}
        <span className="font-normal normal-case tracking-normal">— {evidenceClass.note}</span>
        <span className="ml-auto font-mono text-[var(--color-fg-disabled)]">
          {items.length + templateRows.length}
        </span>
      </h4>
      <div className="space-y-1.5">
        {items.map((item, i) => (
          <ScoredRow
            key={`${item.detectorId}:${item.rank}:${i}`}
            item={item}
            caseId={caseId}
            timelineId={timelineId}
            // `detector` is the API key the method registry is keyed on;
            // `detectorId` is the older UI slug and would not resolve.
            onSelect={() => onSelectFinding(item.detector as MethodId, item.rank)}
            onJumpToTime={onJumpToTime}
          />
        ))}
        {templates && (
          <TemplateRows
            rows={templateRows}
            onSelect={(rank) => onSelectFinding(templates.id, rank)}
          />
        )}
      </div>
    </section>
  );
}
