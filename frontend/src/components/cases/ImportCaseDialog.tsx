import { useCallback, useEffect, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { FileInput } from "@/components/ui/FileInput";
import { JobStatusRow } from "@/components/ui/JobStatusRow";
import { TransferProgressRow } from "@/components/ui/TransferProgressRow";
import { AlertTriangle, Upload } from "lucide-react";
import { jobsApi } from "@/api/jobs";
import { transferApi } from "@/api/transfer";
import { useFileTransfer } from "@/hooks/useFileTransfer";
import { useJobsStore } from "@/stores/jobs";
import { jobPhaseLabel } from "@/lib/jobPhases";

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
  const qc = useQueryClient();
  const navigate = useNavigate();
  const addJob = useJobsStore((s) => s.addJob);

  // Same query key the job tray polls under, so the two collapse into one
  // request stream: this dialog hands the restore job to the tray, and both
  // then watch it. Deliberately still an independent `useQuery` rather than a
  // read of the tray's store — the dialog must not depend on another component
  // being mounted to see the job it started.
  const { data: job } = useQuery({
    queryKey: ["job", jobId],
    queryFn: () => jobsApi.get(jobId!),
    enabled: !!jobId,
    refetchInterval: (query) =>
      query.state.data?.status === "completed" || query.state.data?.status === "failed"
        ? false
        : 2000,
  });

  const upload = useFileTransfer({
    mutationFn: (o) => transferApi.startImport(file!, o),
    onSuccess: (r) => {
      setJobId(r.job_id);
      // The job outlives this dialog: hand it to the tray so closing the
      // dialog mid-import doesn't hide a restore that's still running.
      addJob(r.job_id, `Importing "${file?.name ?? "archive"}"`, [["cases"]]);
    },
    onError: setError,
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
    upload.submit();
  };

  const jobRunning = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));
  const busy = upload.active || jobRunning;

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
          <FileInput
            accept=".vestigo"
            disabled={busy}
            onFiles={(files) => setFile(files[0] ?? null)}
          />
          {upload.active && file && (
            <TransferProgressRow
              label={`Uploading ${file.name}`}
              state={upload.state}
              fallbackTotal={file.size}
              // Safe to offer: the server creates the import job only after the
              // whole upload lands, so an aborted upload leaves no job, no
              // case, and nothing to clean up.
              onCancel={upload.cancel}
              cancelLabel="Cancel upload"
            />
          )}
          {job && !caseId && (
            <JobStatusRow
              label="Restoring case"
              status={job.status}
              progress={job.progress}
              error={null}
              detail={jobPhaseLabel(job.kind, job.progress)}
            />
          )}
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
              <Button variant="ghost" size="sm">
                {/* Closing does not stop the restore — the tray keeps it
                    visible. Cancelling the *upload* is offered on its row. */}
                {busy ? "Close" : "Cancel"}
              </Button>
            </DialogClose>
            {caseId && warnings.length > 0 ? (
              <Button variant="accent" size="sm" onClick={() => goToCase(caseId)}>
                Go to case
              </Button>
            ) : (
              <Button variant="accent" size="sm" disabled={!file || busy} onClick={start}>
                {upload.active ? "Uploading…" : jobRunning ? "Importing…" : "Import"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
