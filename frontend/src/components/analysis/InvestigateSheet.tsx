/**
 * InvestigateSheet — the one detail surface, in three modes.
 *
 * Absolutely positioned inside the explorer stage, never a flex sibling. That
 * is the whole point: the rail is the only fixed-width participant in the row,
 * so no combination of open panels can push anything off screen. A sheet that
 * joined the flex row would reintroduce exactly the bug this redesign exists
 * to remove.
 *
 * Modes:
 *   finding — the verdict in plain language, the evidence where the payload
 *             carries any, the score with its unit, the scope it was computed
 *             under, how the method works, its knobs, the shape of the query it
 *             runs, and the verdict controls pinned below all of it.
 *   method  — the same explanation and controls without a specific finding.
 *   tools   — the machinery accounting (ToolsSheet).
 *
 * "Query shape" is a sketch, not a transcript: unlike the Sigma runner, the
 * detectors do not return their compiled SQL, so presenting one as the executed
 * statement would be a claim nothing in the codebase backs.
 *
 * The methodology prose lives in the method registry and renders here rather
 * than in a separate Method tab: "what does this actually do" is only ever
 * asked while looking at one of a method's findings.
 */
import { useCallback, useEffect, useState } from "react";
import { Play, X } from "lucide-react";
import type { UseQueryResult } from "@tanstack/react-query";
import { METHODS_BY_ID, type MethodId } from "./method-registry";
import { EVIDENCE_CLASSES } from "./method-registry";
import { ToolsSheet } from "./ToolsSheet";
import { MethodKnobForm } from "./MethodKnobForm";
import { ScoredRow, TemplateRows } from "./FindingGroup";
import { FindingRowActions, FindingRowState } from "./detector-shared";
import { DETECTORS } from "./detector-registry";
import { FindingEvidence } from "./FindingEvidence";
import { normalizeFinding } from "@/lib/finding-normalize";
import { evidenceCaption, hasEvidence } from "@/lib/finding-evidence";
import { findingSubject } from "@/lib/finding-subject";
import { findingVerdict } from "@/lib/finding-verdict";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";
import type { AnalysisScope, MethodFindings, MethodResult } from "@/api/analysis";
import type { AnomalyFinding, Event } from "@/api/types";
import { isTemplateRow } from "@/api/analysis";

/** See FindingGroup: method ids are API keys, detector-registry uses UI slugs. */
const DETECTOR_BY_API_KEY = Object.fromEntries(DETECTORS.map((d) => [d.detector, d]));

export type SheetMode =
  | {
      mode: "finding";
      methodId: MethodId;
      finding: MethodResult;
      scope: AnalysisScope;
      /**
       * The params the finding on screen was produced under — the configured
       * entry's own. The knob form opens on these, so "Run with these" means
       * what it says: a form showing defaults under a finding computed from
       * stored settings offers to re-run a question nobody asked.
       */
      initialParams?: Record<string, unknown>;
      /** Re-runs the method with the knobs as typed, in method mode. */
      onRun?: (params: Record<string, unknown>) => void;
      running?: boolean;
    }
  | {
      mode: "method";
      methodId: MethodId;
      /** Opens the knobs on these instead of on the method's defaults. */
      initialParams?: Record<string, unknown>;
      /** Runs the method with the knob values as typed. */
      onRun: (params: Record<string, unknown>) => void;
      query: UseQueryResult<MethodFindings>;
    }
  | { mode: "tools"; section?: "methods" | "signatures" | "explore" | "scope" };

interface Props {
  caseId: string;
  timelineId: string;
  /** Width of the rail this sheet sits beside, so it never overlaps it. */
  railWidth: number;
  onClose: () => void;
  /** Tools mode: run a method the gate skipped, or one that errored. */
  onRunMethod?: (method: MethodId) => void;
  /** Tools mode: open a method's own detail. */
  onOpenMethod?: (method: MethodId) => void;
  /** Tools mode: open the detector wizard, optionally on one method. */
  onAddDetector?: (method?: MethodId) => void;
  /** Tools mode: hand a scope change to the host's confirm gate. */
  onRequestScopeChange?: (next: {
    frame: "self" | "baseline";
    baselineId?: string;
    baselineName?: string | null;
  }) => void;
  /** Tools mode: Sigma hits filter the grid by tag. */
  onTagFilter?: (tag: string) => void;
  /** Drill into the grid's filters — a template in Tools, a value in finding mode. */
  onDrillField?: (field: string, value: string) => void;
  /** Finding mode: jump the grid to the finding's time. */
  onJumpToTime?: (ts: string, eventId?: string) => void;
  /** Tools mode: the event anchored from a grid row, for similarity. */
  similarAnchor?: Event | null;
  onSimilarClose?: () => void;
  onSelectEvent?: (event: Event) => void;
}

function Subhead({ children }: { children: React.ReactNode }) {
  return (
    <h4 className="mt-4 mb-1.5 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-fg-muted)] first:mt-0">
      {children}
    </h4>
  );
}

function MethodBody({
  caseId,
  timelineId,
  methodId,
  initialParams,
  onRun,
  runLabel = "Run",
  running,
}: {
  /** The field knobs offer this timeline's own columns, so both ids are needed. */
  caseId: string;
  timelineId: string;
  methodId: MethodId;
  /** Opens the knobs on these instead of on the method's defaults. */
  initialParams?: Record<string, unknown>;
  /**
   * Runs the method with the knobs as typed. Present in both modes: a form of
   * inputs with no way to submit them is a control that lies about being one,
   * and finding mode had exactly that.
   */
  onRun?: (params: Record<string, unknown>) => void;
  runLabel?: string;
  running?: boolean;
}) {
  const meta = METHODS_BY_ID[methodId];
  const [params, setParams] = useState<Record<string, unknown>>(initialParams ?? {});
  const [blocker, setBlocker] = useState<string | null>(null);
  const onFormChange = useCallback((p: Record<string, unknown>, b: string | null) => {
    setParams(p);
    setBlocker(b);
  }, []);

  return (
    <>
      <Subhead>How this method works</Subhead>
      <p className="text-xs leading-relaxed text-[var(--color-fg-secondary)]">{meta.what}</p>

      <Subhead>Parameters</Subhead>
      {/* An ad hoc run: nothing here is stored. Configuring a detector so the
          rail runs it every time is the wizard's act, not this form's. */}
      <form
        className="flex flex-wrap items-center gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          // Guarded here too, not only on the button: implicit submission from a
          // knob input is a second way into the same run.
          if (blocker) return;
          onRun?.(params);
        }}
      >
        <MethodKnobForm
          caseId={caseId}
          timelineId={timelineId}
          methodId={methodId}
          initialParams={initialParams}
          onChange={onFormChange}
        />
        {onRun && (
          <Button type="submit" variant="outline" size="sm" disabled={running || blocker !== null}>
            <Play size={11} />
            {running ? "Running…" : runLabel}
          </Button>
        )}
      </form>
      {onRun && blocker && (
        <p data-testid="method-knob-blocker" className="mt-1.5 text-xs text-[var(--color-warning)]">
          {blocker}
        </p>
      )}

      <Subhead>Query shape</Subhead>
      <pre className="overflow-x-auto rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-2 font-mono text-xs text-[var(--color-fg-secondary)]">
        {meta.querySketch}
      </pre>
      <p className="mt-1.5 text-xs text-[var(--color-fg-muted)]">
        The structure of the statement this method runs. Fields, windows and thresholds are bound
        from the parameters above — this is not a transcript of the executed query.
      </p>
    </>
  );
}

/**
 * A method's own results, in method mode.
 *
 * This is what makes the Tools sheet's "Run anyway" real. The gate is advice,
 * and a method it skipped has to be one click from running — but a click that
 * only opens prose is not that click, and it was the escape hatch the whole
 * fail-open design rests on.
 */
function MethodResults({
  caseId,
  timelineId,
  methodId,
  query,
}: {
  caseId: string;
  timelineId: string;
  methodId: MethodId;
  query: UseQueryResult<MethodFindings>;
}) {
  const detectorMeta = DETECTOR_BY_API_KEY[methodId];
  const results = query.data?.results ?? [];
  const scored = results.filter((f): f is AnomalyFinding => !isTemplateRow(f));
  const templates = results.filter(isTemplateRow);

  if (query.isFetching) {
    return (
      <p className="mt-3 flex items-center gap-2 text-xs text-[var(--color-fg-muted)]">
        <Spinner size={12} /> Running…
      </p>
    );
  }
  if (query.isError) {
    return (
      <p className="mt-3 text-xs text-[var(--color-warning)]">
        This method failed to run. Adjust a parameter and try again.
      </p>
    );
  }
  if (!query.data) return null;

  return (
    <>
      <Subhead>Results</Subhead>
      {results.length === 0 ? (
        <p className="text-xs text-[var(--color-fg-muted)]">
          Ran, found nothing under these parameters.
        </p>
      ) : (
        <div className="space-y-1">
          {templates.length > 0 && <TemplateRows rows={templates} onSelect={() => {}} />}
          {detectorMeta &&
            scored.map((f, rank) => (
              <ScoredRow
                key={f.event_id ?? rank}
                item={normalizeFinding(detectorMeta, f, rank)}
                caseId={caseId}
                timelineId={timelineId}
                onSelect={() => {}}
              />
            ))}
        </div>
      )}
    </>
  );
}

/**
 * Best available timestamp for a row. Not every finding shape carries
 * `first_seen` (frequency findings carry a window instead), so this reads
 * defensively rather than asserting a field the union does not guarantee.
 */
function rawTs(finding: MethodResult): string | null {
  if (isTemplateRow(finding)) return finding.first_seen ?? null;
  return (
    finding.event?.timestamp ??
    ("first_seen" in finding ? finding.first_seen : null) ??
    ("window_start" in finding ? finding.window_start : null) ??
    ("timestamp" in finding ? finding.timestamp : null)
  );
}

function when(finding: MethodResult): string {
  const ts = rawTs(finding);
  return ts ? fmtTs(ts) : "—";
}


function FindingBody({
  caseId,
  timelineId,
  methodId,
  finding,
  scope,
  initialParams,
  onRun,
  running,
}: {
  caseId: string;
  timelineId: string;
  methodId: MethodId;
  finding: MethodResult;
  scope: AnalysisScope;
  initialParams?: Record<string, unknown>;
  onRun?: (params: Record<string, unknown>) => void;
  running?: boolean;
}) {
  const meta = METHODS_BY_ID[methodId];
  const evidence = EVIDENCE_CLASSES.find((c) => c.id === meta.evidenceClass);
  const template = isTemplateRow(finding);
  const verdict = findingVerdict(finding);
  const caption = evidenceCaption(finding);

  return (
    <>
      {/* What the finding is about, before anything said about it. The rail row
          was the only place the subject appeared: the sheet's own claim reads
          "this value of captured_length…" and never named the value, so
          recognizing it — the analyst's first move — meant closing the sheet
          and re-reading the row underneath. Verbatim and selectable, because
          the value gets pasted into a filter or a report. */}
      <dl data-testid="finding-subject" className="mb-3">
        {findingSubject(finding).map((pair) => (
          <div key={`${pair.label}:${pair.value}`} className="mb-2 last:mb-0">
            <dt className="font-mono text-xs uppercase tracking-wide text-[var(--color-fg-muted)]">
              {pair.label}
            </dt>
            {/* Capped, not truncated: a template or an n-gram can run to
                paragraphs, and pushing the claim off screen to show all of it
                trades one missing subject for another. */}
            <dd className="m-0 max-h-24 select-text overflow-y-auto break-all font-mono text-lg font-bold leading-tight text-[var(--color-fg-primary)]">
              {pair.value}
            </dd>
          </div>
        ))}
      </dl>

      {/* The claim, before the arithmetic. The rail row states the finding in
          the detector's vocabulary, which is right for a list and wrong for the
          surface an analyst opens to decide whether it is real. */}
      <p
        data-testid="finding-verdict"
        className="mb-3 text-sm leading-relaxed text-[var(--color-fg-primary)]"
      >
        {verdict.lead}{" "}
        <span className="rounded-sm bg-[var(--color-anomaly-dim)] px-1 font-semibold text-[var(--color-anomaly,var(--color-warning))]">
          {verdict.highlight}
        </span>{" "}
        {verdict.tail}
      </p>

      {hasEvidence(finding) && (
        <div data-testid="finding-evidence" className="mb-3">
          <Subhead>Evidence{caption ? ` — ${caption}` : ""}</Subhead>
          <FindingEvidence finding={finding} />
        </div>
      )}

      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-[11px]">
        <dt className="text-[var(--color-fg-muted)]">Score</dt>
        <dd data-testid="finding-score" className="m-0 font-mono text-[var(--color-fg-primary)]">
          {template ? `×${finding.count}` : finding.score.toFixed(2)} {meta.scoreUnit}
        </dd>

        <dt className="text-[var(--color-fg-muted)]">When</dt>
        <dd className="m-0 font-mono text-[var(--color-fg-secondary)]">{when(finding)}</dd>

        <dt className="text-[var(--color-fg-muted)]">Claim</dt>
        <dd data-testid="finding-class" className="m-0 text-[var(--color-fg-secondary)]">
          {evidence?.label} — {evidence?.note}
        </dd>

        <dt className="text-[var(--color-fg-muted)]">Scope</dt>
        <dd data-testid="finding-scope" className="m-0 text-[var(--color-fg-secondary)]">
          {scope.frame === "baseline" && scope.baseline_name
            ? `Compared against ${scope.baseline_name}`
            : "All events scanned — no baseline comparison"}
        </dd>
      </dl>

      {/* Seeded with the entry's stored params: the knobs an analyst reads
          under a finding are the ones that produced it, and "Run with these"
          re-asks that question with whatever they change. */}
      <MethodBody
        caseId={caseId}
        timelineId={timelineId}
        methodId={methodId}
        initialParams={initialParams}
        onRun={onRun}
        runLabel="Run with these"
        running={running}
      />
    </>
  );
}

/**
 * The verdict row, pinned below the scroll area.
 *
 * The same `FindingRowActions` the rail rows use, wrapped in the same row-state
 * provider — a second implementation here would fork the disposition semantics,
 * and the sheet is exactly where an analyst decides. Sticky rather than inline
 * because the body scrolls: a verdict control that scrolls out of reach is the
 * one control that must not.
 */
function FindingActions({
  caseId,
  timelineId,
  methodId,
  finding,
  onDrillField,
  onJumpToTime,
}: {
  caseId: string;
  timelineId: string;
  methodId: MethodId;
  finding: MethodResult;
  onDrillField?: (field: string, value: string) => void;
  onJumpToTime?: (ts: string, eventId?: string) => void;
}) {
  // Templates carry no event and no value key, so nothing can be dispositioned
  // against one — the browsing row it is.
  if (isTemplateRow(finding)) return null;
  const verdict = findingVerdict(finding);
  return (
    <div className="flex shrink-0 items-center gap-2 border-t border-[var(--color-border)] bg-[var(--color-bg-surface)] px-3 py-2">
      <span className="text-xs text-[var(--color-fg-muted)]">Verdict</span>
      <div
        data-testid="finding-actions"
        className="flex items-center gap-1.5 text-[var(--color-fg-secondary)]"
      >
        <FindingRowState
          confirmed={finding.confirmed}
          confirmedOtherScope={finding.confirmed_other_scope}
        >
          <FindingRowActions
            ts={rawTs(finding)}
            eventId={"event_id" in finding ? finding.event_id : null}
            field={finding.type === "value_novelty" ? finding.field : undefined}
            value={finding.type === "value_novelty" ? String(finding.value) : undefined}
            onDrillField={onDrillField}
            onJumpToTime={onJumpToTime}
            disposition={{
              caseId,
              timelineId,
              detector: methodId,
              details: finding.details,
              sourceId: finding.event?.source_id ?? null,
              // What a confirmed finding is stored as. The sentence on screen
              // is the one the analyst just read and accepted, so it is also
              // the honest thing to persist.
              content: `${verdict.lead} ${verdict.highlight} ${verdict.tail}`,
            }}
          />
        </FindingRowState>
      </div>
    </div>
  );
}

export function InvestigateSheet({
  caseId,
  timelineId,
  railWidth,
  onClose,
  onRunMethod,
  onOpenMethod,
  onAddDetector,
  onRequestScopeChange,
  onTagFilter,
  onDrillField,
  onJumpToTime,
  similarAnchor,
  onSimilarClose,
  onSelectEvent,
  ...rest
}: Props & SheetMode) {
  // Escape returns to the grid. The sheet covers what the analyst is trying to
  // look at, so leaving it must not require finding a target.
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const title =
    rest.mode === "tools"
      ? "Tools"
      : rest.mode === "method"
        ? METHODS_BY_ID[rest.methodId].label
        : METHODS_BY_ID[rest.methodId].label;

  return (
    <>
      <div
        data-testid="investigate-scrim"
        onClick={onClose}
        className="absolute inset-0 z-20 bg-black/30"
      />
      <div
        data-testid="investigate-sheet"
        // Sized to its content, capped at the viewport — not `inset-y-0`. A
        // finding body is a few hundred pixels; stretching the panel to full
        // height stranded the verdict bar an entire screen below the claim it
        // answers, which is the one control that must stay next to what it
        // acts on. Long bodies (Tools, a method's results) still fill the
        // height and scroll inside, so nothing is lost at the other extreme.
        className="absolute top-0 z-30 flex max-h-full flex-col border-l border-b border-[var(--color-border-strong)] bg-[var(--color-bg-overlay)] shadow-lg"
        style={{
          right: railWidth,
          width: `min(640px, calc(100% - ${railWidth + 24}px))`,
        }}
      >
        <div className="flex shrink-0 items-center gap-2 border-b border-[var(--color-border)] px-3 py-2.5">
          <h3 className="flex-1 text-sm font-semibold text-[var(--color-fg-primary)]">{title}</h3>
          <Button variant="ghost" size="icon" onClick={onClose} title="Close (Esc)">
            <X size={14} />
          </Button>
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-3">
          {rest.mode === "finding" && (
            <FindingBody
              caseId={caseId}
              timelineId={timelineId}
              methodId={rest.methodId}
              finding={rest.finding}
              scope={rest.scope}
              initialParams={rest.initialParams}
              onRun={rest.onRun}
              running={rest.running}
            />
          )}
          {rest.mode === "method" && (
            <>
              <MethodBody
                caseId={caseId}
                timelineId={timelineId}
                methodId={rest.methodId}
                initialParams={rest.initialParams}
                onRun={rest.onRun}
                running={rest.query.isFetching}
              />
              <MethodResults
                caseId={caseId}
                timelineId={timelineId}
                methodId={rest.methodId}
                query={rest.query}
              />
            </>
          )}
          {rest.mode === "tools" && (
            <ToolsSheet
              caseId={caseId}
              timelineId={timelineId}
              section={rest.section}
              onRunMethod={onRunMethod ?? (() => {})}
              onOpenMethod={onOpenMethod ?? (() => {})}
              onAddDetector={onAddDetector ?? (() => {})}
              onRequestScopeChange={onRequestScopeChange ?? (() => {})}
              onTagFilter={onTagFilter}
              onDrillField={onDrillField}
              similarAnchor={similarAnchor}
              onSimilarClose={onSimilarClose}
              onSelectEvent={onSelectEvent}
            />
          )}
        </div>

        {rest.mode === "finding" && (
          <FindingActions
            caseId={caseId}
            timelineId={timelineId}
            methodId={rest.methodId}
            finding={rest.finding}
            onDrillField={onDrillField}
            onJumpToTime={onJumpToTime}
          />
        )}
      </div>
    </>
  );
}
