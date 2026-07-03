import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Merge, Plus } from "lucide-react";
import { timelinesApi } from "@/api/timelines";
import { sourcesApi } from "@/api/sources";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { Spinner } from "@/components/ui/Spinner";
import { FieldMappingEditor, type FieldMappings } from "./FieldMappingEditor";

interface Props {
  caseId: string;
}

const STEPS = ["Name", "Sources", "Field aggregation", "Review"] as const;

function StepIndicator({ step }: { step: number }) {
  return (
    <div className="mb-4 flex items-center gap-1.5 text-[11px] text-[var(--color-fg-muted)]">
      {STEPS.map((label, i) => (
        <span key={label} className="flex items-center gap-1.5">
          {i > 0 && <span className="opacity-40">›</span>}
          <span
            className={
              i === step
                ? "font-medium text-[var(--color-accent)]"
                : i < step
                  ? "text-[var(--color-fg-secondary)]"
                  : undefined
            }
          >
            {i + 1}. {label}
          </span>
        </span>
      ))}
    </div>
  );
}

/**
 * Stepped timeline-creation wizard (issue #10): name → source selection →
 * optional field aggregation (merge equivalent raw fields into canonical
 * ones) → review. Mappings are applied at query time; the ingested events
 * are never modified.
 */
export function CreateTimelineDialog({ caseId }: Props) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [selectedSourceIds, setSelectedSourceIds] = useState<Set<string>>(new Set());
  const [mappings, setMappings] = useState<FieldMappings>({});
  const qc = useQueryClient();

  const { data: sources, isLoading: isLoadingSources } = useQuery({
    queryKey: ["sources", caseId],
    queryFn: () => sourcesApi.list(caseId),
    enabled: open,
  });

  const { mutate, isPending, error, reset } = useMutation({
    mutationFn: () =>
      timelinesApi.create(
        caseId,
        name.trim(),
        desc.trim() || undefined,
        Array.from(selectedSourceIds),
        mappings,
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["timelines", caseId] });
      setOpen(false);
    },
  });

  const openChange = (next: boolean) => {
    setOpen(next);
    if (next) {
      setStep(0);
      setName("");
      setDesc("");
      setSelectedSourceIds(new Set());
      setMappings({});
      reset();
    }
  };

  const toggleSource = (id: string) => {
    setSelectedSourceIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const canNext = step === 0 ? name.trim().length > 0 : step === 1 ? selectedSourceIds.size > 0 : true;

  return (
    <Dialog open={open} onOpenChange={openChange}>
      <DialogTrigger asChild>
        <Button variant="accent" size="sm">
          <Plus size={14} /> New Timeline
        </Button>
      </DialogTrigger>
      <DialogContent
        title="New Timeline"
        description="A timeline is a named grouping of one or more sources."
        className={step === 2 ? "max-w-2xl" : undefined}
      >
        <StepIndicator step={step} />
        <div className="space-y-3">
          {step === 0 && (
            <>
              <div>
                <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
                  Name <span className="text-[var(--color-danger)]">*</span>
                </label>
                <Input
                  placeholder="e.g. Lateral movement timeline"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  autoFocus
                  maxLength={255}
                />
              </div>
              <div>
                <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
                  Description
                </label>
                <Input
                  placeholder="Notes about this grouping…"
                  value={desc}
                  onChange={(e) => setDesc(e.target.value)}
                  maxLength={4096}
                />
              </div>
            </>
          )}

          {step === 1 && (
            <div>
              <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
                Sources <span className="text-[var(--color-danger)]">*</span>
              </label>
              {isLoadingSources && <Spinner size={16} />}
              {sources && sources.length === 0 && (
                <p className="text-xs text-[var(--color-fg-muted)]">
                  No sources available. Upload a source first.
                </p>
              )}
              {sources && sources.length > 0 && (
                <div className="max-h-48 space-y-1 overflow-y-auto rounded border border-[var(--color-border)] p-2">
                  {sources.map((source) => (
                    <label
                      key={source.id}
                      className="flex items-center gap-2 text-xs text-[var(--color-fg-secondary)]"
                    >
                      <input
                        type="checkbox"
                        checked={selectedSourceIds.has(source.id)}
                        onChange={() => toggleSource(source.id)}
                        className="rounded border-[var(--color-border-strong)] accent-[var(--color-accent)]"
                      />
                      <span className="truncate">{source.name}</span>
                    </label>
                  ))}
                </div>
              )}
            </div>
          )}

          {step === 2 && (
            <div>
              <p className="mb-3 text-xs text-[var(--color-fg-muted)]">
                Optional: merge fields that carry the same kind of data under
                different names (e.g. <span className="font-mono">src_ip</span> and{" "}
                <span className="font-mono">ip_addr</span>) into one canonical field.
                Applied at query time — the original events are never modified.
                Skip if the sources are already consistent.
              </p>
              <FieldMappingEditor
                caseId={caseId}
                sourceIds={Array.from(selectedSourceIds)}
                sources={sources ?? []}
                value={mappings}
                onChange={setMappings}
              />
            </div>
          )}

          {step === 3 && (
            <div className="space-y-2 text-xs text-[var(--color-fg-secondary)]">
              <p>
                <span className="text-[var(--color-fg-muted)]">Name:</span>{" "}
                <span className="font-medium text-[var(--color-fg-primary)]">{name}</span>
              </p>
              {desc.trim() && (
                <p>
                  <span className="text-[var(--color-fg-muted)]">Description:</span> {desc}
                </p>
              )}
              <p>
                <span className="text-[var(--color-fg-muted)]">Sources:</span>{" "}
                {Array.from(selectedSourceIds)
                  .map((id) => sources?.find((s) => s.id === id)?.name ?? id)
                  .join(", ")}
              </p>
              {Object.keys(mappings).length > 0 ? (
                <div className="space-y-1">
                  <p className="text-[var(--color-fg-muted)]">Field mappings:</p>
                  {Object.entries(mappings).map(([canonical, raws]) => (
                    <p key={canonical} className="flex items-center gap-1.5 font-mono">
                      <Merge size={10} className="text-[var(--color-accent)]" />
                      {canonical} ← {raws.join(", ")}
                    </p>
                  ))}
                </div>
              ) : (
                <p className="text-[var(--color-fg-muted)]">No field mappings.</p>
              )}
            </div>
          )}

          {error && (
            <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
          )}

          <div className="flex justify-between gap-2 pt-1">
            <div className="flex items-center gap-2">
              {step === 2 && Object.keys(mappings).length > 0 && (
                <Badge variant="accent">
                  {Object.keys(mappings).length} mapping{Object.keys(mappings).length !== 1 ? "s" : ""}
                </Badge>
              )}
            </div>
            <div className="flex gap-2">
              <DialogClose asChild>
                <Button variant="ghost" size="sm">Cancel</Button>
              </DialogClose>
              {step > 0 && (
                <Button variant="outline" size="sm" onClick={() => setStep(step - 1)}>
                  Back
                </Button>
              )}
              {step < STEPS.length - 1 ? (
                <Button
                  variant="accent"
                  size="sm"
                  disabled={!canNext}
                  onClick={() => setStep(step + 1)}
                >
                  {step === 2 && Object.keys(mappings).length === 0 ? "Skip" : "Next"}
                </Button>
              ) : (
                <Button
                  variant="accent"
                  size="sm"
                  disabled={isPending}
                  onClick={() => mutate()}
                >
                  {isPending ? "Creating…" : "Create Timeline"}
                </Button>
              )}
            </div>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
