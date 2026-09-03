/**
 * ScopeChangeDialog — the gate in front of a scope change.
 *
 * Changing frame or baseline is not a view toggle. It moves the cache
 * fingerprint for every method at once, so all of them re-run, and it reframes
 * every verdict already recorded — those verdicts were reached against a
 * comparison that is about to stop being the active one.
 *
 * So the dialog states the consequence in numbers before anything moves, and
 * says plainly that verdicts survive. They are kept and stay tagged with the
 * scope they were reached under; nothing is discarded or rewritten, because
 * a verdict is an analyst's assertion and the tool does not get to revise it.
 */
import * as RadixDialog from "@radix-ui/react-dialog";
import { DialogContent } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import type { AnalysisScope } from "@/api/analysis";

function describe(scope: { frame: "self" | "baseline"; name?: string | null }): string {
  return scope.frame === "baseline" && scope.name
    ? `comparing against “${scope.name}”`
    : "all events, no baseline comparison";
}

export function ScopeChangeDialog({
  open,
  current,
  next,
  affectedVerdicts,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  current: AnalysisScope;
  next: { frame: "self" | "baseline"; baselineId?: string; baselineName?: string | null };
  affectedVerdicts: number;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  return (
    <RadixDialog.Root open={open} onOpenChange={(o) => !o && onCancel()}>
      <DialogContent title="Change the panel scope">
        <p className="text-sm text-[var(--color-fg-secondary)]">
          Switching from <strong>{describe({ frame: current.frame, name: current.baseline_name })}</strong>{" "}
          to <strong>{describe({ frame: next.frame, name: next.baselineName })}</strong> changes what
          ad hoc runs from a method's sheet and the Explore tab compare against. Configured
          detectors keep the scope they were configured with.
        </p>

        {affectedVerdicts > 0 && (
          <p className="mt-3 text-sm text-[var(--color-fg-secondary)]">
            <strong>{affectedVerdicts}</strong> verdict{affectedVerdicts === 1 ? " was" : "s were"}{" "}
            reached under the current scope. They are <strong>kept</strong>, and stay tagged with
            the scope they were reached under — findings computed under a different scope are marked
            so you can re-examine them.
          </p>
        )}

        <div className="mt-5 flex justify-end gap-2">
          <Button variant="ghost" onClick={onCancel}>
            Cancel
          </Button>
          <Button onClick={onConfirm}>Change scope</Button>
        </div>
      </DialogContent>
    </RadixDialog.Root>
  );
}
