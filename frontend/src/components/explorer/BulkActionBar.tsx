import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Tag, MessageSquare, ShieldCheck, X, AlertCircle } from "lucide-react";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { Dialog, DialogContent } from "@/components/ui/Dialog";
import { TagInput } from "@/components/explorer/TagInput";
import { annotationsApi } from "@/api/annotations";
import type { Event, EventFilters } from "@/api/types";

interface Props {
  selectedEvents: Event[];
  /** Total count shown in the label (may exceed selectedEvents.length when mode="all"). */
  selectionCount: number;
  /** "ids" = explicit per-row selection; "all" = all events matching the filter. */
  selectionMode: "ids" | "all";
  caseId: string;
  timelineId: string;
  /** Current filter — used for server-side bulk apply when selectionMode="all". */
  filters: EventFilters;
  onClear: () => void;
  tagSuggestions?: string[];
}

export function BulkActionBar({
  selectedEvents,
  selectionCount,
  selectionMode,
  caseId,
  timelineId,
  filters,
  onClear,
  tagSuggestions = [],
}: Props) {
  const qc = useQueryClient();
  const [mode, setMode] = useState<"tag" | "comment" | null>(null);
  const [value, setValue] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isPending, setIsPending] = useState(false);
  // Confirm-dialog state for "all" mode writes
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [pendingAction, setPendingAction] = useState<{
    annotation_type: "tag" | "comment" | "normal";
    content: string;
  } | null>(null);

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["annotations", caseId] });
    qc.invalidateQueries({ queryKey: ["anomalies-novelty", caseId] });
    qc.invalidateQueries({ queryKey: ["anomalies-frequency", caseId] });
    qc.invalidateQueries({ queryKey: ["tags", caseId] });
  };

  /** Execute the actual write — called either directly (ids mode) or after confirm (all mode). */
  async function execute(annotation_type: "tag" | "comment" | "normal", content: string) {
    setError(null);
    setIsPending(true);
    try {
      if (selectionMode === "all") {
        await annotationsApi.bulkByFilter(caseId, timelineId, {
          annotation_type,
          content,
          filters,
        });
      } else {
        await Promise.all(
          selectedEvents.map((event) =>
            annotationsApi.create(
              caseId,
              event.source_id,
              event.event_id,
              annotation_type,
              content,
            ),
          ),
        );
      }
      invalidate();
      onClear();
      setMode(null);
      setValue("");
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setIsPending(false);
    }
  }

  /** Guard: for "all" mode show confirm dialog; for "ids" execute immediately. */
  function requestAction(annotation_type: "tag" | "comment" | "normal", content: string) {
    if (selectionMode === "all") {
      setPendingAction({ annotation_type, content });
      setConfirmOpen(true);
    } else {
      void execute(annotation_type, content);
    }
  }

  async function applyToAll() {
    if (!mode || !value.trim()) return;
    const annotation_type = mode === "tag" ? "tag" : "comment";
    requestAction(annotation_type, value.trim());
  }

  function markAllNormal() {
    requestAction("normal", "normal operation");
  }

  if (selectionCount === 0) return null;

  return (
    <>
      {/* Confirm dialog for "all events matching filter" writes */}
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent
          title="Apply to all matching events?"
          description={
            pendingAction
              ? `${pendingAction.annotation_type === "normal" ? "Mark" : `Apply ${pendingAction.annotation_type}`} "${pendingAction.content}" on all ${selectionCount.toLocaleString()} events matching the current filter. This cannot be undone easily.`
              : undefined
          }
        >
          <div className="flex justify-end gap-2 mt-4">
            <Button variant="ghost" size="sm" onClick={() => setConfirmOpen(false)}>
              Cancel
            </Button>
            <Button
              variant="accent"
              size="sm"
              disabled={isPending}
              onClick={async () => {
                setConfirmOpen(false);
                if (pendingAction) {
                  await execute(pendingAction.annotation_type, pendingAction.content);
                }
              }}
            >
              {isPending ? <Spinner size={13} /> : `Apply to ${selectionCount.toLocaleString()}`}
            </Button>
          </div>
        </DialogContent>
      </Dialog>

      <div className="flex flex-col border-t border-[var(--color-border)] bg-[var(--color-bg-elevated)]">
        {error && (
          <div className="flex items-center gap-1.5 px-4 py-1.5 text-xs text-[var(--color-danger)] bg-[var(--color-danger-dim)]">
            <AlertCircle size={12} />
            {error}
          </div>
        )}
        <div className="flex items-center gap-3 px-4 py-2.5">
          <span className="text-xs font-medium text-[var(--color-fg-secondary)]">
            {selectionCount.toLocaleString()} selected
            {selectionMode === "all" && (
              <span className="ml-1 text-[var(--color-accent)]">(all matching filter)</span>
            )}
          </span>

          {mode === null ? (
            <>
              <Button variant="outline" size="sm" onClick={() => setMode("tag")}>
                <Tag size={13} /> Tag
              </Button>
              <Button variant="outline" size="sm" onClick={() => setMode("comment")}>
                <MessageSquare size={13} /> Comment
              </Button>
              <Button
                variant="outline"
                size="sm"
                disabled={isPending}
                onClick={markAllNormal}
              >
                {isPending ? <Spinner size={13} /> : <ShieldCheck size={13} />}
                Mark Normal
              </Button>
              <Button
                variant="ghost"
                size="icon"
                className="ml-auto"
                onClick={onClear}
              >
                <X size={14} />
              </Button>
            </>
          ) : (
            <>
              <span className="text-xs text-[var(--color-fg-muted)] capitalize">{mode}:</span>
              {mode === "tag" ? (
                <TagInput
                  autoFocus
                  dropUp
                  value={value}
                  onChange={setValue}
                  onSubmit={(v) => { setValue(v); requestAction("tag", v); }}
                  onCancel={() => setMode(null)}
                  suggestions={tagSuggestions}
                  isPending={isPending}
                  className="flex-1 max-w-xs"
                />
              ) : (
                <Input
                  autoFocus
                  placeholder="your comment…"
                  value={value}
                  onChange={(e) => setValue(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && value.trim()) applyToAll();
                    if (e.key === "Escape") setMode(null);
                  }}
                  className="flex-1 max-w-xs"
                />
              )}
              {mode === "comment" && (
                <Button
                  variant="accent"
                  size="sm"
                  disabled={!value.trim() || isPending}
                  onClick={applyToAll}
                >
                  {isPending ? <Spinner size={13} /> : "Apply"}
                </Button>
              )}
              <Button variant="ghost" size="sm" onClick={() => setMode(null)}>
                Cancel
              </Button>
              <Button variant="ghost" size="icon" className="ml-auto" onClick={onClear}>
                <X size={14} />
              </Button>
            </>
          )}
        </div>
      </div>
    </>
  );
}
