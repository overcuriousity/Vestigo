/**
 * DistributionDriftView — fields whose *whole value distribution* changed
 * between the baseline window and a suspect window.
 *
 * Calls the value_distribution_drift detector: numeric fields get a
 * Kolmogorov–Smirnov test (computed inside ClickHouse), categorical fields a
 * k-category G-test over the top baseline categories (+ __other__). One
 * BH-FDR pool across both branches; effect floors on KS D / total-variation
 * distance. Temporal-only, like ProportionShiftView: it requires the
 * "Compare windows" frame with an active baseline definition.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, ArrowDownRight, ArrowUpRight, Info, Sigma } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { AnomalyFieldPicker } from "./AnomalyFieldPicker";
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
  fieldsParamOf,
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
} from "./detector-hooks";
import { Spinner } from "@/components/ui/Spinner";
import { useBaselineStore } from "@/stores/baseline";
import type { AnomalyMarker, DistributionDriftFinding, Event } from "@/api/types";
import { anomalyFieldLabel as fieldLabel, truncate } from "@/lib/format";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";

interface Props {
  caseId: string;
  timelineId: string;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onFindingsChange?: (markers: AnomalyMarker[]) => void;
  onRunIdChange?: (runId: string | undefined) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}

interface Contributor {
  value: string;
  baseline_share: number;
  window_share: number;
  delta: number;
}

function topContributor(f: DistributionDriftFinding): Contributor | undefined {
  const list = f.details["top_contributors"] as Contributor[] | undefined;
  return list?.[0];
}

function directionIcon(direction: DistributionDriftFinding["direction"]) {
  if (direction === "up")
    return <ArrowUpRight size={12} className="shrink-0 text-[var(--color-error)]" />;
  if (direction === "down")
    return <ArrowDownRight size={12} className="shrink-0 text-[var(--color-warning)]" />;
  return <Sigma size={12} className="shrink-0 text-[var(--color-error)]" />;
}

const pct = (v: number) => `${(v * 100).toFixed(v * 100 >= 10 ? 0 : 1)}%`;

function DriftRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onDrillField,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  finding: DistributionDriftFinding;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);
  const numeric = finding.test === "ks";
  const top = topContributor(finding);

  return (
    <FindingShell
      dismissed={finding.dismissed}
      details={finding.details}
      onClick={() => {
        if (finding.event_id) openEvent.mutate();
      }}
      actions={
        <FindingRowActions
          // Drill only makes sense for the categorical branch — jump to the
          // most-shifted category's value; a numeric drift has no one value.
          field={!numeric && top ? finding.field : undefined}
          value={!numeric && top ? top.value : undefined}
          ts={finding.event?.timestamp ?? finding.first_seen}
          eventId={finding.event_id}
          onDrillField={onDrillField}
          onJumpToTime={onJumpToTime}
          disposition={{
            caseId,
            timelineId,
            detector: "value_distribution_drift",
            details: finding.details,
            sourceId: finding.event?.source_id,
          }}
        />
      }
    >
      {/* Field + test badge + window */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
          {fieldLabel(finding.field)}
        </span>
        {directionIcon(finding.direction)}
        <span className="inline-block rounded border border-[var(--color-border)] px-1 py-0.5 font-mono text-[10px] uppercase text-[var(--color-fg-muted)]">
          {numeric ? "KS" : "G"}
        </span>
        <span className="min-w-0 break-all font-mono text-xs font-medium text-[var(--color-fg-primary)]">
          {truncate(finding.value)}
        </span>
      </div>

      {/* Shape change (the explainability shot) */}
      <div className="text-xs text-[var(--color-fg-muted)]">
        {numeric ? (
          <>
            median{" "}
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {String(finding.details["baseline_median"])} →{" "}
              {String(finding.details["window_median"])}
            </span>{" "}
            (D ={" "}
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {finding.effect.toFixed(2)}
            </span>
            {" — "}
            {pct(finding.effect)} of probability mass moved)
          </>
        ) : top ? (
          <>
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {truncate(top.value)}
            </span>{" "}
            {pct(top.baseline_share)} → {pct(top.window_share)} of events (TVD{" "}
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {finding.effect.toFixed(2)}
            </span>
            )
          </>
        ) : (
          <>
            distribution shifted (TVD{" "}
            <span className="font-mono text-[var(--color-fg-secondary)]">
              {finding.effect.toFixed(2)}
            </span>
            )
          </>
        )}
      </div>

      {/* Meta line */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span>
          q{" "}
          <strong className="text-[var(--color-fg-secondary)]">
            {finding.q_value < 0.000001 ? "<1e-6" : finding.q_value.toPrecision(2)}
          </strong>
        </span>
        <span>
          n{" "}
          <strong className="text-[var(--color-fg-secondary)]">{finding.window_n}</strong> vs{" "}
          {finding.baseline_n} baseline
        </span>
        {finding.first_seen && <span>at {fmtTs(finding.first_seen)}</span>}
      </div>
    </FindingShell>
  );
}

export function DistributionDriftView({
  caseId,
  timelineId,
  onSelectEvent,
  onDrillField,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();
  // Temporal-only, same frame gating as ProportionShiftView: a distribution
  // can only drift between two windows, so there is no self-baseline fallback.
  const frame = useBaselineStore((s) => s.frame);
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  const fieldsParam = fieldsParamOf(selectedFields);
  const fl = useFindingsLimit();
  const sd = useShowDismissed();
  const enabled = frame === "baseline" && !needsBaseline;

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: [
      "anomalies",
      caseId,
      timelineId,
      "value_distribution_drift",
      blKey,
      fieldsParam ?? "__auto__",
      fl.limit,
      sd.enabled,
    ],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "value_distribution_drift",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
        ...(sd.enabled ? { include_dismissed: true } : {}),
      }),
    staleTime: 60_000,
    enabled,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "value_distribution_drift",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = useMemo(
    () =>
      (data?.results ?? []).filter(
        (r): r is DistributionDriftFinding => r.type === "value_distribution_drift",
      ),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f) => {
      const ts =
        f.event?.timestamp ?? f.first_seen ?? (f.details["window_start"] as string | undefined);
      if (!ts) return null;
      const label = `${fieldLabel(f.field)} drift (${f.value})`;
      const top = topContributor(f);
      const detail =
        f.test === "ks"
          ? `Distribution drift: ${fieldLabel(f.field)} — numeric distribution shifted ` +
            `${f.direction} in ${f.value} (median ${String(f.details["baseline_median"])} → ` +
            `${String(f.details["window_median"])}, KS D=${f.effect.toFixed(2)}, ` +
            `q=${f.q_value.toPrecision(2)})`
          : `Distribution drift: ${fieldLabel(f.field)} — category mix shifted in ${f.value}` +
            (top
              ? ` (${top.value}: ${pct(top.baseline_share)} → ${pct(top.window_share)}`
              : " (") +
            `, TVD=${f.effect.toFixed(2)}, q=${f.q_value.toPrecision(2)})`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "value_distribution_drift" as const,
        rawDetails: f.details,
        windowEnd: (f.details["window_end"] as string | undefined) ?? null,
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
          Distribution drift compares a field's whole value distribution
          between two windows, so it always needs a baseline — switch the
          frame to <strong>Compare windows</strong> and pick a baseline
          definition. It has no scan-all-events mode.
        </span>
      </div>
    );
  }
  if (needsBaseline) return <NeedsBaselinePrompt />;

  return (
    <div className="space-y-3">
      {/* Toolbar */}
      <div className="flex items-center gap-2 flex-wrap">
        <span className="flex-1" />
        <AnomalyFieldPicker
          caseId={caseId}
          timelineId={timelineId}
          selected={selectedFields}
          onChange={setSelectedFields}
        />
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
              ? "No drift findings. No events ingested yet."
              : data?.status === "insufficient_data"
                ? "Nothing to test — the baseline window has no events, or no scanned field had enough samples on both sides."
                : "No field's value distribution changed significantly between the baseline and the suspect windows."}
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
            dismissedCount={data?.dismissed_count}
            showDismissed={sd.enabled}
            onToggleDismissed={sd.toggle}
          />
          {cap.shown.map((f, i) => (
            <DriftRow
              key={`${f.field}:${f.value}:${f.test}:${i}`}
              caseId={caseId}
              timelineId={timelineId}
              finding={f}
              onSelectEvent={onSelectEvent}
              onDrillField={onDrillField}
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
          Each finding is one field × suspect window. Numeric fields use a
          two-sample Kolmogorov–Smirnov test (D = largest CDF gap); categorical
          fields a G-test over the top-50 baseline categories plus __other__
          (TVD = share of probability mass that moved). Both branches share one
          Benjamini–Hochberg correction, and each has an effect floor so
          significance alone never flags at large volumes.
        </span>
      </div>
    </div>
  );
}
