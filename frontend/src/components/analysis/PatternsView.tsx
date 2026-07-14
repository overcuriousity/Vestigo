/**
 * PatternsView — the Patterns tab: latent repeating event sequences (motifs)
 * mined by the sequence_motif detector. Discovery, not anomaly detection —
 * mode-less (no baseline needed), ranked by support × cadence regularity.
 *
 * The analyst's verb here is **Mark routine**: a motif declared routine gets a
 * `kind="routine"` disposition; the backend materializes its occurrences so
 * the event grid can collapse them behind an explicit "N routine events
 * collapsed" count. Routine motifs stay listed here (dimmed, un-markable) —
 * suppression is never silent.
 */
import { Fragment, useCallback, useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { AlertTriangle, ArrowRight, EyeOff, Info, Repeat, Undo2 } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { dispositionsApi } from "@/api/dispositions";
import { jobsApi } from "@/api/jobs";
import { useDisposition } from "@/hooks/useDisposition";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import {
  DetectorStatusLine,
  FindingRowActions,
  FindingShell,
  RefreshButton,
  ResultsBar,
} from "./detector-shared";
import { useCappedFindings, useFindingsLimit, useOpenEvent } from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import type { Event, SequenceMotifFinding } from "@/api/types";
import { anomalyFieldLabel as fieldLabel, truncate } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
}

const SERIES_FIELD_OPTIONS = [
  { value: "artifact", label: "Artifact type", group: "standard" },
  { value: "timestamp_desc", label: "Event category", group: "standard" },
  { value: "display_name", label: "Display name", group: "standard" },
  { value: "parser_name", label: "Parser", group: "standard" },
];

const NGRAM_OPTIONS = [2, 3, 4, 5];

/**
 * Human-readable problem with a routine row's occurrence materialization, or
 * null when it completed cleanly (or hasn't reported yet). The background job
 * persists its outcome to details.materialization — a failed or capped
 * materialization means the grid collapse is inactive or partial, which must
 * be visible on the row, not just in an ephemeral job result.
 */
function materializationIssue(details: Record<string, unknown> | null): string | null {
  const mat = details?.materialization as
    | { status?: string; error?: string; warnings?: string[] }
    | undefined;
  if (!mat) return null;
  if (mat.status === "failed") {
    return `Collapse inactive — materialization failed: ${mat.error ?? "unknown error"}`;
  }
  if (Array.isArray(mat.warnings) && mat.warnings.length > 0) {
    return mat.warnings.join(" ");
  }
  return null;
}

/** Occurrence rows the materialization wrote, or null before it reports. */
function materializedRows(details: Record<string, unknown> | null): number | null {
  const mat = details?.materialization as
    | { status?: string; rows_written?: number }
    | undefined;
  if (mat?.status !== "completed" || typeof mat.rows_written !== "number") return null;
  return mat.rows_written;
}

/**
 * Inline watcher for one routine-materialization background job: polls until
 * it reaches a terminal state, then refreshes the dispositions (so
 * `details.materialization` lands on the routine row) and the event grid
 * (whose collapse just became active). Rendered per active job in the
 * routine-patterns section — the analyst sees "collapsing…" instead of a
 * silently dimming row.
 */
function MaterializationWatch({
  caseId,
  timelineId,
  jobId,
  onDone,
}: {
  caseId: string;
  timelineId: string;
  jobId: string;
  onDone: (jobId: string) => void;
}) {
  const qc = useQueryClient();
  const { data: job } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => jobsApi.get(jobId),
    refetchInterval: (query) => {
      const status = query.state.data?.status;
      return status === "completed" || status === "failed" ? false : 1000;
    },
  });
  const done = job?.status === "completed" || job?.status === "failed";
  useEffect(() => {
    if (!done) return;
    qc.invalidateQueries({ queryKey: ["dispositions", caseId, timelineId] });
    qc.invalidateQueries({ queryKey: ["events"] });
    onDone(jobId);
  }, [done, jobId, caseId, timelineId, qc, onDone]);
  if (!job || done) return null;
  return (
    <div className="flex items-center gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-2 py-1.5 text-xs text-[var(--color-fg-muted)]">
      <Spinner size={11} />
      <span>Collapsing occurrences in the event grid…</span>
    </div>
  );
}

/** Humanize a gap in seconds: 90 → "1.5 min", 300 → "5 min", 7200 → "2 h". */
function fmtPeriod(seconds: number): string {
  if (seconds < 1) return `${(seconds * 1000).toFixed(0)} ms`;
  if (seconds < 90) return `${seconds.toFixed(seconds < 10 ? 1 : 0)} s`;
  if (seconds < 5400) return `${(seconds / 60).toFixed(1).replace(/\.0$/, "")} min`;
  if (seconds < 129_600) return `${(seconds / 3600).toFixed(1).replace(/\.0$/, "")} h`;
  return `${(seconds / 86_400).toFixed(1).replace(/\.0$/, "")} d`;
}

function RegularityBar({ score }: { score: number }) {
  return (
    <span
      className="inline-flex h-1.5 w-16 overflow-hidden rounded bg-[var(--color-bg-elevated)]"
      title={`Regularity ${(score * 100).toFixed(0)}% — 100% is a metronome, 0% shows no rhythm`}
    >
      <span
        className="h-full rounded bg-[var(--color-accent)]"
        style={{ width: `${Math.round(score * 100)}%` }}
      />
    </span>
  );
}

function MotifRow({
  caseId,
  timelineId,
  finding,
  isRoutine,
  onSelectEvent,
  onJumpToTime,
  onRoutineMarked,
}: {
  caseId: string;
  timelineId: string;
  finding: SequenceMotifFinding;
  isRoutine: boolean;
  onSelectEvent: (event: Event) => void;
  onJumpToTime?: (ts: string, eventId?: string, windowEnd?: string) => void;
  /** Called with the materialization job id after a routine mark succeeds. */
  onRoutineMarked: (jobId: string | undefined) => void;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);
  const dispositionMut = useDisposition(caseId, timelineId);

  return (
    <FindingShell
      dismissed={isRoutine}
      details={finding.details}
      onClick={() => {
        if (finding.event_id) openEvent.mutate();
      }}
      actions={
        <>
          {!isRoutine && (
            <button
              title="Mark routine: a real, recurring, expected pattern (cron jobs, heartbeats, poller loops) — its occurrences can be collapsed in the event grid (always with a visible count). Reversible via Unmark below."
              className="rounded p-0.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-accent)]"
              disabled={dispositionMut.isPending}
              onClick={(e) => {
                e.stopPropagation();
                dispositionMut.mutate(
                  {
                    kind: "routine",
                    detector: "sequence_motif",
                    field: finding.field,
                    value: finding.value,
                    details: finding.details,
                  },
                  { onSuccess: (data) => onRoutineMarked(data.materializationJobId) },
                );
              }}
            >
              {dispositionMut.isPending ? <Spinner size={11} /> : <Repeat size={12} />}
            </button>
          )}
          {/* The motif's own jump — targets its first occurrence and spans to
              the last, so the whole recurring pattern highlights in the grid. */}
          <FindingRowActions
            ts={finding.first_seen ?? finding.event?.timestamp}
            eventId={finding.event_id}
            jumpTitle="Jump to this pattern's first occurrence in the grid — highlights its whole first-to-last span, clears active filters (a breadcrumb lets you return)"
            onJumpToTime={
              onJumpToTime
                ? (ts, eventId) => onJumpToTime(ts, eventId, finding.last_seen ?? undefined)
                : undefined
            }
            disposition={{
              caseId,
              timelineId,
              detector: "sequence_motif",
              details: finding.details,
              sourceId: finding.event?.source_id,
            }}
          />
        </>
      }
    >
      {/* The sequence itself */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
          {fieldLabel(finding.field)}
        </span>
        {finding.values.map((v, i) => (
          <Fragment key={i}>
            {i > 0 && <ArrowRight size={10} className="shrink-0 text-[var(--color-fg-muted)]" />}
            <span className="min-w-0 break-all rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs font-medium text-[var(--color-fg-primary)]">
              {truncate(v, 40)}
            </span>
          </Fragment>
        ))}
        {isRoutine && (
          <span className="ml-1 flex items-center gap-1 rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 text-[10px] text-[var(--color-fg-muted)]">
            <EyeOff size={10} />
            routine
          </span>
        )}
      </div>

      {/* Meta line: support · period · regularity · sources */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span>
          ×<strong className="text-[var(--color-fg-secondary)]">{finding.support.toLocaleString()}</strong>
        </span>
        {finding.period_seconds !== null && (
          <span>
            every ~
            <strong className="text-[var(--color-fg-secondary)]">
              {fmtPeriod(finding.period_seconds)}
            </strong>
            {finding.cv !== null && <> (CV {finding.cv})</>}
          </span>
        )}
        <RegularityBar score={finding.regularity_score} />
        {finding.sources_count > 1 && <span>{finding.sources_count} sources</span>}
        {finding.first_seen && <span>first {fmtTs(finding.first_seen)}</span>}
        {finding.last_seen && <span>last {fmtTs(finding.last_seen)}</span>}
      </div>
    </FindingShell>
  );
}

export function PatternsView({ caseId, timelineId, onSelectEvent, onJumpToTime }: Props) {
  const [seriesField, setSeriesField] = useState("artifact");
  const [ngramSize, setNgramSize] = useState(3);
  const [minSupport, setMinSupport] = useState(3);
  const qc = useQueryClient();

  const fl = useFindingsLimit();

  // Dynamic attribute fields extend the grouping-field dropdown (same source
  // as the sequence view's picker).
  const { data: fieldsData } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "fields"],
    queryFn: () => anomaliesApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });
  const seriesFieldOptions = useMemo(() => {
    const attrOptions = (fieldsData?.fields ?? [])
      .filter((f) => f.token.startsWith("attr:"))
      .map((f) => ({
        value: f.token,
        label: f.token.slice(5) + (f.recommended ? "" : " ·"),
        group: "dynamic",
      }));
    return [...SERIES_FIELD_OPTIONS, ...attrOptions];
  }, [fieldsData]);

  // Mode-less mining — deliberately ignores the detector frame/baseline.
  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "sequence_motif", seriesField, ngramSize, minSupport, fl.limit],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "sequence_motif",
        series_field: seriesField,
        ngram_size: ngramSize,
        min_support: minSupport,
        limit: fl.limit,
      }),
    staleTime: 60_000,
  });

  // Active routine dispositions — renders matching motifs dimmed and fills
  // the "Routine patterns" section (with unmark).
  const { data: routineData } = useQuery({
    queryKey: ["dispositions", caseId, timelineId, "routine"],
    queryFn: () => dispositionsApi.list(caseId, timelineId, { kind: "routine", detector: "sequence_motif" }),
  });
  const routineRows = useMemo(() => routineData?.dispositions ?? [], [routineData]);
  const routineValues = useMemo(
    () => new Set(routineRows.map((d) => `${d.field}:${d.value}`)),
    [routineRows],
  );
  const unmarkMut = useMutation({
    mutationFn: (id: string) => dispositionsApi.remove(caseId, timelineId, id),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["dispositions", caseId, timelineId] });
      qc.invalidateQueries({ queryKey: ["events"] });
    },
    meta: { errorTitle: "Couldn't unmark routine" },
  });
  const [routineOpen, setRoutineOpen] = useState(false);

  // Active routine-materialization jobs being watched. A successful mark
  // lands its job id here and pops the routine section open, so the row's
  // new home — and the "collapsing…" progress — are immediately visible.
  const [matJobIds, setMatJobIds] = useState<string[]>([]);
  const handleRoutineMarked = useCallback((jobId: string | undefined) => {
    setRoutineOpen(true);
    if (jobId) setMatJobIds((ids) => (ids.includes(jobId) ? ids : [...ids, jobId]));
  }, []);
  const handleMatDone = useCallback((jobId: string) => {
    setMatJobIds((ids) => ids.filter((id) => id !== jobId));
  }, []);

  const findings = useMemo(
    () =>
      (data?.results ?? []).filter((r): r is SequenceMotifFinding => r.type === "sequence_motif"),
    [data],
  );
  const cap = useCappedFindings(findings);

  return (
    <div className="space-y-3">
      {/* First-run explainer — folds away permanently once dismissed. */}
      <GuidancePanel id="investigate-patterns" title="How pattern mining works">
        <p>
          This tab <strong>discovers repeating event sequences</strong> (motifs) —
          it needs no baseline and detects nothing by itself; it shows the log's
          routine structure so you can separate it from the interesting rest.
        </p>
        <p className="mt-1">
          <strong>Mark routine</strong> when you recognize a sequence as expected
          operations — cron jobs, heartbeats, poller loops, backup runs. Its
          occurrences collapse in the event grid behind a visible "N routine
          events" count, decluttering the timeline without hiding anything.
          Routine patterns stay listed below and can be unmarked anytime.
        </p>
      </GuidancePanel>

      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="shrink-0 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
          Sequence of
        </span>
        <select
          value={seriesField}
          onChange={(e) => setSeriesField(e.target.value)}
          className="min-w-0 flex-1 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-0.5 text-xs text-[var(--color-fg-primary)] focus:border-[var(--color-accent)] focus:outline-none"
        >
          <optgroup label="Standard">
            {seriesFieldOptions
              .filter((o) => o.group === "standard")
              .map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
          </optgroup>
          {seriesFieldOptions.some((o) => o.group === "dynamic") && (
            <optgroup label="Dynamic fields">
              {seriesFieldOptions
                .filter((o) => o.group === "dynamic")
                .map((o) => (
                  <option key={o.value} value={o.value}>
                    {o.label}
                  </option>
                ))}
            </optgroup>
          )}
        </select>
        <span className="flex shrink-0 items-center gap-1 text-xs text-[var(--color-fg-muted)]">
          n =
          <select
            value={ngramSize}
            onChange={(e) => setNgramSize(Number(e.target.value))}
            title="Sequence length — how many consecutive events form one pattern"
            className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1 py-0.5 text-xs text-[var(--color-fg-primary)] focus:border-[var(--color-accent)] focus:outline-none"
          >
            {NGRAM_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </span>
        <span className="flex shrink-0 items-center gap-1 text-xs text-[var(--color-fg-muted)]">
          ≥
          <input
            type="number"
            min={2}
            value={minSupport}
            onChange={(e) => setMinSupport(Math.max(2, Number(e.target.value) || 2))}
            title="Minimum occurrences before a sequence counts as a repeating pattern"
            className="w-12 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1 py-0.5 text-xs text-[var(--color-fg-primary)] focus:border-[var(--color-accent)] focus:outline-none"
          />
          ×
        </span>
        <RefreshButton isFetching={isFetching} onClick={() => refetch()} />
      </div>

      <DetectorStatusLine data={data} baselineLabel="events scanned" />

      {isLoading && (
        <div className="flex justify-center py-6">
          <Spinner size={18} />
        </div>
      )}

      {!isLoading && findings.length === 0 && (
        <div className="flex items-center gap-2 py-4 text-xs text-[var(--color-fg-muted)]">
          <Info size={13} />
          <span>
            {data?.status === "no_data"
              ? "No events ingested yet."
              : `No sequence of ${ngramSize} repeats at least ${minSupport} times for this field — the log has no routine structure at these settings.`}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar
            total={cap.total}
            shownCount={cap.shown.length}
            hasMore={cap.hasMore}
            expanded={cap.expanded}
            onToggle={cap.toggle}
            serverTotal={data?.total_findings}
            onLoadMore={fl.canRaise ? fl.raise : undefined}
            loadingMore={isFetching}
          />
          {cap.shown.map((f, i) => (
            <MotifRow
              key={`${f.field}:${f.value}:${i}`}
              caseId={caseId}
              timelineId={timelineId}
              finding={f}
              isRoutine={routineValues.has(`${f.field}:${f.value}`)}
              onSelectEvent={onSelectEvent}
              onJumpToTime={onJumpToTime}
              onRoutineMarked={handleRoutineMarked}
            />
          ))}
        </div>
      )}

      {/* Routine patterns — collapsed list of the declared-routine motifs. */}
      {routineRows.length > 0 && (
        <div className="border-t border-[var(--color-border)] pt-2">
          <button
            onClick={() => setRoutineOpen((v) => !v)}
            className="mb-1.5 flex w-full items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-secondary)] hover:text-[var(--color-fg-primary)]"
          >
            <Repeat size={12} />
            Routine patterns ({routineRows.length})
          </button>
          {matJobIds.map((jobId) => (
            <MaterializationWatch
              key={jobId}
              caseId={caseId}
              timelineId={timelineId}
              jobId={jobId}
              onDone={handleMatDone}
            />
          ))}
          {routineOpen && (
            <div className="space-y-1">
              {routineRows.map((d) => (
                <div
                  key={d.id}
                  className="flex items-center gap-2 rounded border border-[var(--color-border)] px-2 py-1.5 text-xs"
                >
                  <span className="min-w-0 flex-1 break-all font-mono text-[var(--color-fg-secondary)]">
                    {fieldLabel(d.field ?? "")}: {d.value}
                  </span>
                  {materializedRows(d.details) !== null && (
                    <span
                      className="shrink-0 text-[var(--color-fg-muted)]"
                      title="Occurrence events this pattern collapses in the grid"
                    >
                      {materializedRows(d.details)!.toLocaleString()} collapsed
                    </span>
                  )}
                  {materializationIssue(d.details) && (
                    <span
                      title={materializationIssue(d.details)!}
                      className="shrink-0 text-[var(--color-warning)]"
                    >
                      <AlertTriangle size={12} />
                    </span>
                  )}
                  <button
                    title="Unmark routine — its events reappear in the grid immediately"
                    className="flex shrink-0 items-center gap-1 rounded p-0.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-elevated)] hover:text-[var(--color-fg-primary)]"
                    disabled={unmarkMut.isPending}
                    onClick={() => unmarkMut.mutate(d.id)}
                  >
                    {unmarkMut.isPending ? <Spinner size={11} /> : <Undo2 size={12} />}
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}

      {/* Methodology note */}
      <div className="flex items-start gap-1.5 pt-1 text-xs text-[var(--color-fg-muted)]">
        <Info size={10} className="mt-0.5 shrink-0" />
        <span>
          Per source, events are ordered by time and every run of n consecutive
          values forms one sequence; sequences repeating at least the minimum
          number of times are ranked by volume × rhythm (a pattern recurring on
          a steady period outranks one recurring at random). Mark a pattern{" "}
          <strong>routine</strong> to collapse its events in the grid — the grid
          always shows how many were collapsed.
        </span>
      </div>
    </div>
  );
}
