import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { JobStatusRow } from "@/components/ui/JobStatusRow";
import { Download } from "lucide-react";
import { ApiError } from "@/api/client";
import { transferApi } from "@/api/transfer";
import { useTransferRate } from "@/hooks/useTransferRate";
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
  const [starting, setStarting] = useState(false);
  // The server deletes the archive once it has been streamed, so the download
  // must fire exactly once per job — StrictMode double-invokes effects, and any
  // re-render with a fresh `job` reference would otherwise refetch and 404.
  const downloadedRef = useRef(false);
  // Same synchronous-guard reasoning as ImportCaseDialog: a second click can
  // land before React re-renders the button as disabled, and the endpoint is
  // capped by `transfer_max_concurrent`.
  const startingRef = useRef(false);
  const [downloading, setDownloading] = useState(false);
  const { state: dl, report: reportDl, reset: resetDl } = useTransferRate();

  // The dialog polls the job itself — JobTray's invalidation can't trigger a
  // browser download, so on completion we fetch the archive here.
  const { data: job } = useQuery({
    queryKey: ["transfer-export", jobId],
    queryFn: () => transferApi.getJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) =>
      query.state.data?.status === "completed" || query.state.data?.status === "failed"
        ? false
        : 2000,
  });

  const download = useCallback(
    (id: string) => {
      downloadedRef.current = true;
      setError(null);
      setDownloading(true);
      resetDl();
      transferApi
        .downloadExport(case_.id, id, case_.name, { onProgress: reportDl })
        .then(() => {
          setDownloading(false);
          setOpen(false);
        })
        .catch((e) => {
          // Worth offering a retry rather than making the analyst export the
          // case again: the archive is only deleted once the response has
          // fully streamed, so a transfer that died early still has one. If it
          // died late the retry 404s, which surfaces as an error here anyway.
          downloadedRef.current = false;
          setDownloading(false);
          setError(
            e instanceof ApiError && e.status === 0
              ? "The transfer was interrupted before it finished — the archive is still on the server."
              : (e as Error).message,
          );
        });
    },
    [case_.id, case_.name, reportDl, resetDl],
  );

  useEffect(() => {
    if (job?.status === "completed" && !downloadedRef.current) {
      download(job.id);
    } else if (job?.status === "failed") {
      setError(job.error ?? "Export failed");
    }
  }, [job, download]);

  const start = () => {
    if (startingRef.current) return;
    startingRef.current = true;
    setStarting(true);
    setError(null);
    downloadedRef.current = false;
    transferApi
      .startExport(case_.id, includeBlobs)
      .then((r) => setJobId(r.job_id))
      .catch((e) => setError((e as Error).message))
      .finally(() => {
        startingRef.current = false;
        setStarting(false);
      });
  };

  const jobRunning = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));
  const busy = starting || jobRunning || downloading;

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
          {downloading && (
            <JobStatusRow
              label="Downloading archive"
              status="running"
              error={null}
              progress={{
                // `Content-Length` arrives with the first progress event; until
                // then the exporter's published archive size sizes the bar.
                total: dl?.total ?? job?.progress?.bytes_total ?? 0,
                processed: dl?.loaded ?? 0,
                rate_bps: dl?.rate_bps,
                eta_s: dl?.eta_s,
              }}
            />
          )}
          {error && <p className="text-xs text-[var(--color-danger)]">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            {job?.status === "completed" && error ? (
              <Button variant="accent" size="sm" onClick={() => download(job.id)}>
                Retry download
              </Button>
            ) : (
              <Button variant="accent" size="sm" disabled={busy} onClick={start}>
                {downloading ? "Downloading…" : busy ? "Exporting…" : "Export"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
