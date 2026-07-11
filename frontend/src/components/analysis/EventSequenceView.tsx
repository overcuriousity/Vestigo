/**
 * EventSequenceView — event-order n-grams that occur in a suspect window but
 * never in the baseline window (sequence_novelty detector).
 *
 * Per source, events are ordered by time and every run of n consecutive
 * values of the grouping field forms one n-gram; an n-gram absent from the
 * baseline is flagged with a surprise score against the suspect window's own
 * n-gram total. Temporal-only — like proportion shift it requires the
 * "Compare windows" frame with an active baseline definition.
 */
import { Fragment, useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info, ArrowRight } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import {
  DetectorStatusLine,
  FindingRowActions,
  FindingShell,
  NeedsBaselinePrompt,
  ResultsBar,
  RefreshButton,
  TagFindingsBar,
} from "./detector-shared";
import {
  useCappedFindings,
  useFindingsLimit,
  useShowDismissed,
  useBaselineRequest,
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import type { AnomalyMarker, Event, SequenceNoveltyFinding } from "@/api/types";
import { anomalyFieldLabel as fieldLabel, truncate } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  onFindingsChange?: (markers: AnomalyMarker[]) => void;
  onRunIdChange?: (runId: string | undefined) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}

const SERIES_FIELD_OPTIONS = [
  { value: "artifact", label: "Artifact type", group: "standard" },
  { value: "timestamp_desc", label: "Event category", group: "standard" },
  { value: "display_name", label: "Display name", group: "standard" },
  { value: "parser_name", label: "Parser", group: "standard" },
];

const NGRAM_OPTIONS = [2, 3, 4, 5];

function SequenceRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  finding: SequenceNoveltyFinding;
  onSelectEvent: (event: Event) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);
  const windowTotal = finding.details["window_ngram_total"] as number | undefined;

  return (
    <FindingShell
      dismissed={finding.dismissed}
      details={finding.details}
      onClick={() => {
        if (finding.event_id) openEvent.mutate();
      }}
      actions={
        <FindingRowActions
          ts={finding.event?.timestamp ?? finding.first_seen}
          eventId={finding.event_id}
          onJumpToTime={onJumpToTime}
          disposition={{
            caseId,
            timelineId,
            detector: "sequence_novelty",
            details: finding.details,
            sourceId: finding.event?.source_id,
          }}
        />
      }
    >
      {/* Field + the sequence itself */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
          {fieldLabel(finding.field)}
        </span>
        {finding.values.map((v, i) => (
          <Fragment key={i}>
            {i > 0 && (
              <ArrowRight size={10} className="shrink-0 text-[var(--color-fg-muted)]" />
            )}
            <span className="min-w-0 break-all rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs font-medium text-[var(--color-fg-primary)]">
              {truncate(v, 40)}
            </span>
          </Fragment>
        ))}
      </div>

      {/* Meta line */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span>
          never in baseline; ×
          <strong className="text-[var(--color-fg-secondary)]">{finding.count}</strong> in{" "}
          {String(finding.details["window_label"] ?? "the suspect window")}
          {windowTotal !== undefined && <> (of {windowTotal.toLocaleString()} sequences)</>}
        </span>
        <span>
          surprise{" "}
          <strong className="text-[var(--color-fg-secondary)]">{finding.score.toFixed(2)}</strong>
        </span>
        {finding.first_seen && <span>first {fmtTs(finding.first_seen)}</span>}
      </div>
    </FindingShell>
  );
}

export function EventSequenceView({
  caseId,
  timelineId,
  onSelectEvent,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();
  // Temporal-only, like proportion shift: gate on the frame itself too —
  // there is no self-baseline mode to fall back to.
  const frame = useBaselineStore((s) => s.frame);
  const [seriesField, setSeriesField] = useState("artifact");
  const [ngramSize, setNgramSize] = useState(3);
  const qc = useQueryClient();

  const fl = useFindingsLimit();
  const sd = useShowDismissed();
  const enabled = frame === "baseline" && !needsBaseline;

  // Dynamic attribute fields extend the grouping-field dropdown (same source
  // as the frequency view's GROUP BY picker).
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

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "sequence_novelty", seriesField, ngramSize, blKey, fl.limit, sd.enabled],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "sequence_novelty",
        series_field: seriesField,
        ngram_size: ngramSize,
        limit: fl.limit,
        ...blParams,
        ...(sd.enabled ? { include_dismissed: true } : {}),
      }),
    staleTime: 60_000,
    enabled,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "sequence_novelty",
        series_field: seriesField,
        ngram_size: ngramSize,
        limit: fl.limit,
        ...blParams,
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = useMemo(
    () =>
      (data?.results ?? []).filter(
        (r): r is SequenceNoveltyFinding => r.type === "sequence_novelty",
      ),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f) => {
      const ts = f.event?.timestamp ?? f.first_seen;
      if (!ts) return null;
      const label = `${fieldLabel(f.field)}: ${truncate(f.value, 60)}`;
      const detail =
        `New sequence: ${f.value} — this ${f.values.length}-event order never occurs in the ` +
        `baseline window; ×${f.count} in ${String(f.details["window_label"] ?? "the suspect window")} ` +
        `(surprise ${f.score.toFixed(2)})`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "sequence_novelty" as const,
        rawDetails: f.details,
        windowEnd: null,
      };
    },
    onFindingsChange,
  );

  useDetectorRunId(data?.run_id, onRunIdChange);

  const cap = useCappedFindings(findings);

  if (frame !== "baseline") {
    return (
      <div className="flex items-start gap-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 text-xs text-[var(--color-fg-muted)]">
        <AlertTriangle size={13} className="mt-0.5 shrink-0 text-[var(--color-warning)]" />
        <span>
          Event sequences flags orderings never seen in a baseline, so it always
          needs one — switch the frame to <strong>Compare windows</strong> and
          pick a baseline definition. It has no scan-all-events mode.
        </span>
      </div>
    );
  }
  if (needsBaseline) return <NeedsBaselinePrompt />;

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-muted)] shrink-0">
          Sequence of
        </span>
        <select
          value={seriesField}
          onChange={(e) => setSeriesField(e.target.value)}
          className="flex-1 min-w-0 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-0.5 text-xs text-[var(--color-fg-primary)] focus:outline-none focus:border-[var(--color-accent)]"
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
        <span className="flex items-center gap-1 text-xs text-[var(--color-fg-muted)] shrink-0">
          n =
          <select
            value={ngramSize}
            onChange={(e) => setNgramSize(Number(e.target.value))}
            title="Sequence length — how many consecutive events form one n-gram"
            className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1 py-0.5 text-xs text-[var(--color-fg-primary)] focus:outline-none focus:border-[var(--color-accent)]"
          >
            {NGRAM_OPTIONS.map((n) => (
              <option key={n} value={n}>
                {n}
              </option>
            ))}
          </select>
        </span>
        <RefreshButton isFetching={isFetching} onClick={() => refetch()} />
      </div>

      <DetectorStatusLine data={data} />

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
              ? "No event sequences. No events ingested yet."
              : data?.status === "insufficient_data"
                ? "Nothing to compare — the baseline window has no complete sequences of this length for the chosen field."
                : "Every event ordering in the suspect windows also occurs in the baseline."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar total={cap.total} shownCount={cap.shown.length} hasMore={cap.hasMore} expanded={cap.expanded} onToggle={cap.toggle} serverTotal={data?.total_findings} onLoadMore={fl.canRaise ? fl.raise : undefined} loadingMore={isFetching} dismissedCount={data?.dismissed_count} showDismissed={sd.enabled} onToggleDismissed={sd.toggle} />
          {cap.shown.map((f, i) => (
            <SequenceRow
              key={`${f.field}:${f.value}:${i}`}
              caseId={caseId}
              timelineId={timelineId}
              finding={f}
              onSelectEvent={onSelectEvent}
              onJumpToTime={onJumpToTime}
            />
          ))}
        </div>
      )}

      {/* Tag action */}
      {findings.length > 0 && (
        <TagFindingsBar mutation={tagMutation} label={`Tag ${findings.length} as anomaly`} />
      )}

      {/* Methodology note */}
      <div className="flex items-start gap-1.5 text-xs text-[var(--color-fg-muted)] pt-1">
        <AlertTriangle size={10} className="mt-0.5 shrink-0" />
        <span>
          Per source, events are ordered by time and every run of n consecutive
          values of the chosen field forms one sequence; a sequence never seen
          in the baseline window is flagged, scored by how rare it is within
          its suspect window. Sequences never mix events from different sources
          or span a window boundary. Sources that interleave several
          independent streams (multi-writer logs) can produce interleaving
          artifacts — prefer per-stream fields there.
        </span>
      </div>
    </div>
  );
}
