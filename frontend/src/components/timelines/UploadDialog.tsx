import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Upload, FileText } from "lucide-react";
import { sourcesApi } from "@/api/sources";
import { useJobsStore } from "@/stores/jobs";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { fmtBytes } from "@/lib/format";

interface Props {
  caseId: string;
}

export function UploadDialog({ caseId }: Props) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [parser, setParser] = useState("");
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const addJob = useJobsStore((s) => s.addJob);

  const { mutate, isPending, error, data } = useMutation({
    mutationFn: () =>
      sourcesApi.upload(
        caseId,
        file!,
        file?.name,
        parser || undefined,
      ),
    onSuccess: (result) => {
      qc.invalidateQueries({ queryKey: ["sources", caseId] });
      qc.invalidateQueries({ queryKey: ["timelines", caseId] });
      // Ingestion continues as a background job — hand it to the job tray
      // (which polls progress and refreshes the source list with the final
      // event count) and close the dialog. Keep the dialog open for
      // duplicates so the message is visible.
      if (!result.duplicate && result.job_id) {
        addJob(result.job_id, `Ingesting "${file?.name ?? "upload"}"`, [
          ["sources", caseId],
          ["timelines", caseId],
        ]);
        setOpen(false);
        setFile(null);
        setParser("");
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
        title="Upload source file"
        description="Uploading creates a new Source and adds it to the default timeline. Supported formats: Timesketch CSV, JSONL. Parser auto-detected if omitted."
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
