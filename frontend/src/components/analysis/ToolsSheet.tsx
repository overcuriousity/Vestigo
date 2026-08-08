/**
 * ToolsSheet — the machinery accounting, in one surface.
 *
 * Four sections in one scroll: Methods, Signatures, Explore, Scope. This is the
 * answer to "what did you examine, what did you not, under what scope, and with
 * what result" — a question with evidentiary weight in a post-mortem, which is
 * why it gets one place that can be read without giving up the findings list
 * beside it, and why the negative answers (a method skipped, a rule that
 * matched nothing) are shown as prominently as the positive ones.
 *
 * The alternatives were considered and rejected: an icon strip is tabs rotated
 * ninety degrees, and it trades the triage queue for the accounting; pushing
 * these to other pages scatters the record across three surfaces and drops two
 * tools out of it entirely.
 */
import { useEffect, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import { Layers, ScanLine } from "lucide-react";
import { METHODS, type MethodId } from "./method-registry";
import { MethodRow } from "./MethodRow";
import { useStreamingSweep } from "@/hooks/useMethodFindings";
import { SigmaPanel } from "./SigmaPanel";
import { PatternsView } from "./PatternsView";
import { TemplatesView } from "./TemplatesView";
import { BaselineBuilderDrawer } from "./BaselineBuilderDrawer";
import { SimilarEvents } from "./SimilarEvents";
import { GuidancePanel } from "@/components/ui/GuidancePanel";
import { baselinesApi } from "@/api/baselines";
import { useCapabilities } from "@/api/health";
import { useTimelineReadiness } from "@/hooks/useTimelineReadiness";
import { useBaselineStore } from "@/stores/baseline";
import { useUiStore } from "@/stores/ui";
import { cn } from "@/lib/cn";
import type { AnalysisScope } from "@/api/analysis";
import type { Event } from "@/api/types";

export type ToolsSection = "methods" | "signatures" | "explore" | "scope";

interface Props {
  caseId: string;
  timelineId: string;
  section?: ToolsSection;
  onRunMethod: (method: MethodId) => void;
  onOpenMethod: (method: MethodId) => void;
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

function Section({
  id,
  title,
  children,
}: {
  id: ToolsSection;
  title: string;
  children: React.ReactNode;
}) {
  return (
    <section id={`tools-${id}`} className="mt-5 first:mt-0">
      <h4 className="mb-2 text-[10px] font-semibold uppercase tracking-wide text-[var(--color-fg-muted)]">
        {title}
      </h4>
      {children}
    </section>
  );
}

export function ToolsSheet({
  caseId,
  timelineId,
  section,
  onRunMethod,
  onOpenMethod,
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
  const setBaselineBuilderOpen = useUiStore((s) => s.setBaselineBuilderOpen);
  const rootRef = useRef<HTMLDivElement>(null);

  // Opening from a specific rail affordance lands on that section rather than
  // making the analyst hunt the scroll for it.
  useEffect(() => {
    if (!section) return;
    const target = rootRef.current?.querySelector(`#tools-${section}`);
    // Feature-detected rather than assumed: scrollIntoView is absent in jsdom
    // and, more to the point, this is a nicety — failing to scroll must never
    // take the whole accounting down with it.
    if (target && typeof target.scrollIntoView === "function") {
      target.scrollIntoView({ block: "start", behavior: "smooth" });
    }
  }, [section]);

  const states = METHODS.map((m) => byMethod[m.id]).filter(Boolean);
  // `pending`, not just the plan status: on first open the heavy set is still
  // queued behind `cheapSettled`, and counting it as run claims an accounting
  // that has not happened yet. A method still in flight is neither ran nor
  // skipped, so it is named separately rather than folded into either.
  const running = states.filter((s) => s.status === "applicable" && s.pending).length;
  const ran = states.filter((s) => s.status === "applicable" && !s.error && !s.pending).length;
  const skipped = states.filter((s) => s.status !== "applicable").length;

  const openBaselineBuilder = () => setBaselineBuilderOpen(true);

  return (
    <div ref={rootRef}>
      <Section id="methods" title="Methods">
        <p data-testid="methods-summary" className="mb-2 text-[11px] text-[var(--color-fg-muted)]">
          {METHODS.length} considered · {ran} ran · {skipped} skipped
          {running > 0 && ` · ${running} still running`}
        </p>
        <div className="space-y-1">
          {METHODS.map((meta) =>
            byMethod[meta.id] ? (
              <MethodRow
                key={meta.id}
                state={byMethod[meta.id]}
                onRun={onRunMethod}
                onOpen={onOpenMethod}
                onSetupBaseline={openBaselineBuilder}
              />
            ) : null,
          )}
        </div>
      </Section>

      {/* Absent, not disabled, when Sigma is unconfigured — the house rule for
          every optional subsystem. And absent on an empty timeline: a run that
          matches nothing there reads as "these rules cleared you", when in
          fact there was nothing to match against. */}
      {sigma && !nothingToAnalyse && (
        <Section id="signatures" title="Signatures">
          <GuidancePanel id="investigate-sigma" />
          <SigmaPanel caseId={caseId} timelineId={timelineId} onTagFilter={onTagFilter} />
        </Section>
      )}

      <Section id="explore" title="Explore">
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
        <GuidancePanel id="investigate-patterns" />
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
      </Section>

      <Section id="scope" title="Scope">
        <ScopeSection
          caseId={caseId}
          timelineId={timelineId}
          scope={scope}
          onRequest={onRequestScopeChange}
          onManage={openBaselineBuilder}
        />
      </Section>

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
