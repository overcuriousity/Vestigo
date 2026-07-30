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
import { AlertTriangle } from "lucide-react";
import { anomaliesApi } from "@/api/anomalies";
import { AnomalyFieldPicker } from "./AnomalyFieldPicker";
import { AnalysisEmptyState, DetectorStatusLine, FindingRowActions, FindingShell, NeedsBaselinePrompt, RefreshButton, ResultsBar, TagFindingsBar } from "./detector-shared";
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

/** Standard grouping fields for the optional per-identifier scoping (D14) —
 * same set the sequence view offers for its series field. Labels come from
 * `anomalyFieldLabel`, so these are tokens only. */
const GROUP_FIELD_TOKENS = ["artifact", "timestamp_desc", "display_name", "parser_name"];

/** A group value as shown to the analyst — events missing the grouping field
 * form a real group of their own, so it is named rather than hidden. */
function groupValueLabel(value: unknown): string {
  return typeof value === "string" && value !== "" ? value : "(no value)";
}

/** Which reference scored a grouped finding (D14). A group without enough
 * values of its own — including none at all — is scored against a fallback
 * rather than skipped, and `own` says how much evidence it did contribute. */
function groupBasisHint(basis: unknown, own: unknown): string {
  const short =
    typeof own === "number"
      ? own === 0
        ? "This group had no values of its own in the baseline window"
        : `This group had only ${own} distinct values of its own, below the 20 needed`
      : "This group had no usable reference of its own";
  if (basis === "outside-suspect-windows")
    return `${short}; scored against a reference learned outside the suspect windows.`;
  if (basis === "scope-merged")
    return `${short}; scored against the merged whole-scope alphabet, which is what this field was measured against before grouping.`;
  if (basis === "baseline-window") return "Scored against this group's baseline-window alphabet.";
  return "Scored against this group's alphabet learned across the scope.";
}

/** Basis values that mean "not this group's own alphabet". */
const FALLBACK_BASES = new Set(["outside-suspect-windows", "scope-merged"]);

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
      dismissed={finding.dismissed}
      confirmed={finding.confirmed}
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
          disposition={{
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
            className="inline-block rounded border border-[var(--color-danger)]/40 bg-[var(--color-bg-elevated)] px-1 py-0.5 font-mono text-xs text-[var(--color-danger)]"
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
        {typeof finding.details.group_field === "string" && (
          <span
            title={groupBasisHint(
              finding.details.group_basis,
              finding.details.group_baseline_distinct_values,
            )}
          >
            per {fieldLabel(finding.details.group_field)}{" "}
            <strong className="text-[var(--color-fg-secondary)]">
              {groupValueLabel(finding.details.group_value)}
            </strong>
            {/* Why a fallback scored it: no evidence of its own, or too little.
                Labelling a thin group "no baseline" would be wrong. */}
            {FALLBACK_BASES.has(String(finding.details.group_basis)) &&
              (finding.details.group_baseline_distinct_values
                ? " (thin baseline)"
                : " (no baseline)")}
          </span>
        )}
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
  // D14: optional per-identifier scoping — one learned alphabet per value of
  // this field instead of one merged alphabet across the scope.
  const [groupField, setGroupField] = useState("");
  const qc = useQueryClient();

  const fieldsParam = fieldsParamOf(selectedFields);
  const fl = useFindingsLimit();
  const sd = useShowDismissed();

  // Dynamic attribute fields extend the group-by dropdown (same source as the
  // sequence view's grouping-field picker).
  const { data: fieldsData } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "fields"],
    queryFn: () => anomaliesApi.fields(caseId, timelineId),
    staleTime: 5 * 60 * 1000,
  });

  const groupFieldOptions = useMemo(() => {
    const attrOptions = (fieldsData?.fields ?? [])
      .filter((f) => f.token.startsWith("attr:"))
      .map((f) => f.token);
    return [...GROUP_FIELD_TOKENS, ...attrOptions];
  }, [fieldsData]);

  const { data, isLoading, isFetching, refetch } = useQuery({
    queryKey: ["anomalies", caseId, timelineId, "charset", blKey, fieldsParam ?? "__auto__", groupField || "__scope__", fl.limit, sd.keyPart],
    queryFn: () =>
      anomaliesApi.list(caseId, timelineId, {
        detector: "charset",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
        ...(groupField ? { group_field: groupField } : {}),
        ...(sd.enabled ? { include_dismissed: true } : {}),
      }),
    staleTime: 60_000,
    enabled: !needsBaseline,
  });

  const tagMutation = useMutation({
    mutationFn: () =>
      anomaliesApi.tag(caseId, timelineId, {
        detector: "charset",
        limit: fl.limit,
        ...blParams,
        ...(fieldsParam !== undefined ? { fields: fieldsParam } : {}),
        ...(groupField ? { group_field: groupField } : {}),
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
        <select
          value={groupField}
          onChange={(e) => setGroupField(e.target.value)}
          data-testid="charset-group-select"
          title="Group by — learn one alphabet per value of this field (e.g. per host) instead of one merged alphabet"
          className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-1 py-0.5 text-xs text-[var(--color-fg-primary)] focus:outline-none focus:border-[var(--color-accent)]"
        >
          <option value="">Whole scope</option>
          {groupFieldOptions.map((token) => (
            <option key={token} value={token}>
              per {fieldLabel(token)}
            </option>
          ))}
        </select>
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
        <AnalysisEmptyState
          hint={
            data?.status === "no_data"
              ? "Check the frame above — the scanned windows may not cover any events."
              : data?.status === "insufficient_data"
                ? "A learned alphabet needs enough distinct baseline values, and stays useful only while the alphabet is small. Pick fields explicitly above."
                : isTemporal
                  ? "Every suspect-window value used characters the baseline had already seen."
                  : "Every value stayed inside the alphabet the corpus already uses."
          }
        >
          {data?.status === "no_data"
            ? "The scan matched no events."
            : data?.status === "insufficient_data"
              ? "No field had a learnable alphabet."
              : isTemporal
                ? "No values with characters new to the baseline."
                : "No values with rare characters."}
        </AnalysisEmptyState>
      )}

      {/* Findings list */}
      {findings.length > 0 && (
        <div className="space-y-1.5">
          <ResultsBar total={cap.total} shownCount={cap.shown.length} hasMore={cap.hasMore} expanded={cap.expanded} onToggle={cap.toggle} serverTotal={data?.total_findings} onLoadMore={fl.canRaise ? fl.raise : undefined} loadingMore={isFetching} dismissedCount={data?.dismissed_count} showDismissed={sd.enabled} onToggleDismissed={sd.toggle} />
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
