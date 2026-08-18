import { useEffect, useMemo, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Upload, Wand2 } from "lucide-react";
import { agentApi } from "@/api/agent";
import { convertersApi } from "@/api/converters";
import { useCapabilities } from "@/api/health";
import { sourcesApi } from "@/api/sources";
import { fmtBytes } from "@/lib/format";
import { useFileTransfer } from "@/hooks/useFileTransfer";
import { useJobsStore } from "@/stores/jobs";
import { tourEvent } from "@/stores/tour";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { FileDropZone } from "@/components/ui/FileInput";
import { Input } from "@/components/ui/Input";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { TransferProgressRow } from "@/components/ui/TransferProgressRow";

type Mode = "file" | "generate";

/** Files the normal upload already understands — pointless to send to a model. */
const KNOWN_SUFFIX = /\.(csv|jsonl|parquet)(\.gz)?$/i;

interface Props {
  caseId: string;
}

export function UploadDialog({ caseId }: Props) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [parser, setParser] = useState("");
  const [mode, setMode] = useState<Mode>("file");
  const [hint, setHint] = useState("");
  const [reuseId, setReuseId] = useState("");
  const qc = useQueryClient();
  const addJob = useJobsStore((s) => s.addJob);
  const caps = useCapabilities();
  const canGenerate = caps.converter_generation;
  const generating = mode === "generate" && canGenerate;

  // Only fetched while the AI mode is open: what the disclosure names (the
  // endpoint and model, from the same source the agent panel uses) and the
  // case's saved converters plus the sample budget the server will send.
  const { data: agentInfo } = useQuery({
    queryKey: ["agent-info"],
    queryFn: agentApi.getInfo,
    staleTime: 60_000,
    enabled: open && generating,
  });
  const { data: caseConverters } = useQuery({
    queryKey: ["converters", caseId],
    queryFn: () => convertersApi.listForCase(caseId),
    enabled: open && generating,
  });
  const reusable = useMemo(
    () => (caseConverters?.scripts ?? []).filter((c) => c.status === "working"),
    [caseConverters],
  );
  const sampleBytes = caseConverters?.sample_bytes ?? 65536;

  // The app's largest routine transfer: the server takes up to 10 GiB and the
  // ingest job the tray polls does not exist until the whole body has landed,
  // so the upload's own byte progress is the only feedback for what is often
  // the longest part of the operation.
  const upload = useFileTransfer({
    mutationFn: (o) => sourcesApi.upload(caseId, file!, file?.name, parser || undefined, o),
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
  });

  // The AI path: the server writes (or re-runs) a converter, then ingests the
  // Parquet it produced. Same transfer hook, different endpoint and job label.
  const convert = useFileTransfer({
    mutationFn: (o) =>
      convertersApi.convert(
        caseId,
        file!,
        { hint: hint.trim() || undefined, converterScriptId: reuseId || undefined },
        o,
      ),
    onSuccess: (result) => {
      addJob(
        result.job_id,
        reuseId
          ? `Converting "${file?.name ?? "upload"}" with a saved converter`
          : `Converting "${file?.name ?? "upload"}" with AI`,
        [
          ["sources", caseId],
          ["timelines", caseId],
          ["converters", caseId],
        ],
        true,
      );
      setOpen(false);
      setFile(null);
      setHint("");
      setReuseId("");
    },
  });

  const { reset } = upload;
  const { reset: resetConvert } = convert;
  // Reset selection and the previous upload's result/error whenever the
  // dialog is reopened, so a stale duplicate warning or error doesn't linger.
  useEffect(() => {
    if (open) {
      setFile(null);
      setParser("");
      setHint("");
      setReuseId("");
      reset();
      resetConvert();
      tourEvent("upload-dialog-opened");
    }
  }, [open, reset, resetConvert]);

  const active = upload.active || convert.active;
  const looksKnown = !!file && KNOWN_SUFFIX.test(file.name);
  const endpointHost = (() => {
    const url = agentInfo?.api_base_url;
    if (!url) return "the configured endpoint";
    try {
      return new URL(url).host;
    } catch {
      return url;
    }
  })();
  const modelName = agentInfo?.model ?? "the configured model";
  // Rough: the disclosure quotes bytes exactly and lines approximately.
  const approxLines = file ? Math.max(1, Math.round(Math.min(file.size, sampleBytes) / 80)) : 0;

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
          {canGenerate && (
            <SegmentedControl<Mode>
              value={mode}
              onChange={(m) => {
                setMode(m);
                setFile(null);
              }}
              options={[
                { id: "file", label: "Upload timeline", icon: Upload },
                {
                  id: "generate",
                  label: "Let AI write the converter",
                  icon: Wand2,
                  hint: "For plain-text logs no converter covers: a sample goes to the configured model, the script it writes runs here, and the result is ingested.",
                },
              ]}
            />
          )}

          <FileDropZone
            data-tour="upload-dropzone"
            accept={generating ? undefined : ".csv,.jsonl,.parquet,.log"}
            files={file}
            disabled={active}
            onFiles={(picked) => {
              if (picked[0]) setFile(picked[0]);
            }}
            hint={
              generating
                ? "Any plain-text, time-annotated log (.log, .txt, syslog, .gz…). Binary files are refused."
                : ".csv, .jsonl, .parquet — any size. Other formats (e.g. .log) need a parser override below."
            }
          />

          {generating && looksKnown && (
            <p className="text-xs text-[var(--color-warning)]">
              This looks like a file the normal upload already understands — switch to “Upload
              timeline” unless it really needs a converter.
            </p>
          )}

          {(upload.active || convert.active) && file && (
            <TransferProgressRow
              label={`Uploading ${file.name}`}
              state={generating ? convert.state : upload.state}
              fallbackTotal={file.size}
              // Safe: the server streams the body to a temp file and only
              // creates the Source row and ingest job once all of it has
              // landed, so a cancelled upload leaves nothing behind.
              onCancel={generating ? convert.cancel : upload.cancel}
              cancelLabel="Cancel upload"
            />
          )}

          {generating ? (
            <div className="space-y-3">
              {reusable.length > 0 && (
                <div>
                  <label
                    htmlFor="upload-reuse-converter"
                    className="mb-1 block text-xs text-[var(--color-fg-muted)]"
                  >
                    Reuse a converter from this case
                  </label>
                  <select
                    id="upload-reuse-converter"
                    className="h-8 w-full rounded border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-2 text-xs text-[var(--color-fg-primary)]"
                    value={reuseId}
                    onChange={(e) => setReuseId(e.target.value)}
                  >
                    <option value="">Generate a new one</option>
                    {reusable.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name} v{c.version}
                      </option>
                    ))}
                  </select>
                </div>
              )}

              {reuseId ? (
                <p
                  role="note"
                  className="rounded border border-[var(--color-border)] px-3 py-2 text-xs text-[var(--color-fg-muted)]"
                >
                  Nothing is sent to the model — the saved converter runs on this server.
                </p>
              ) : (
                <div
                  role="note"
                  className="rounded border border-[var(--color-warning)]/40 bg-[var(--color-warning-dim)] px-3 py-2 text-xs text-[var(--color-fg-primary)]"
                >
                  <p>
                    A {fmtBytes(sampleBytes)} excerpt
                    {file ? ` of “${file.name}” (about ${approxLines.toLocaleString()} lines)` : " of the file"}{" "}
                    — taken from its beginning, its middle and its end, so newest entries are
                    included — will be sent to <span className="font-mono">{modelName}</span> at{" "}
                    <span className="font-mono">{endpointHost}</span>. Nothing else about this case
                    is sent. The script it writes runs on this server in a guarded subprocess, and
                    every attempt is recorded with the converter.
                  </p>
                </div>
              )}

              {!reuseId && (
                <div>
                  <label
                    htmlFor="upload-converter-hint"
                    className="mb-1 block text-xs text-[var(--color-fg-muted)]"
                  >
                    Hint for the model{" "}
                    <span className="text-[var(--color-fg-muted)]">— optional</span>
                  </label>
                  <Input
                    id="upload-converter-hint"
                    placeholder="e.g. timestamps are local time (Europe/Berlin)"
                    value={hint}
                    onChange={(e) => setHint(e.target.value)}
                  />
                </div>
              )}
            </div>
          ) : (
            /* Parser override */
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
          )}

          {/* Result */}
          {upload.data?.duplicate && (
            <div className="rounded border border-[var(--color-warning)]/40 bg-[var(--color-warning-dim)] px-3 py-2 text-xs text-[var(--color-warning)]">
              {upload.data.status === "ready"
                ? `This file has already been ingested (${upload.data.events_parsed.toLocaleString()} events).`
                : "This file is already being ingested by another upload — check the source list for progress."}
            </div>
          )}
          {upload.error && (
            <p className="text-xs text-[var(--color-danger)]">{upload.error}</p>
          )}
          {convert.error && (
            <p className="text-xs text-[var(--color-danger)]">{convert.error}</p>
          )}

          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">Close</Button>
            </DialogClose>
            {generating ? (
              <Button
                variant="accent"
                size="sm"
                data-tour="upload-submit"
                disabled={!file || active}
                onClick={() => convert.submit()}
              >
                {convert.active
                  ? "Uploading…"
                  : reuseId
                    ? "Convert & ingest"
                    : "Generate & ingest"}
              </Button>
            ) : (
              <Button
                variant="accent"
                size="sm"
                data-tour="upload-submit"
                disabled={!file || active}
                onClick={() => upload.submit()}
              >
                {upload.active ? "Uploading…" : "Upload"}
              </Button>
            )}
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
