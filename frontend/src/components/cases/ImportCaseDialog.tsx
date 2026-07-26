import { useCallback, useEffect, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useNavigate } from "react-router-dom";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { FileInput } from "@/components/ui/FileInput";
import { JobStatusRow } from "@/components/ui/JobStatusRow";
import { AlertTriangle, Upload } from "lucide-react";
import { transferApi } from "@/api/transfer";
import { useTransferRate } from "@/hooks/useTransferRate";
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
  const [uploading, setUploading] = useState(false);
  // The ref, not the state, is what actually prevents a duplicate import: a
  // second click can land in the same task as the first, before React has
  // re-rendered the button as disabled. Archives are multi-GB, so that window
  // used to stay open for the entire upload.
  const submittingRef = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const upload = useTransferRate();
  const qc = useQueryClient();
  const navigate = useNavigate();
  const addJob = useJobsStore((s) => s.addJob);

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
    if (submittingRef.current || !file) return;
    submittingRef.current = true;
    setUploading(true);
    setError(null);
    upload.reset();
    const controller = new AbortController();
    abortRef.current = controller;
    transferApi
      .startImport(file, { onProgress: upload.report, signal: controller.signal })
      .then((r) => {
        setJobId(r.job_id);
        // The job outlives this dialog: hand it to the tray so closing the
        // dialog mid-import doesn't hide a restore that's still running.
        addJob(r.job_id, `Importing "${file.name}"`, [["cases"]]);
        setUploading(false);
        submittingRef.current = false;
      })
      .catch((e) => {
        // A cancel is not a failure — the user asked for it.
        if ((e as Error).name !== "AbortError") setError((e as Error).message);
        setUploading(false);
        submittingRef.current = false;
        upload.reset();
      });
  };

  const cancelUpload = () => {
    abortRef.current?.abort();
    abortRef.current = null;
  };

  const jobRunning = !!jobId && (!job || (job.status !== "completed" && job.status !== "failed"));
  const busy = uploading || jobRunning;

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
          {uploading && upload.state && file && (
            <JobStatusRow
              label={`Uploading ${file.name}`}
              status="running"
              error={null}
              progress={{
                total: upload.state.total ?? file.size,
                processed: upload.state.loaded,
                rate_bps: upload.state.rate_bps,
                eta_s: upload.state.eta_s,
              }}
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
            {uploading ? (
              // Safe to offer: the server creates the import job only after the
              // whole upload lands, so an aborted upload leaves no job, no
              // case, and nothing to clean up.
              <Button variant="ghost" size="sm" onClick={cancelUpload}>
                Cancel upload
              </Button>
            ) : (
              <DialogClose asChild>
                <Button variant="ghost" size="sm">Cancel</Button>
              </DialogClose>
            )}
            {caseId && warnings.length > 0 ? (
              <Button variant="accent" size="sm" onClick={() => goToCase(caseId)}>
                Go to case
              </Button>
            ) : (
              <Button variant="accent" size="sm" disabled={!file || busy} onClick={start}>
                {uploading ? "Uploading…" : jobRunning ? "Importing…" : "Import"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
