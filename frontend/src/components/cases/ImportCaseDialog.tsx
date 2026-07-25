import { useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Upload } from "lucide-react";
import { transferApi } from "@/api/transfer";

export function ImportCaseDialog() {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);
  const qc = useQueryClient();
  const navigate = useNavigate();

  const { data: job } = useQuery({
    queryKey: ["transfer-import", jobId],
    queryFn: () => transferApi.getJob(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) =>
      query.state.data?.status === "completed" || query.state.data?.status === "failed"
        ? false
        : 2000,
  });

  useEffect(() => {
    // `Job.result` is loosely typed (`unknown`) — the import job returns the
    // new case id there, so narrow it before use.
    const caseId = (job?.result as { case_id?: string } | null | undefined)?.case_id;
    if (job?.status === "completed" && caseId) {
      qc.invalidateQueries({ queryKey: ["cases"] });
      setOpen(false);
      navigate(`/cases/${caseId}`);
    } else if (job?.status === "failed") {
      setError(job.error ?? "Import failed");
    }
  }, [job, qc, navigate]);

  const start = () => {
    if (!file) return;
    setError(null);
    transferApi
      .startImport(file)
      .then((r) => setJobId(r.job_id))
      .catch((e) => setError((e as Error).message));
  };

  const running = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button variant="ghost" size="sm">
          <Upload size={14} /> Import
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Import Case"
        description="Restore a .vestigo archive as a new case owned by you. Nobody else gets access automatically."
      >
        <div className="space-y-3">
          <input
            ref={inputRef}
            type="file"
            accept=".vestigo"
            onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            className="block w-full text-sm text-[var(--color-fg-primary)]"
          />
          {error && <p className="text-xs text-[var(--color-danger)]">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            <Button variant="accent" size="sm" disabled={!file || running} onClick={start}>
              {running ? "Importing…" : "Import"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
