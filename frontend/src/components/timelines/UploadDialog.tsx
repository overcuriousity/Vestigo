import { useEffect, useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Upload } from "lucide-react";
import { sourcesApi } from "@/api/sources";
import { useJobsStore } from "@/stores/jobs";
import { tourEvent } from "@/stores/tour";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { FileDropZone } from "@/components/ui/FileInput";
import { Input } from "@/components/ui/Input";

interface Props {
  caseId: string;
}

export function UploadDialog({ caseId }: Props) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [parser, setParser] = useState("");
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
          <Upload size={13} /> Upload Data
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Upload source file"
        description="Uploading creates a new Source and adds it to the default timeline. Supported formats: Timesketch CSV, JSONL, Vestigo Parquet (from a converter script). Parser auto-detected if omitted."
      >
        <div className="space-y-4">
          <FileDropZone
            data-tour="upload-dropzone"
            accept=".csv,.jsonl,.parquet,.log"
            files={file}
            onFiles={(picked) => {
              if (picked[0]) setFile(picked[0]);
            }}
            hint=".csv, .jsonl, .parquet — any size. Other formats (e.g. .log) need a parser override below."
          />

          {/* Parser override */}
          <div>
            <label className="mb-1 block text-xs text-[var(--color-fg-muted)]">
              Parser override{" "}
              <span className="text-[var(--color-fg-muted)]">
                — only needed for .log or unrecognized files; leave blank to
                auto-detect .csv/.jsonl/.parquet
              </span>
            </label>
            <Input
              placeholder="e.g. timesketch_csv, jsonl, vestigo_parquet"
              value={parser}
              onChange={(e) => setParser(e.target.value)}
            />
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
