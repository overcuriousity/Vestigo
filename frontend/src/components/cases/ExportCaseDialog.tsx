import { useEffect, useState } from "react";
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

  useEffect(() => {
    if (job?.status === "completed") {
      transferApi
        .downloadExport(case_.id, job.id, case_.name)
        .then(() => setOpen(false))
        .catch((e) => setError((e as Error).message));
    } else if (job?.status === "failed") {
      setError(job.error ?? "Export failed");
    }
  }, [job, case_.id, case_.name]);

  const start = () => {
    setError(null);
    transferApi
      .startExport(case_.id, includeBlobs)
      .then((r) => setJobId(r.job_id))
      .catch((e) => setError((e as Error).message));
  };

  const running = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm" title="Export case as .vestigo archive">
          <Download size={14} />
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Export Case"
        description="Downloads a single .vestigo archive: all case data, events, and analyst work. Restorable on any Vestigo instance."
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
            <Button variant="accent" size="sm" disabled={running} onClick={start}>
              {running ? "Exporting…" : "Export"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
