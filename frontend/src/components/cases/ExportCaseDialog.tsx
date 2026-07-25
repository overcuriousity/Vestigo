import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Download } from "lucide-react";
import { transferApi } from "@/api/transfer";
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
      transferApi
        .downloadExport(case_.id, id, case_.name)
        .then(() => setOpen(false))
        .catch((e) => {
          // The archive survives a failed transfer, so a retry is worth
          // offering rather than making the analyst export the case again.
          downloadedRef.current = false;
          setError((e as Error).message);
        });
    },
    [case_.id, case_.name],
  );

  useEffect(() => {
    if (job?.status === "completed" && !downloadedRef.current) {
      download(job.id);
    } else if (job?.status === "failed") {
      setError(job.error ?? "Export failed");
    }
  }, [job, download]);

  const start = () => {
    setError(null);
    downloadedRef.current = false;
    transferApi
      .startExport(case_.id, includeBlobs)
      .then((r) => setJobId(r.job_id))
      .catch((e) => setError((e as Error).message));
  };

  const running = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));

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
              <Button variant="accent" size="sm" disabled={running} onClick={start}>
                {running ? "Exporting…" : "Export"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
