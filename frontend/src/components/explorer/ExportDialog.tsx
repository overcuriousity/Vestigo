import { useState } from "react";
import { Download } from "lucide-react";
import { downloadExport } from "@/api/export";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
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
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleExport = async () => {
    setLoading(true);
    setError(null);
    try {
      await downloadExport(caseId, timelineId, format, filters);
      setOpen(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setLoading(false);
    }
  };

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
            Streams directly from the backend — no memory limit. Large exports may take a
            moment to complete.
          </p>
          {error && (
            <p className="text-xs text-[var(--color-danger)]">{error}</p>
          )}
          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            <Button
              variant="accent"
              size="sm"
              disabled={loading}
              onClick={handleExport}
            >
              {loading ? (
                <>
                  <Spinner size={13} /> Downloading…
                </>
              ) : (
                <>
                  <Download size={13} /> Download .{format}
                </>
              )}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
