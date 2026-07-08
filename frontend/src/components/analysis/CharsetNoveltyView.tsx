/**
 * CharsetNoveltyView — values containing characters outside a field's
 * learned character set.
 *
 * Calls the charset detector. Self-baseline mode ("rare-chars") flags values
 * containing characters that appear in almost no other value of the field;
 * temporal mode flags characters never seen in the baseline window. The novel
 * characters themselves are the explainability money-shot, rendered as chips
 * with their unicode codepoints.
 */
import { useMemo, useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, Info } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { AnomalyFieldPicker } from "./AnomalyFieldPicker";
import {
  DetectorStatusLine,
  FindingRowActions,
  FindingShell,
  NeedsBaselinePrompt,
  ResultsBar,
  useCappedFindings,
  useBaselineRequest,
  RefreshButton,
  TagFindingsBar,
  fieldsParamOf,
  useAnomalyMarkers,
  useDetectorRunId,
  useOpenEvent,
} from "./detector-shared";
import { Spinner } from "@/components/ui/Spinner";
import type { AnomalyMarker, CharsetFinding, Event } from "@/api/types";
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

/** "U+0000"-style codepoint label for a (possibly multi-codepoint) char. */
function codepointLabel(c: string): string {
  return Array.from(c)
    .map((ch) => `U+${(ch.codePointAt(0) ?? 0).toString(16).toUpperCase().padStart(4, "0")}`)
    .join(" ");
}

/** Chip text for a novel character — codepoint escape when unprintable. */
function charLabel(c: string): string {
  const cp = c.codePointAt(0) ?? 0;
  // Controls, whitespace, and the C1/NBSP block render invisibly — show the
  // codepoint instead so a NUL byte is actually visible in the finding.
  const printable = cp > 0x20 && !(cp >= 0x7f && cp <= 0xa0);
  return printable ? c : codepointLabel(c);
}

function CharsetRow({
  caseId,
  timelineId,
  finding,
  onSelectEvent,
  onDrillField,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  finding: CharsetFinding;
  onSelectEvent: (event: Event) => void;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  const openEvent = useOpenEvent(caseId, timelineId, finding.event_id, onSelectEvent);

  return (
    <FindingShell
      details={finding.details}
      onClick={() => {
        if (finding.event_id) openEvent.mutate();
      }}
      actions={
        <FindingRowActions
          field={finding.field}
          value={finding.value}
          ts={finding.event?.timestamp ?? finding.first_seen}
          eventId={finding.event_id}
          onDrillField={onDrillField}
          onJumpToTime={onJumpToTime}
          markNormal={{
            caseId,
            timelineId,
            detector: "charset",
            details: finding.details,
            sourceId: finding.event?.source_id,
          }}
        />
      }
    >
      {/* Field + value */}
      <div className="flex flex-wrap items-center gap-1">
        <span className="inline-block rounded bg-[var(--color-bg-elevated)] px-1.5 py-0.5 font-mono text-xs text-[var(--color-fg-muted)]">
          {fieldLabel(finding.field)}
        </span>
        <span className="min-w-0 break-all font-mono text-xs font-medium text-[var(--color-fg-primary)]">
          {truncate(finding.value)}
        </span>
      </div>

      {/* Novel characters (the explainability shot) */}
      <div className="flex flex-wrap items-center gap-1 text-xs text-[var(--color-fg-muted)]">
        <span>novel char{finding.novel_chars.length === 1 ? "" : "s"}</span>
        {finding.novel_chars.map((c, i) => (
          <span
            key={`${c}:${i}`}
            title={codepointLabel(c)}
            className="inline-block rounded border border-[var(--color-error)]/40 bg-[var(--color-bg-elevated)] px-1 py-0.5 font-mono text-xs text-[var(--color-error)]"
          >
            {charLabel(c)}
          </span>
        ))}
      </div>

      {/* Meta line */}
      <div className="flex flex-wrap items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <span>
          count <strong className="text-[var(--color-fg-secondary)]">{finding.count}</strong>
        </span>
        <span>
          surprise{" "}
          <strong className="text-[var(--color-fg-secondary)]">{finding.score.toFixed(1)}</strong>
        </span>
        {finding.first_seen && <span>first {fmtTs(finding.first_seen)}</span>}
      </div>
    </FindingShell>
  );
}

export function CharsetNoveltyView({
  caseId,
  timelineId,
  onSelectEvent,
  onDrillField,
  onFindingsChange,
  onRunIdChange,
  onJumpToTime,
}: Props) {
  const { params: blParams, key: blKey, needsBaseline } = useBaselineRequest();
  const [selectedFields, setSelectedFields] = useState<string[] | null>(null);
  const qc = useQueryClient();

  const fieldsParam = fieldsParamOf(selectedFields);

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "charset", blKey, fieldsParam ?? "__auto__"],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "charset",
        limit: 50,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    staleTime: 60_000,
    enabled: !needsBaseline,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "charset",
        limit: 50,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["annotations"] });
    },
  });

  const findings = useMemo(
    () => (data?.results ?? []).filter((r): r is CharsetFinding => r.type === "charset"),
    [data],
  );

  useAnomalyMarkers(
    findings,
    (f) => {
      const ts = f.event?.timestamp ?? f.first_seen;
      if (!ts) return null;
      const label = `${fieldLabel(f.field)}=${truncate(f.value)}`;
      const chars = f.novel_chars.map((c) => `${charLabel(c)} (${codepointLabel(c)})`).join(", ");
      const originDesc =
        data?.method === "temporal-charset"
          ? "never seen in the baseline window"
          : "rare across this field's values";
      const detail =
        `Charset novelty: ${label} — contains character${
          f.novel_chars.length === 1 ? "" : "s"
        } ${chars} ${originDesc} (${f.count} occurrence${f.count === 1 ? "" : "s"})`;
      return {
        ts,
        label,
        detail,
        eventId: f.event_id,
        sourceId: f.event?.source_id,
        detector: "charset" as const,
        rawDetails: f.details,
      };
    },
    onFindingsChange,
  );

  useDetectorRunId(data?.run_id, onRunIdChange);

  const cap = useCappedFindings(findings);

  if (needsBaseline) return <NeedsBaselinePrompt />;

  const isTemporal = data?.method === "temporal-charset";

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
          autoIncludesIdentifiers
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
              ? "No charset novelties. No events ingested yet."
              : data?.status === "insufficient_data"
                ? "No fields with enough distinct baseline values (or the alphabet is too large). Pick fields explicitly above."
                : isTemporal
                  ? "No values with characters absent from the baseline window."
                  : "No values with rare characters."}
          </span>
        </div>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar total={cap.total} shownCount={cap.shown.length} hasMore={cap.hasMore} expanded={cap.expanded} onToggle={cap.toggle} />
          {cap.shown.map((f, i) => (
            <CharsetRow
              key={`${f.field}:${f.value}:${i}`}
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
          {isTemporal
            ? "Comparing windows: learns every character seen in the baseline window's values and flags suspect-window values containing never-seen characters."
            : "Scanning all events: flags values containing characters that appear in almost no other distinct value of the field (rare-character set)."}{" "}
          Purely syntactic — null bytes, homoglyphs, and injection metacharacters are detected by character identity, never by meaning.
        </span>
      </div>
    </div>
  );
}
