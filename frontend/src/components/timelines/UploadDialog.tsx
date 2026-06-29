import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Upload, FileText } from "lucide-react";
import { timelinesApi } from "@/api/timelines";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { fmtBytes } from "@/lib/format";

interface Props {
  caseId: string;
  timelineId: string;
  timelineName: string;
}

export function UploadDialog({ caseId, timelineId, timelineName }: Props) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [parser, setParser] = useState("");
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();

  const { mutate, isPending, error, data } = useMutation({
    mutationFn: () =>
      timelinesApi.upload(caseId, timelineId, file!, parser || undefined),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["timeline", caseId, timelineId] });
      qc.invalidateQueries({ queryKey: ["timelines", caseId] });
      console.info("Upload result", result);
      // Auto-close on successful new uploads after a short delay so the user
      // cannot immediately click Upload again on the same selection. Keep the
      // dialog open for duplicates so the message is visible.
      if (!result.duplicate) {
        window.setTimeout(() => {
          setOpen(false);
          setFile(null);
          setParser("");
        }, 1200);
      }
    },
  });

  const handleFile = (f: File) => setFile(f);

  // Reset selection whenever the dialog is reopened so a previous upload does
  // not linger.
  useEffect(() => {
    if (open) {
      setFile(null);
      setParser("");
    }
  }, [open]);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Upload size={13} /> Upload Log File
        </Button>
      </DialogTrigger>
      <DialogContent
        title={`Upload to "${timelineName}"`}
        description="Supported formats: Timesketch CSV, JSONL. Parser auto-detected if omitted."
      >
        <div className="space-y-4">
          {/* Drop zone */}
          <div
            onDragOver={(e) => {
              e.preventDefault();
              setDragging(true);
            }}
            onDragLeave={() => setDragging(false)}
            onDrop={(e) => {
              e.preventDefault();
              setDragging(false);
              const f = e.dataTransfer.files[0];
              if (f) handleFile(f);
            }}
            onClick={() => inputRef.current?.click()}
            className={`flex cursor-pointer flex-col items-center gap-2 rounded-lg border-2 border-dashed px-6 py-8 text-center transition-base ${
              dragging
                ? "border-[var(--color-accent)] bg-[var(--color-accent-dim)]"
                : "border-[var(--color-border-strong)] bg-[var(--color-bg-base)] hover:border-[var(--color-accent)] hover:bg-[var(--color-accent-dim)]"
            }`}
          >
            <FileText
              size={28}
              className="text-[var(--color-fg-muted)] opacity-60"
            />
            {file ? (
              <div>
                <p className="text-sm font-medium text-[var(--color-fg-primary)]">
                  {file.name}
                </p>
                <p className="text-xs text-[var(--color-fg-muted)]">
                  {fmtBytes(file.size)}
                </p>
              </div>
            ) : (
              <>
                <p className="text-sm text-[var(--color-fg-secondary)]">
                  Drop a log file here or click to browse
                </p>
                <p className="text-xs text-[var(--color-fg-muted)]">
                  .csv, .jsonl, .log — any size
                </p>
              </>
            )}
            <input
              ref={inputRef}
              type="file"
              className="hidden"
              onChange={(e) => {
                const f = e.target.files?.[0];
                if (f) handleFile(f);
              }}
            />
          </div>

          {/* Parser override */}
          <div>
            <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
              Parser <span className="text-[var(--color-fg-muted)]">(optional, auto-detected)</span>
            </label>
            <Input
              placeholder="e.g. timesketch_csv, jsonl"
              value={parser}
              onChange={(e) => setParser(e.target.value)}
            />
          </div>

          {/* Result */}
          {data && data.duplicate && (
            <div className="rounded border border-[var(--color-warning)]/40 bg-[var(--color-warning-dim)] px-3 py-2 text-xs text-[var(--color-warning)]">
              This file has already been ingested ({data.events_parsed.toLocaleString()} events).
            </div>
          )}
          {data && !data.duplicate && (
            <div className="rounded border border-[var(--color-success)]/40 bg-[var(--color-success-dim)] px-3 py-2 text-xs text-[var(--color-success)]">
              Ingested {data.events_inserted.toLocaleString()} events via{" "}
              <span className="font-mono">{data.parser}</span>
              {data.events_parsed !== data.events_inserted &&
                ` (${data.events_parsed.toLocaleString()} parsed)`}
            </div>
          )}

          {error && (
            <p className="text-xs text-[var(--color-danger)]">
              {(error as Error).message}
            </p>
          )}

          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Close</Button>
            </DialogClose>
            <Button
              variant="accent"
              size="sm"
              disabled={!file || isPending}
              onClick={() => mutate()}
            >
              {isPending ? "Uploading…" : "Upload"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
