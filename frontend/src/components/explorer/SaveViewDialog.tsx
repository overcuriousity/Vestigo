import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { viewsApi } from "@/api/views";
import { Dialog, DialogContent, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { filtersToViewPayload } from "@/lib/queryParams";
import type { EventFilters } from "@/api/types";

interface Props {
  open: boolean;
  onClose: () => void;
  caseId: string;
  filters: EventFilters;
  /** The grid's current column layout — saved with the view so applying it
   *  later restores what the analyst was actually looking at. Omitted where
   *  there is no grid to speak for (the agent's finding cards): the view then
   *  makes no statement about columns and applying it leaves them alone. */
  visibleColumns?: string[];
}

export function SaveViewDialog({ open, onClose, caseId, filters, visibleColumns }: Props) {
  const [name, setName] = useState("");
  const qc = useQueryClient();

  const { mutate, isPending, error } = useMutation({
    mutationFn: () =>
      viewsApi.create(caseId, name.trim(), filters.q ?? "", {
        ...filtersToViewPayload(filters),
        // Added here rather than inside filtersToViewPayload: the Visualize
        // page shares that helper for chart configs, where columns mean
        // nothing.
        ...(visibleColumns ? { columns: visibleColumns } : {}),
      }),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["views", caseId] });
      onClose();
      setName("");
    },
  });

  return (
    <Dialog open={open} onOpenChange={(o) => { if (!o) { setName(""); onClose(); } }}>
      <DialogContent title="Save View" description="Name this filter set for quick access later.">
        <div className="space-y-3">
          <Input
            autoFocus
            placeholder="e.g. Suspicious PowerShell events"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && name.trim() && mutate()}
          />
          {error && (
            <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
          )}
          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm" onClick={onClose}>Cancel</Button>
            </DialogClose>
            <Button
              variant="accent"
              size="sm"
              disabled={!name.trim() || isPending}
              onClick={() => mutate()}
            >
              {isPending ? "Saving…" : "Save View"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
