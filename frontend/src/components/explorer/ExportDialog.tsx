import { useState } from "react";
import { Download } from "lucide-react";
import { downloadExport } from "@/api/export";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { TransferProgressRow } from "@/components/ui/TransferProgressRow";
import { useFileTransfer } from "@/hooks/useFileTransfer";
import type { EventFilters } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  filters: EventFilters;
  total: number | null;
}

export function ExportDialog({ caseId, timelineId, filters, total }: Props) {
  const [open, setOpen] = useState(false);
  const [format, setFormat] = useState<"csv" | "jsonl">("csv");

  // The response is a chunked stream with no `Content-Length`, so progress can
  // only ever be bytes-so-far against an unknown total — an indeterminate bar.
  // That is still the difference between "working" and "hung" on an export of
  // a few million events, which is the whole point.
  const download = useFileTransfer({
    mutationFn: (o) => downloadExport(caseId, timelineId, format, filters, o),
    onSuccess: () => setOpen(false),
  });

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Download size={13} /> Export
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Export Events"
        description={
          total !== null
            ? `Download all ${total.toLocaleString()} matching events with current filters applied.`
            : "Download all matching events with current filters applied."
        }
      >
        <div className="space-y-4">
          <div>
            <label className="mb-2 block text-xs text-[var(--color-fg-muted)]">
              Format
            </label>
            <div className="flex gap-2">
              {(["csv", "jsonl"] as const).map((f) => (
                <button
                  key={f}
                  onClick={() => setFormat(f)}
                  className={`flex-1 rounded border px-3 py-2 text-sm font-mono transition-base ${
                    format === f
                      ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)] text-[var(--color-accent)]"
                      : "border-[var(--color-border)] text-[var(--color-fg-muted)] hover:border-[var(--color-border-strong)]"
                  }`}
                >
                  .{f}
                </button>
              ))}
            </div>
          </div>
          <p className="text-xs text-[var(--color-fg-muted)]">
            The server streams this with no row limit, but your browser holds the whole
            file in memory until it finishes — an export of many million events can be
            slow and heavy here even though the backend is fine. For a full copy of the
            case, export the case archive instead.
          </p>
          {download.active && (
            <TransferProgressRow
              label={`Downloading .${format}`}
              state={download.state}
              // Nothing is written to disk until the transfer completes, so
              // cancelling simply drops it.
              onCancel={download.cancel}
              cancelLabel="Cancel download"
            />
          )}
          {download.error && (
            <p className="text-xs text-[var(--color-danger)]">{download.error}</p>
          )}
          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            <Button
              variant="accent"
              size="sm"
              disabled={download.active}
              onClick={() => download.submit()}
            >
              <Download size={13} />
              {download.active ? "Downloading…" : `Download .${format}`}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
