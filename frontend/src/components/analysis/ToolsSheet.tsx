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
import { Layers, ScanLine } from "lucide-react";
import { METHODS, type MethodId } from "./method-registry";
import { MethodRow } from "./MethodRow";
import { useStreamingSweep } from "@/hooks/useMethodFindings";
import { SigmaPanel } from "./SigmaPanel";
import { PatternsView } from "./PatternsView";
import { BaselineBuilderDrawer } from "./BaselineBuilderDrawer";
import { useUiStore } from "@/stores/ui";
import { cn } from "@/lib/cn";
import type { AnalysisScope } from "@/api/analysis";

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
  onRequestScopeChange: (next: { frame: "self" | "baseline"; baselineId?: string }) => void;
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
}: Props) {
  const { byMethod, scope } = useStreamingSweep(caseId, timelineId);
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
  const ran = states.filter((s) => s.status === "applicable" && !s.error).length;
  const skipped = states.filter((s) => s.status !== "applicable").length;

  const openBaselineBuilder = () => setBaselineBuilderOpen(true);

  return (
    <div ref={rootRef}>
      <Section id="methods" title="Methods">
        <p data-testid="methods-summary" className="mb-2 text-[11px] text-[var(--color-fg-muted)]">
          {METHODS.length} considered · {ran} ran · {skipped} skipped
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

      <Section id="signatures" title="Signatures">
        <SigmaPanel caseId={caseId} timelineId={timelineId} />
      </Section>

      <Section id="explore" title="Explore">
        <p className="mb-2 text-[11px] text-[var(--color-fg-muted)]">
          Motif mining answers &ldquo;what is routine here?&rdquo; — its results{" "}
          <strong className="font-medium">suppress</strong> findings rather than raising them.
        </p>
        <PatternsView caseId={caseId} timelineId={timelineId} onSelectEvent={() => {}} />
      </Section>

      <Section id="scope" title="Scope">
        <ScopeSection scope={scope} onRequest={onRequestScopeChange} onManage={openBaselineBuilder} />
      </Section>

      <BaselineBuilderDrawer caseId={caseId} timelineId={timelineId} />
    </div>
  );
}

function ScopeSection({
  scope,
  onRequest,
  onManage,
}: {
  scope: AnalysisScope;
  onRequest: Props["onRequestScopeChange"];
  onManage: () => void;
}) {
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
      label: "Compare baseline",
      hint: scope.baseline_name
        ? `Suspect windows against “${scope.baseline_name}”.`
        : "Pick or build a baseline definition to enable the comparison methods.",
    },
  ];

  return (
    <>
      <div className="space-y-1">
        {options.map((opt) => {
          const active = scope.frame === opt.id;
          return (
            <button
              key={opt.id}
              data-testid={`scope-switch-${opt.id}`}
              onClick={() => onRequest({ frame: opt.id, baselineId: scope.baseline_id ?? undefined })}
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
