/**
 * ScopeStrip — one line stating the comparison every finding below was
 * computed under.
 *
 * Replaces FrameBar. The scope *switch* itself moved into the Tools sheet,
 * because changing it invalidates every method's cache at once and reframes
 * every verdict already recorded — that deserves a confirm, not a toggle
 * sitting one stray click above the results.
 */
import { Layers, ScanLine } from "lucide-react";
import type { AnalysisScope } from "@/api/analysis";

export function ScopeStrip({ scope, onOpen }: { scope: AnalysisScope; onOpen: () => void }) {
  const inBaseline = scope.frame === "baseline" && Boolean(scope.baseline_name);
  const Icon = inBaseline ? Layers : ScanLine;
  return (
    <button
      data-testid="scope-strip"
      onClick={onOpen}
      title="Open Tools to review or change the scope every method runs under"
      className="mb-2 flex w-full items-center gap-1.5 rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-left text-[11px] text-[var(--color-fg-secondary)] transition-base hover:border-[var(--color-border-strong)] hover:text-[var(--color-fg-primary)]"
    >
      <Icon size={11} className="shrink-0" />
      <span className="min-w-0 flex-1 truncate">
        {inBaseline ? (
          <>
            Compared against{" "}
            <strong className="font-medium text-[var(--color-fg-primary)]">
              {scope.baseline_name}
            </strong>
          </>
        ) : (
          "All events scanned · no baseline comparison"
        )}
      </span>
    </button>
  );
}
