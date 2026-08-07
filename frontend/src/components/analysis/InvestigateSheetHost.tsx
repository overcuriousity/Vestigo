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
import { InvestigateSheet } from "./InvestigateSheet";
import { ScopeChangeDialog } from "./ScopeChangeDialog";
import { useMethodFindings } from "@/hooks/useMethodFindings";
import { useScopeChange } from "@/hooks/useScopeChange";
import type { MethodId } from "./method-registry";
import type { Event } from "@/api/types";

export type SheetRequest =
  | { kind: "finding"; method: MethodId; rank: number }
  | { kind: "method"; method: MethodId }
  | { kind: "tools"; section?: "methods" | "signatures" | "explore" | "scope" };

export function InvestigateSheetHost({
  caseId,
  timelineId,
  railWidth,
  sheet,
  onClose,
  onOpenMethod,
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
  onTagFilter?: (tag: string) => void;
  similarAnchor?: Event | null;
  onSimilarClose?: () => void;
  onSelectEvent?: (event: Event) => void;
}) {
  const scopeChange = useScopeChange(caseId, timelineId);

  // Only enabled for finding mode; the other modes need no findings fetch, and
  // an always-on query here would re-run a method just to open Tools.
  const findings = useMethodFindings(caseId, timelineId, methodOf(sheet), {
    enabled: sheet.kind === "finding",
  });

  const finding =
    sheet.kind === "finding" ? findings.data?.results[sheet.rank] : undefined;

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
      ) : sheet.kind === "method" ? (
        <InvestigateSheet
          caseId={caseId}
          timelineId={timelineId}
          railWidth={railWidth}
          onClose={onClose}
          mode="method"
          methodId={sheet.method}
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
          onRunMethod={onOpenMethod}
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
