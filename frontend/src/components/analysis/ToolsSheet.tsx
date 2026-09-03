/**
 * ToolsSheet — the machinery accounting, in one surface.
 *
 * Four sections — Scope, Methods, Signatures, Explore — behind a tab strip.
 * This is the answer to "what did you examine, what did you not, under what
 * scope, and with what result": a question with evidentiary weight in a
 * post-mortem, which is why it gets one place that can be read without giving
 * up the findings list beside it, and why the negative answers (a method
 * skipped, a rule that matched nothing) are shown as prominently as the
 * positive ones.
 *
 * The alternatives were considered and rejected: an icon strip *in the rail* is
 * tabs rotated ninety degrees, and it trades the triage queue for the
 * accounting; pushing these to other pages scatters the record across three
 * surfaces and drops two tools out of it entirely.
 *
 * These sections were one long scroll until the template list grew to a
 * thousand rows and buried Scope — the section that reframes every other one —
 * below all of it. Tabs are what keep each section's length its own problem;
 * they cost nothing the single scroll had, since the sheet only ever showed one
 * section's worth of viewport anyway. Scope leads the strip for the same
 * reason it stopped being last.
 */
import { useEffect, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Layers, Plus, ScanLine } from "lucide-react";
import { METHODS, METHODS_BY_ID, type MethodId } from "./method-registry";
import { MethodRow } from "./MethodRow";
import { useStreamingSweep } from "@/hooks/useMethodFindings";
import { SigmaPanel } from "./SigmaPanel";
import { PatternsView } from "./PatternsView";
import { TemplatesView } from "./TemplatesView";
import { BaselineBuilderDrawer } from "./BaselineBuilderDrawer";
import { NormalValuesList } from "./WindowsNormality";
import { SimilarEvents } from "./SimilarEvents";
import { baselinesApi } from "@/api/baselines";
import { useCapabilities } from "@/api/health";
import { useFieldOverrides } from "@/hooks/useFieldOverrides";
import { useTimelineReadiness } from "@/hooks/useTimelineReadiness";
import { useBaselineStore } from "@/stores/baseline";
import { useUiStore } from "@/stores/ui";
import { Button } from "@/components/ui/Button";
import { cn } from "@/lib/cn";
import { anomalyFieldLabel } from "@/lib/format";
import type { AnalysisScope } from "@/api/analysis";
import type { Event } from "@/api/types";

export type ToolsSection = "methods" | "signatures" | "explore" | "scope";

interface Props {
  caseId: string;
  timelineId: string;
  section?: ToolsSection;
  onRunMethod: (method: MethodId) => void;
  onOpenMethod: (method: MethodId) => void;
  /** Open the detector wizard, optionally straight on one method (edit). */
  onAddDetector: (method?: MethodId) => void;
  /** Never applies a scope change directly — it invalidates every cached
   * method at once and reframes every verdict already recorded, so the host
   * routes it through a confirm. */
  onRequestScopeChange: (next: {
    frame: "self" | "baseline";
    baselineId?: string;
    baselineName?: string | null;
  }) => void;
  /** Sigma hits filter the grid by their "sigma: <title>" tag. */
  onTagFilter?: (tag: string) => void;
  /** Drill a template (or any field/value) into the grid's filters. */
  onDrillField?: (field: string, value: string) => void;
  /** The event the analyst anchored from the grid, if any. */
  similarAnchor?: Event | null;
  onSimilarClose?: () => void;
  onSelectEvent?: (event: Event) => void;
}

/**
 * Tab order, Scope first. Signatures drops out when Sigma is unconfigured —
 * the house rule for every optional subsystem is absent, never disabled.
 */
const TABS: { id: ToolsSection; label: string }[] = [
  { id: "scope", label: "Scope" },
  { id: "methods", label: "Methods" },
  { id: "signatures", label: "Signatures" },
  { id: "explore", label: "Explore" },
];

/** Where Tools opens when nothing asked for a particular section. */
const DEFAULT_TAB: ToolsSection = "methods";

export function ToolsSheet({
  caseId,
  timelineId,
  section,
  onRunMethod,
  onOpenMethod,
  onAddDetector,
  onRequestScopeChange,
  onTagFilter,
  onDrillField,
  similarAnchor,
  onSimilarClose,
  onSelectEvent,
}: Props) {
  const { byMethod, scope } = useStreamingSweep(caseId, timelineId);
  const { embeddings, sigma } = useCapabilities();
  const { nothingToAnalyse } = useTimelineReadiness(caseId, timelineId);
  // The timeline's field declarations, summarized here because the control that
  // sets them lives in one method's field picker: a decision the whole case
  // inherits needs somewhere it can be read back and undone in one place.
  const {
    overrides,
    clearMethod,
    canEdit: canDeclare,
    saveError: declareError,
  } = useFieldOverrides(caseId, timelineId);
  const declared = Object.entries(overrides).filter(
    ([id, fields]) => id in METHODS_BY_ID && Object.keys(fields).length > 0,
  ) as [MethodId, Record<string, boolean>][];
  const setBaselineBuilderOpen = useUiStore((s) => s.setBaselineBuilderOpen);

  // Absent, not disabled, when Sigma is unconfigured — and absent on an empty
  // timeline, where a run that matches nothing reads as "these rules cleared
  // you" when in fact there was nothing to match against.
  const sigmaAvailable = sigma && !nothingToAnalyse;
  const tabs = TABS.filter((t) => t.id !== "signatures" || sigmaAvailable);

  // Opening from a specific rail affordance selects that tab. Held as state
  // rather than read straight from the prop so the analyst can then move
  // between tabs without the request that opened the sheet snapping them back.
  const [tab, setTab] = useState<ToolsSection>(section ?? DEFAULT_TAB);
  useEffect(() => {
    setTab(section ?? DEFAULT_TAB);
  }, [section]);
  // A tab can vanish under the selection — Sigma being unconfigured, or the
  // timeline emptying out — which would otherwise render an empty sheet.
  const activeTab = tabs.some((t) => t.id === tab) ? tab : DEFAULT_TAB;

  // Only configured detectors are counted: an unconfigured method was not
  // asked, and listing it here with a count would be the "checked, clear"
  // misread this surface exists to prevent. `pending`, not just the plan
  // status: on first open the heavy set is still queued behind `cheapSettled`.
  const configured = METHODS.map((m) => byMethod[m.id]).filter((s) => s?.configured);
  const running = configured.filter((s) => s.pending).length;
  const failed = configured.filter((s) => s.error).length;
  const ran = configured.filter((s) => !s.error && !s.pending).length;

  const openBaselineBuilder = () => setBaselineBuilderOpen(true);

  return (
    <div>
      <div
        role="tablist"
        aria-label="Tools sections"
        className="mb-3 flex gap-1 border-b border-[var(--color-border)]"
      >
        {tabs.map((t) => (
          <button
            key={t.id}
            role="tab"
            data-testid={`tools-tab-${t.id}`}
            aria-selected={activeTab === t.id}
            onClick={() => setTab(t.id)}
            className={cn(
              "-mb-px border-b-2 px-2 py-1 text-[11px] font-medium transition-base",
              activeTab === t.id
                ? "border-[var(--color-accent)] text-[var(--color-accent)]"
                : "border-transparent text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]",
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {activeTab === "methods" && (
        <section id="tools-methods">
          <p
            data-testid="methods-summary"
            className="mb-2 text-[11px] text-[var(--color-fg-muted)]"
          >
            {configured.length} configured · {ran} ran
            {running > 0 && ` · ${running} still running`}
            {failed > 0 && (
              <span className="text-[var(--color-warning)]"> · {failed} failed</span>
            )}
          </p>
          <Button
            variant="outline"
            size="sm"
            data-testid="tools-add-detector"
            className="mb-2"
            onClick={() => onAddDetector()}
          >
            <Plus size={11} /> Add detector
          </Button>
          <p className="mb-2 text-[11px] text-[var(--color-fg-muted)]">
            Only configured detectors run. A method the analysis gate marks not applicable can
            still be configured — the gate is advice.
          </p>
          {declared.length > 0 && (
            <div
              data-testid="field-overrides-summary"
              className="mb-2 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] p-2 text-xs"
            >
              <p className="font-semibold text-[var(--color-fg-secondary)]">Declared fields</p>
              <p className="mt-0.5 text-[var(--color-fg-muted)]">
                Which fields these methods read when they pick for themselves. Naming a field
                explicitly still scans it, and a run that held one back says so.
              </p>
              <div className="mt-1.5 space-y-1">
                {declared.map(([id, fields]) => (
                  <div key={id} className="flex items-start justify-between gap-2">
                    <span className="text-[var(--color-fg-secondary)]">
                      {METHODS_BY_ID[id].label}:{" "}
                      {Object.entries(fields)
                        .map(([token, on]) => `${on ? "+" : "−"}${anomalyFieldLabel(token)}`)
                        .join(", ")}
                    </span>
                    {canDeclare && (
                      <Button
                        variant="ghost"
                        size="sm"
                        data-testid={`clear-overrides-${id}`}
                        onClick={() => clearMethod(id)}
                        className="h-auto shrink-0 px-1 py-0 text-[var(--color-fg-muted)]"
                      >
                        Reset
                      </Button>
                    )}
                  </div>
                ))}
              </div>
              {/* Reset removes a declaration the whole case inherits; a failed
                  PATCH only makes the row reappear, which reads as a click
                  that missed rather than as a write that did not land. */}
              {declareError && (
                <p
                  data-testid="field-overrides-error"
                  className="mt-1.5 text-[var(--color-danger)]"
                >
                  Not saved: {declareError}
                </p>
              )}
            </div>
          )}
          <div className="space-y-1">
            {configured.map((state) => (
              <MethodRow
                key={state.meta.id}
                state={state}
                onRun={onRunMethod}
                onOpen={onOpenMethod}
                onSetupBaseline={openBaselineBuilder}
              />
            ))}
          </div>
        </section>
      )}

      {activeTab === "signatures" && sigmaAvailable && (
        <section id="tools-signatures">
          <SigmaPanel caseId={caseId} timelineId={timelineId} onTagFilter={onTagFilter} />
        </section>
      )}

      {activeTab === "explore" && (
        <section id="tools-explore">
          {/* Similarity is anchor-driven: it cannot join an unprompted sweep, so
              it lives here and appears once the analyst anchors an event from a
              grid row. Without this the grid's "find similar" action would set
              an anchor nothing ever reads — a dead button. Gated on embeddings:
              with no vector store configured there is nothing to be similar in. */}
          {embeddings &&
            (similarAnchor && onSelectEvent ? (
              <div className="mb-3">
                <SimilarEvents
                  caseId={caseId}
                  timelineId={timelineId}
                  anchorEvent={similarAnchor}
                  onClose={onSimilarClose ?? (() => {})}
                  onSelectEvent={onSelectEvent}
                />
              </div>
            ) : (
              <p className="mb-3 text-[11px] text-[var(--color-fg-muted)]">
                Expand an event in the grid, then use the search icon in its detail panel to find events like it.
              </p>
            ))}
          <p className="mb-2 text-[11px] text-[var(--color-fg-muted)]">
            Motif mining answers &ldquo;what is routine here?&rdquo; — its results{" "}
            <strong className="font-medium">suppress</strong> findings rather than raising them.
          </p>
          <PatternsView caseId={caseId} timelineId={timelineId} onSelectEvent={() => {}} />

          {/* Log templates. The rail lists template *rows*, but muting one — a
              `kind="routine"`, `detector="log_template"` disposition — is only
              offered here, and this is the only surface that can list and reverse
              an existing mute. `events.py` still collapses matching events out of
              the grid, histogram and export, so dropping it would leave a mute
              hiding evidence with no way left to inspect or undo it. */}
          <p className="mt-3 mb-2 text-[11px] text-[var(--color-fg-muted)]">
            Log-line shapes, with the variable parts masked. Muting one{" "}
            <strong className="font-medium">collapses</strong> its events out of the grid — always
            with a visible count, and reversible here.
          </p>
          <TemplatesView caseId={caseId} timelineId={timelineId} onDrillField={onDrillField} />
        </section>
      )}

      {activeTab === "scope" && (
        <section id="tools-scope">
          <ScopeSection
            caseId={caseId}
            timelineId={timelineId}
            scope={scope}
            onRequest={onRequestScopeChange}
            onManage={openBaselineBuilder}
          />
          {/* Verdicts belong beside the scope for the same reason the baseline
              does: both decide what a sweep will show. A `normal` verdict is
              not a note about a finding, it is a standing instruction to
              suppress matching findings in every later scan — so the analyst
              needs somewhere to read the instructions they have given and take
              one back. 1.12.0 shipped without it: the old panel was the only
              mount of this list, and deleting the panel left a four-second
              toast as the entire undo path for a detection-affecting decision. */}
          <div
            data-testid="dispositions-section"
            className="mt-4 border-t border-[var(--color-border)] pt-3"
          >
            <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-[var(--color-fg-secondary)]">
              Recorded verdicts
            </h3>
            <NormalValuesList caseId={caseId} timelineId={timelineId} />
          </div>
        </section>
      )}

      {/* Outside the tab switch: the drawer is opened from both Methods
          ("Set a baseline") and Scope, and unmounting it with its opener would
          close it the moment either tab changed under it. */}
      <BaselineBuilderDrawer caseId={caseId} timelineId={timelineId} />
    </div>
  );
}

function ScopeSection({
  caseId,
  timelineId,
  scope,
  onRequest,
  onManage,
}: {
  caseId: string;
  timelineId: string;
  scope: AnalysisScope;
  onRequest: Props["onRequestScopeChange"];
  onManage: () => void;
}) {
  // The *selected* definition, from the store — not `scope.baseline_id`, which
  // the plan response only carries in the baseline frame. Reading it from the
  // scope made `needsDefinition` permanently true in the self frame, so
  // "Compare baseline" could never request the switch it names and always fell
  // through to opening the builder.
  const selectedBaselineId = useBaselineStore((s) => s.activeBaselineId);
  const baselineId = scope.baseline_id ?? selectedBaselineId;
  // Only to name a selection the scope cannot: in the self frame the plan
  // response carries no baseline at all, so without this the button offering
  // the switch could not say what it would switch to.
  const { data: baselines } = useQuery({
    queryKey: ["baselines", caseId, timelineId],
    queryFn: () => baselinesApi.list(caseId, timelineId),
  });
  const baselineName =
    scope.baseline_name ?? baselines?.baselines.find((b) => b.id === baselineId)?.name ?? null;

  const options = [
    {
      id: "self" as const,
      icon: ScanLine,
      label: "Scan all events",
      hint: "Self-baseline over the whole corpus. Two-window methods have nothing to compare and stay unavailable.",
    },
    {
      id: "baseline" as const,
      icon: Layers,
      label: baselineName ? "Compare baseline" : "Pick a baseline…",
      hint: baselineName
        ? `Suspect windows against “${baselineName}”.`
        : "Pick or build a baseline definition to enable the comparison methods.",
    },
  ];

  return (
    <>
      <div className="space-y-1">
        {options.map((opt) => {
          const active = scope.frame === opt.id;
          // Switching to the baseline frame without a definition cannot take
          // effect — the store falls back to `self` — so this affordance opens
          // the builder instead of promising a re-run that will not happen.
          const needsDefinition = opt.id === "baseline" && !baselineId;
          return (
            <button
              key={opt.id}
              data-testid={`scope-switch-${opt.id}`}
              onClick={() =>
                needsDefinition
                  ? onManage()
                  : onRequest({
                      frame: opt.id,
                      baselineId: baselineId ?? undefined,
                      baselineName,
                    })
              }
              className={cn(
                "flex w-full items-start gap-2 rounded border px-2 py-1.5 text-left text-xs transition-base",
                active
                  ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)]"
                  : "border-[var(--color-border)] hover:border-[var(--color-border-strong)]",
              )}
            >
              <opt.icon
                size={12}
                className={cn(
                  "mt-0.5 shrink-0",
                  active ? "text-[var(--color-accent)]" : "text-[var(--color-fg-muted)]",
                )}
              />
              <span className="min-w-0">
                <b
                  className={cn(
                    "block font-medium",
                    active ? "text-[var(--color-accent)]" : "text-[var(--color-fg-primary)]",
                  )}
                >
                  {opt.label}
                </b>
                <em className="not-italic text-[11px] text-[var(--color-fg-muted)]">{opt.hint}</em>
              </span>
            </button>
          );
        })}
      </div>
      <button
        onClick={onManage}
        className="mt-2 rounded border border-[var(--color-border)] px-2 py-0.5 text-[11px] text-[var(--color-fg-secondary)] hover:border-[var(--color-border-strong)]"
      >
        Manage baselines
      </button>
      <p className="mt-2 text-[11px] text-[var(--color-fg-muted)]">
        Changing scope re-runs every method against a different comparison. Verdicts already
        recorded are kept, tagged with the scope they were reached under.
      </p>
    </>
  );
}
