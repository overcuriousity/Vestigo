import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { AlertTriangle, Upload } from "lucide-react";
import { transferApi } from "@/api/transfer";

/** `Job.result` is loosely typed (`unknown`); this is what the import job puts there. */
interface ImportResult {
  case_id?: string;
  warnings?: string[];
}

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

  const result = job?.result as ImportResult | null | undefined;
  const caseId = result?.case_id;
  const warnings = result?.warnings ?? [];

  const goToCase = useCallback(
    (id: string) => {
      qc.invalidateQueries({ queryKey: ["cases"] });
      setOpen(false);
      navigate(`/cases/${id}`);
    },
    [qc, navigate],
  );

  useEffect(() => {
    // Navigate straight through on a clean import, but hold the dialog open
    // when the importer had something to say — "no blob for X", "user Y not
    // found, attributed to importer", "N timeline(s) were embedded" all
    // change what the analyst is looking at, and navigating away is the one
    // moment they can never be surfaced again (the job store is in-memory).
    if (job?.status === "completed" && caseId && warnings.length === 0) {
      goToCase(caseId);
    } else if (job?.status === "failed") {
      setError(job.error ?? "Import failed");
    }
  }, [job, caseId, warnings.length, goToCase]);

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
          {warnings.length > 0 && (
            <div className="space-y-1">
              <p className="text-xs text-[var(--color-fg-muted)]">
                The case was restored, with caveats:
              </p>
              <ul className="space-y-0.5">
                {warnings.map((w, i) => (
                  <li
                    key={i}
                    className="flex items-start gap-1 text-[11px] text-[var(--color-warning)]"
                  >
                    <AlertTriangle size={11} className="mt-0.5 shrink-0" />
                    <span>{w}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}
          <div className="flex justify-end gap-2 pt-1">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Cancel</Button>
            </DialogClose>
            {caseId && warnings.length > 0 ? (
              <Button variant="accent" size="sm" onClick={() => goToCase(caseId)}>
                Go to case
              </Button>
            ) : (
              <Button variant="accent" size="sm" disabled={!file || running} onClick={start}>
                {running ? "Importing…" : "Import"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
