/**
 * MethodFocusStrip — discloses that some methods are scanning only the fields
 * this analyst chose (#341).
 *
 * A focus narrows what is actually *scanned*, so a focused method reports
 * nothing about the fields it no longer reads. Nothing in this product may
 * hold something back without saying so, which is the whole reason this strip
 * exists rather than the focus simply applying invisibly.
 *
 * Deliberately distinct from `DetectorMuteStrip` beside it: muting is shared,
 * audited state on the timeline, and so are the declared fields in the Tools
 * sheet. A focus is only ever this analyst's, and the copy has to keep those
 * two apart — an analyst who thinks they have changed the case's field set
 * when they have changed their own view has been misled.
 */
import { Crosshair, X } from "lucide-react";
import { METHODS_BY_ID, type MethodId } from "./method-registry";
import { Button } from "@/components/ui/Button";
import type { MethodFocus } from "@/hooks/useMethodFocus";

export function MethodFocusStrip({
  focus,
  onClear,
}: {
  focus: MethodFocus;
  onClear: (method: MethodId) => void;
}) {
  const entries = Object.entries(focus).filter(([, fields]) => fields && fields.length > 0);
  if (entries.length === 0) return null;

  return (
    <div
      data-testid="method-focus-strip"
      className="rounded border border-[var(--color-border)] bg-[var(--color-bg-elevated)] px-2 py-1 text-xs text-[var(--color-fg-secondary)]"
    >
      <div className="flex items-center gap-1.5">
        <Crosshair size={11} className="text-[var(--color-accent)]" />
        <span className="flex-1">
          <span className="font-semibold text-[var(--color-accent)]">
            {entries.length} {entries.length === 1 ? "method is" : "methods are"} focused
          </span>{" "}
          — scanning only the fields you chose, so nothing is reported about the rest. Only you
          see this.
        </span>
      </div>
      <ul className="mt-1 space-y-0.5">
        {entries.map(([method, fields]) => (
          <li key={method} className="flex items-center gap-1.5">
            <span className="text-[var(--color-fg-muted)]">
              {METHODS_BY_ID[method as MethodId]?.label ?? method}: {fields.join(", ")}
            </span>
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className="h-4 w-4"
              onClick={() => onClear(method as MethodId)}
              aria-label={`Clear focus on ${METHODS_BY_ID[method as MethodId]?.label ?? method}`}
            >
              <X size={10} />
            </Button>
          </li>
        ))}
      </ul>
    </div>
  );
}
