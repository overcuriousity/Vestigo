import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { JobStatusRow } from "@/components/ui/JobStatusRow";
import { TransferProgressRow } from "@/components/ui/TransferProgressRow";
import { Download } from "lucide-react";
import { jobsApi } from "@/api/jobs";
import { transferApi } from "@/api/transfer";
import { useFileTransfer } from "@/hooks/useFileTransfer";
import { jobPhaseLabel } from "@/lib/jobPhases";
import type { Case } from "@/api/types";

interface Props {
  case_: Case;
}

export function ExportCaseDialog({ case_ }: Props) {
  const [open, setOpen] = useState(false);
  const [includeBlobs, setIncludeBlobs] = useState(false);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // The server deletes the archive once it has been streamed, so the download
  // must fire exactly once per job — StrictMode double-invokes effects, and any
  // re-render with a fresh `job` reference would otherwise refetch and 404.
  const downloadedRef = useRef(false);

  // Unlike the import job, this one is *not* handed to the job tray: the
  // archive only becomes useful when this dialog turns it into a browser
  // download, and the server unlinks it once streamed. A tray row for it would
  // announce a finished export the analyst has no way to collect.
  const { data: job } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => jobsApi.get(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) =>
      query.state.data?.status === "completed" || query.state.data?.status === "failed"
        ? false
        : 2000,
  });

  // Guarded like the import upload even though a double-start here is a 429
  // rather than a duplicate (the endpoint is capped by
  // `transfer_max_concurrent`): same bug, milder symptom.
  const exportJob = useFileTransfer({
    mutationFn: () => transferApi.startExport(case_.id, includeBlobs),
    onSuccess: (r) => setJobId(r.job_id),
    onError: setError,
  });

  const archive = useFileTransfer<void>({
    mutationFn: (o) => transferApi.downloadExport(case_.id, jobId!, case_.name, o),
    onSuccess: () => setOpen(false),
    onError: (message) => {
      // Worth offering a retry rather than making the analyst export the case
      // again: the archive is only deleted once the response has fully
      // streamed, so a transfer that died early still has one. If it died late
      // the retry 404s, which surfaces as an error here anyway.
      downloadedRef.current = false;
      setError(`${message} The archive is still on the server.`);
    },
    onCancel: () => {
      downloadedRef.current = false;
    },
  });

  const { submit: submitDownload } = archive;
  const download = useCallback(() => {
    downloadedRef.current = true;
    setError(null);
    submitDownload();
  }, [submitDownload]);

  useEffect(() => {
    if (job?.status === "completed" && !downloadedRef.current) {
      download();
    } else if (job?.status === "failed") {
      setError(job.error ?? "Export failed");
    }
  }, [job, download]);

  const start = () => {
    setError(null);
    downloadedRef.current = false;
    exportJob.submit();
  };

  const jobRunning = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));
  const busy = exportJob.active || jobRunning || archive.active;

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        if (next) {
          setJobId(null);
          setError(null);
          downloadedRef.current = false;
        }
        setOpen(next);
      }}
    >
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm" title="Export case as .vestigo archive">
          <Download size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Export Case"
        description="Downloads a single .vestigo archive: all case data, events, and analyst work. Restorable on any Vestigo instance. The archive is removed from the server once downloaded."
      >
        <div className="space-y-3">
          <label className="flex items-center gap-2 text-sm text-[var(--color-fg-primary)]">
            <input
              type="checkbox"
              checked={includeBlobs}
              onChange={(e) => setIncludeBlobs(e.target.checked)}
            />
            Include original source files (larger archive, full backup)
          </label>
          {jobRunning && job && (
            <JobStatusRow
              label="Building archive"
              status={job.status}
              progress={job.progress}
              error={null}
              detail={jobPhaseLabel(job.kind, job.progress)}
            />
          )}
          {archive.active && (
            <TransferProgressRow
              label="Downloading archive"
              state={archive.state}
              // `Content-Length` arrives with the first progress event; until
              // then the exporter's published archive size sizes the bar.
              fallbackTotal={job?.progress?.bytes_total ?? null}
              // Aborting only costs the transfer: the server unlinks the
              // archive after a *completed* stream, so a cancelled download
              // leaves it in place and "Retry download" still works.
              onCancel={archive.cancel}
              cancelLabel="Cancel download"
            />
          )}
          {error && <p className="text-xs text-[var(--color-danger)]">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            {job?.status === "completed" && !archive.active && error ? (
              <Button variant="accent" size="sm" onClick={download}>
                Retry download
              </Button>
            ) : (
              <Button variant="accent" size="sm" disabled={busy} onClick={start}>
                {archive.active ? "Downloading…" : busy ? "Exporting…" : "Export"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
