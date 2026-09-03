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
import {
  useFindingsPageKey,
  useMethodFindings,
} from "@/hooks/useMethodFindings";
import { useScopeChange } from "@/hooks/useScopeChange";
import { scopeOf, useTimelineDetectors } from "@/hooks/useTimelineDetectors";
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
  onAddDetector,
  onTagFilter,
  onDrillField,
  onJumpToTime,
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
  /** Open the detector wizard, optionally on one method. */
  onAddDetector: (method?: MethodId) => void;
  onTagFilter?: (tag: string) => void;
  /** Drill into the grid's filters — a template in Tools, a value in finding mode. */
  onDrillField?: (field: string, value: string) => void;
  /** Jump the grid to a finding's time, from the finding sheet's verdict row. */
  onJumpToTime?: (ts: string, eventId?: string) => void;
  similarAnchor?: Event | null;
  onSimilarClose?: () => void;
  onSelectEvent?: (event: Event) => void;
}) {
  const scopeChange = useScopeChange(caseId, timelineId);

  // Null until the analyst runs the method. Method mode has to be able to sit
  // there showing prose without firing a request — opening a method's detail is
  // not the same act as running it — while `autorun` covers the one case where
  // it is: the Tools sheet's Run anyway / Retry.
  const [runParams, setRunParams] = useState<Record<string, unknown> | null>(
    null,
  );

  // Set when the analyst submits the knobs from *finding* mode ("Run with
  // these"). The question they just asked is a method-wide one — "what does
  // this method find at these settings" — so the sheet answers it in method
  // mode rather than re-rendering one finding that the new parameters may no
  // longer produce.
  const [ranFromFinding, setRanFromFinding] = useState(false);

  // Reset whenever the sheet changes what it is showing, so the knobs typed for
  // one view never silently become the request for the next. `kind` is part of
  // that: running a method with custom parameters and then clicking one of that
  // *same* method's rows in the rail leaves the method unchanged, and without
  // resetting here the finding view would key on the custom run while the rail
  // addressed a rank in the plain sweep — rendering a different finding than
  // the one clicked, or none at all. So is `rank`: after "Run with these" the
  // sheet sits in method mode with `ranFromFinding` set, and clicking a
  // *different* rank of the same method changes neither `kind` nor `method`.
  // Without the rank in here that click resets nothing and appears dead — the
  // sheet keeps showing the custom run and never renders the row clicked.
  const methodKey = sheet.kind === "tools" ? null : sheet.method;
  const rankKey = sheet.kind === "finding" ? sheet.rank : null;
  const autorun = sheet.kind === "method" && Boolean(sheet.autorun);
  useEffect(() => {
    setRunParams(autorun ? {} : null);
    setRanFromFinding(false);
  }, [sheet.kind, methodKey, rankKey, autorun]);

  // Finding mode addresses a row of the rail's list, which the rail fetched
  // under the *configured* entry's params and scope — so the sheet must read
  // the same query, or it renders a different list than the row clicked.
  // Method mode with the analyst's own knobs runs under the panel scope.
  const { byMethod: entries, isLoaded: detectorsLoaded } = useTimelineDetectors(
    caseId,
    timelineId,
  );
  const entry =
    sheet.kind === "finding" ? entries.get(sheet.method) : undefined;
  // The rail stays interactive beside the sheet, so the detector whose finding
  // is open can be removed while it is open. Without this the query would fall
  // back to the panel scope with empty params and fire a *fresh* scan for a
  // detector that no longer exists — an unprompted heavy run, and a different
  // finding at that rank than the row that was clicked.
  const entryGone =
    sheet.kind === "finding" && detectorsLoaded && entry === undefined;
  const findings = useMethodFindings(caseId, timelineId, methodOf(sheet), {
    enabled:
      (sheet.kind === "finding" && entry !== undefined) ||
      (sheet.kind === "method" && runParams !== null),
    params: runParams ?? entry?.params ?? {},
    // The entry's own scope, for the re-run too. "Run with these" tweaks a
    // configured detector's knobs; dropping its frame back to the panel's
    // would answer a different question — and for a baseline-framed method
    // that question has no baseline to answer it with at all.
    scope: entry ? scopeOf(entry) : undefined,
  });
  // The same identity the query above is keyed by, so "Show more" in the sheet
  // raises this run's page and not the rail's run of the same method.
  const pageKey = useFindingsPageKey(caseId, timelineId, methodOf(sheet), {
    params: runParams ?? entry?.params ?? {},
    scope: entry ? scopeOf(entry) : undefined,
  });

  // Closing is the honest answer to "what you were reading is gone" — leaving
  // the overlay up would show a stale finding under a detector that is no
  // longer configured.
  useEffect(() => {
    if (entryGone) onClose();
  }, [entryGone, onClose]);

  const finding =
    sheet.kind === "finding" ? findings.data?.results[sheet.rank] : undefined;

  return (
    <>
      {sheet.kind === "finding" &&
      !ranFromFinding &&
      finding &&
      findings.data ? (
        <InvestigateSheet
          caseId={caseId}
          timelineId={timelineId}
          railWidth={railWidth}
          onClose={onClose}
          mode="finding"
          methodId={sheet.method}
          finding={finding}
          scope={findings.data.scope}
          // The knobs open on what produced the finding above them, not on
          // the method's defaults.
          initialParams={entry?.params}
          onRun={(params) => {
            setRunParams(params);
            setRanFromFinding(true);
          }}
          running={findings.isFetching}
          onDrillField={onDrillField}
          onJumpToTime={onJumpToTime}
        />
      ) : sheet.kind === "finding" ? (
        // The rail addresses a finding as (method, rank), so the sheet can open
        // before the query lands or after a refetch shortened the list. Both
        // used to render nothing at all, which reads as a dead click. This is
        // also where "Run with these" lands, so the knobs keep showing what
        // was submitted rather than snapping back to defaults.
        <InvestigateSheet
          caseId={caseId}
          timelineId={timelineId}
          railWidth={railWidth}
          onClose={onClose}
          mode="method"
          methodId={sheet.method}
          initialParams={runParams ?? entry?.params}
          onRun={setRunParams}
          query={findings}
          pageKey={pageKey}
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
          pageKey={pageKey}
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
          onAddDetector={onAddDetector}
          onRequestScopeChange={(next) => scopeChange.request(next)}
          onTagFilter={onTagFilter}
          onDrillField={onDrillField}
          similarAnchor={similarAnchor}
          onSimilarClose={onSimilarClose}
          onSelectEvent={onSelectEvent}
        />
      ) : null}

      <ScopeChangeDialog
        open={scopeChange.pending !== null}
        current={scopeChange.currentScope}
        next={scopeChange.pending ?? { frame: "self" }}
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
