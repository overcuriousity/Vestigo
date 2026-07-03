import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Merge } from "lucide-react";
import { timelinesApi } from "@/api/timelines";
import { sourcesApi } from "@/api/sources";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { FieldMappingEditor, type FieldMappings } from "./FieldMappingEditor";
import type { Timeline } from "@/api/types";

interface Props {
  caseId: string;
  timeline: Timeline;
}

/**
 * Edit a timeline's field mappings after creation (issue #10). Mappings are
 * auditable timeline metadata — every change lands in the audit trail; the
 * underlying events are untouched.
 */
export function EditFieldMappingsDialog({ caseId, timeline }: Props) {
  const [open, setOpen] = useState(false);
  const [mappings, setMappings] = useState<FieldMappings>({});
  const qc = useQueryClient();

  const { data: sources } = useQuery({
    queryKey: ["sources", caseId],
    queryFn: () => sourcesApi.list(caseId),
    enabled: open,
  });

  const { mutate, isPending, error, reset } = useMutation({
    mutationFn: () => timelinesApi.patchFieldMappings(caseId, timeline.id, mappings),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["timelines", caseId] });
      qc.invalidateQueries({ queryKey: ["fields"] });
      setOpen(false);
    },
  });

  const openChange = (next: boolean) => {
    setOpen(next);
    if (next) {
      setMappings(timeline.field_mappings ?? {});
      reset();
    }
  };

  return (
    <Dialog open={open} onOpenChange={openChange}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="icon" title="Edit field mappings">
          <Merge size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Field mappings"
        description="Merge equivalent raw fields into canonical ones. Applied at query time; changes are audited and the original events never change."
        className="max-w-2xl"
      >
        <div className="space-y-3">
          <FieldMappingEditor
            caseId={caseId}
            sourceIds={timeline.source_ids}
            sources={sources ?? []}
            value={mappings}
            onChange={setMappings}
          />
          {error && (
            <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            <Button variant="accent" size="sm" disabled={isPending} onClick={() => mutate()}>
              {isPending ? "Saving…" : "Save mappings"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
