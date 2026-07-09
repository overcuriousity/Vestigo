import { useEffect, useRef, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Upload, FileText, ChevronDown, ChevronRight } from "lucide-react";
import { sourcesApi } from "@/api/sources";
import { ConverterPanel } from "@/components/sources/ConverterPanel";
import { useJobsStore } from "@/stores/jobs";
import { tourEvent } from "@/stores/tour";
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
  const [showConverters, setShowConverters] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const addJob = useJobsStore((s) => s.addJob);

  const { mutate, isPending, error, data, reset } = useMutation({
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
      // The upload action happened either way — tell the tour so a duplicate
      // response doesn't strand it on this step.
      tourEvent("source-uploaded");
      // Ingestion continues as a background job — hand it to the job tray
      // (which polls progress and refreshes the source list with the final
      // event count) and close the dialog. Keep the dialog open for
      // duplicates so the message is visible.
      if (!result.duplicate && result.job_id) {
        addJob(
          result.job_id,
          `Ingesting "${file?.name ?? "upload"}"`,
          [
            ["sources", caseId],
            ["timelines", caseId],
          ],
          true,
        );
        setOpen(false);
        setFile(null);
        setParser("");
      } else if (result.duplicate) {
        // Duplicates never get a job_id, so the tour's "ingesting" step
        // would otherwise wait forever on an event that can never fire.
        tourEvent("ingest-complete");
      }
      // A duplicate can point at a source that lost a concurrent-upload race
      // and is still ingesting (status !== "ready") — the source list panel
      // shows its live "Ingesting" badge/progress, so don't claim it's done.
    },
    // Upload failures render inline in the dialog — skip the global toast.
    meta: { silentError: true },
  });

  const handleFile = (f: File) => setFile(f);

  // Reset selection and the previous upload's result/error whenever the
  // dialog is reopened, so a stale duplicate warning or error doesn't linger.
  useEffect(() => {
    if (open) {
      setFile(null);
      setParser("");
      reset();
      tourEvent("upload-dialog-opened");
    }
  }, [open, reset]);

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm" data-tour="upload-log">
          <Upload size={13} /> Upload Log File
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Upload source file"
        description="Uploading creates a new Source and adds it to the default timeline. Supported formats: Timesketch CSV, JSONL, TraceSignal Parquet (from a converter script). Parser auto-detected if omitted."
      >
        <div className="space-y-4">
          {/* Drop zone */}
          <div
            data-tour="upload-dropzone"
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
                  .csv, .jsonl, .parquet, .log — any size
                </p>
              </>
            )}
            <input
              ref={inputRef}
              type="file"
              accept=".csv,.jsonl,.parquet,.log"
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
              placeholder="e.g. timesketch_csv, jsonl, tracesignal_parquet"
              value={parser}
              onChange={(e) => setParser(e.target.value)}
            />
          </div>

          {/* Converter downloads for raw (non-Timesketch) logs */}
          <div>
            <button
              type="button"
              data-tour="converter-hint"
              onClick={() => setShowConverters((s) => !s)}
              aria-expanded={showConverters}
              className="flex w-full items-center gap-1.5 text-left text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)] transition-base"
            >
              {showConverters ? <ChevronDown size={12} /> : <ChevronRight size={12} />}
              Raw logs (nginx, firewall, CloudTrail, browser, journal)? Get a converter script
            </button>
            {showConverters && (
              <div className="mt-2">
                <ConverterPanel />
              </div>
            )}
          </div>

          {/* Result */}
          {data && data.duplicate && (
            <div className="rounded border border-[var(--color-warning)]/40 bg-[var(--color-warning-dim)] px-3 py-2 text-xs text-[var(--color-warning)]">
              {data.status === "ready"
                ? `This file has already been ingested (${data.events_parsed.toLocaleString()} events).`
                : "This file is already being ingested by another upload — check the source list for progress."}
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
              data-tour="upload-submit"
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
