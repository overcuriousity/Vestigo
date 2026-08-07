/**
 * InvestigateSheetHost — resolves what the overlay should show, and owns the
 * scope-change confirm.
 *
 * Kept out of ExplorerPage because it needs hooks (the method's findings, the
 * pending scope change) that only matter while the overlay is open, and
 * ExplorerPage is already the largest file in the app.
 *
 * A finding is addressed as (method, rank) rather than by value: the rail and
 * the sheet then read the same cached query, so the sheet cannot show a
 * finding that the rail has since re-fetched away.
 */
import { useEffect, useState } from "react";
import { InvestigateSheet } from "./InvestigateSheet";
import { ScopeChangeDialog } from "./ScopeChangeDialog";
import { useMethodFindings } from "@/hooks/useMethodFindings";
import { useScopeChange } from "@/hooks/useScopeChange";
import type { MethodId } from "./method-registry";
import type { Event } from "@/api/types";

export type SheetRequest =
  | { kind: "finding"; method: MethodId; rank: number }
  /** `autorun` is the Tools sheet's Run/Retry: open already running. */
  | { kind: "method"; method: MethodId; autorun?: boolean }
  | { kind: "tools"; section?: "methods" | "signatures" | "explore" | "scope" };

export function InvestigateSheetHost({
  caseId,
  timelineId,
  railWidth,
  sheet,
  onClose,
  onOpenMethod,
  onRunMethod,
  onTagFilter,
  similarAnchor,
  onSimilarClose,
  onSelectEvent,
}: {
  caseId: string;
  timelineId: string;
  railWidth: number;
  sheet: SheetRequest;
  onClose: () => void;
  onOpenMethod: (method: MethodId) => void;
  /** Open the method's sheet already running it. */
  onRunMethod: (method: MethodId) => void;
  onTagFilter?: (tag: string) => void;
  similarAnchor?: Event | null;
  onSimilarClose?: () => void;
  onSelectEvent?: (event: Event) => void;
}) {
  const scopeChange = useScopeChange(caseId, timelineId);

  // Null until the analyst runs the method. Method mode has to be able to sit
  // there showing prose without firing a request — opening a method's detail is
  // not the same act as running it — while `autorun` covers the one case where
  // it is: the Tools sheet's Run anyway / Retry.
  const [runParams, setRunParams] = useState<Record<string, unknown> | null>(null);

  // Reset when the sheet changes method, so the knobs typed for one method
  // never silently become the request for the next.
  const methodKey = sheet.kind === "tools" ? null : sheet.method;
  const autorun = sheet.kind === "method" && Boolean(sheet.autorun);
  useEffect(() => {
    setRunParams(autorun ? {} : null);
  }, [methodKey, autorun]);

  const findings = useMethodFindings(caseId, timelineId, methodOf(sheet), {
    enabled: sheet.kind === "finding" || (sheet.kind === "method" && runParams !== null),
    params: runParams ?? {},
  });

  const finding = sheet.kind === "finding" ? findings.data?.results[sheet.rank] : undefined;

  return (
    <>
      {sheet.kind === "finding" && finding && findings.data ? (
        <InvestigateSheet
          caseId={caseId}
          timelineId={timelineId}
          railWidth={railWidth}
          onClose={onClose}
          mode="finding"
          methodId={sheet.method}
          finding={finding}
          scope={findings.data.scope}
        />
      ) : sheet.kind === "finding" ? (
        // The rail addresses a finding as (method, rank), so the sheet can open
        // before the query lands or after a refetch shortened the list. Both
        // used to render nothing at all, which reads as a dead click.
        <InvestigateSheet
          caseId={caseId}
          timelineId={timelineId}
          railWidth={railWidth}
          onClose={onClose}
          mode="method"
          methodId={sheet.method}
          onRun={setRunParams}
          query={findings}
        />
      ) : sheet.kind === "method" ? (
        <InvestigateSheet
          caseId={caseId}
          timelineId={timelineId}
          railWidth={railWidth}
          onClose={onClose}
          mode="method"
          methodId={sheet.method}
          onRun={setRunParams}
          query={findings}
        />
      ) : sheet.kind === "tools" ? (
        <InvestigateSheet
          caseId={caseId}
          timelineId={timelineId}
          railWidth={railWidth}
          onClose={onClose}
          mode="tools"
          section={sheet.section}
          onOpenMethod={onOpenMethod}
          // Not an alias of onOpenMethod: "Run anyway" on a gated method and
          // "Retry" on a failed one both have to *run* it. Routing them to the
          // prose panel is what made the gate a lock in practice, whatever the
          // endpoint allowed.
          onRunMethod={onRunMethod}
          onRequestScopeChange={(next) => scopeChange.request(next)}
          onTagFilter={onTagFilter}
          similarAnchor={similarAnchor}
          onSimilarClose={onSimilarClose}
          onSelectEvent={onSelectEvent}
        />
      ) : null}

      <ScopeChangeDialog
        open={scopeChange.pending !== null}
        current={scopeChange.currentScope}
        next={scopeChange.pending ?? { frame: "self" }}
        methodsToRerun={scopeChange.methodsToRerun}
        affectedVerdicts={scopeChange.affectedVerdicts}
        onConfirm={scopeChange.confirm}
        onCancel={scopeChange.cancel}
      />
    </>
  );
}

/** Any valid method id — the hook needs one even when the mode has none. */
function methodOf(sheet: SheetRequest): MethodId {
  return sheet.kind === "tools" ? "value_novelty" : sheet.method;
}
