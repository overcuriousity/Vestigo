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
 *   finding — the verdict in plain language, the evidence, the score with its
 *             unit, the scope it was computed under, how the method works, its
 *             knobs, and the query it ran.
 *   method  — the same explanation and controls without a specific finding.
 *   tools   — the machinery accounting (ToolsSheet).
 *
 * The methodology prose lives in the method registry and renders here rather
 * than in a separate Method tab: "what does this actually do" is only ever
 * asked while looking at one of a method's findings.
 */
import { useEffect } from "react";
import { X } from "lucide-react";
import { METHODS_BY_ID, type MethodId } from "./method-registry";
import { EVIDENCE_CLASSES } from "./method-registry";
import { ToolsSheet } from "./ToolsSheet";
import { Button } from "@/components/ui/Button";
import { fmtTimestampCompactUtc as fmtTs } from "@/lib/time";
import type { AnalysisScope, MethodResult } from "@/api/analysis";
import type { Event } from "@/api/types";
import { isTemplateRow } from "@/api/analysis";

export type SheetMode =
  | { mode: "finding"; methodId: MethodId; finding: MethodResult; scope: AnalysisScope }
  | { mode: "method"; methodId: MethodId }
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
  /** Tools mode: hand a scope change to the host's confirm gate. */
  onRequestScopeChange?: (next: { frame: "self" | "baseline"; baselineId?: string }) => void;
  /** Tools mode: Sigma hits filter the grid by tag. */
  onTagFilter?: (tag: string) => void;
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

function MethodBody({ methodId }: { methodId: MethodId }) {
  const meta = METHODS_BY_ID[methodId];
  return (
    <>
      <Subhead>How this method works</Subhead>
      <p className="text-xs leading-relaxed text-[var(--color-fg-secondary)]">{meta.what}</p>

      <Subhead>Parameters</Subhead>
      <div className="flex flex-wrap items-center gap-2">
        {meta.knobs.map((knob) => (
          <label
            key={knob.param}
            data-testid="method-knob"
            className="flex items-center gap-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-[11px] text-[var(--color-fg-secondary)]"
          >
            {knob.label}
            <input
              defaultValue=""
              placeholder={knob.placeholder}
              className="w-16 bg-transparent font-mono text-[11px] text-[var(--color-fg-primary)] outline-none placeholder:text-[var(--color-fg-disabled)]"
            />
          </label>
        ))}
      </div>
    </>
  );
}

/**
 * Best available timestamp for a row. Not every finding shape carries
 * `first_seen` (frequency findings carry a window instead), so this reads
 * defensively rather than asserting a field the union does not guarantee.
 */
function when(finding: MethodResult): string {
  if (isTemplateRow(finding)) return finding.first_seen ?? "—";
  const ts =
    finding.event?.timestamp ??
    ("first_seen" in finding ? finding.first_seen : null) ??
    ("window_start" in finding ? finding.window_start : null);
  return ts ? fmtTs(ts) : "—";
}


function FindingBody({
  methodId,
  finding,
  scope,
}: {
  methodId: MethodId;
  finding: MethodResult;
  scope: AnalysisScope;
}) {
  const meta = METHODS_BY_ID[methodId];
  const evidence = EVIDENCE_CLASSES.find((c) => c.id === meta.evidenceClass);
  const template = isTemplateRow(finding);

  return (
    <>
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

      <MethodBody methodId={methodId} />
    </>
  );
}

export function InvestigateSheet({
  caseId,
  timelineId,
  railWidth,
  onClose,
  onRunMethod,
  onOpenMethod,
  onRequestScopeChange,
  onTagFilter,
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
        className="absolute inset-y-0 z-30 flex flex-col border-l border-[var(--color-border-strong)] bg-[var(--color-bg-overlay)] shadow-lg"
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
              methodId={rest.methodId}
              finding={rest.finding}
              scope={rest.scope}
            />
          )}
          {rest.mode === "method" && <MethodBody methodId={rest.methodId} />}
          {rest.mode === "tools" && (
            <ToolsSheet
              caseId={caseId}
              timelineId={timelineId}
              section={rest.section}
              onRunMethod={onRunMethod ?? (() => {})}
              onOpenMethod={onOpenMethod ?? (() => {})}
              onRequestScopeChange={onRequestScopeChange ?? (() => {})}
              onTagFilter={onTagFilter}
              similarAnchor={similarAnchor}
              onSimilarClose={onSimilarClose}
              onSelectEvent={onSelectEvent}
            />
          )}
        </div>
      </div>
    </>
  );
}
