/**
 * The converter scripts the configured model wrote for this case
 * (`docs/INPUT_FORMATS.md` §"Generated converters").
 *
 * Each row is a record — name, version, status, model, how many sources it
 * produced — with Download (the script plus its provenance header) and, when
 * the capability is on, Regenerate (a new version from the retained raw file
 * plus a hint). Expanding a row shows every attempt the harness recorded and
 * the exact sample that was sent to the model. Renders nothing when the
 * feature is off *and* the case has no scripts; scripts outlive the switch.
 */
import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { useSearchParams } from "react-router-dom";
import { Download, RefreshCw, Wand2 } from "lucide-react";
import { agentApi } from "@/api/agent";
import { convertersApi } from "@/api/converters";
import type { ConverterAttempt, ConverterScript } from "@/api/converters";
import { useCapabilities } from "@/api/health";
import { useJobsStore } from "@/stores/jobs";
import { Badge } from "@/components/ui/Badge";
import { Button } from "@/components/ui/Button";
import { Dialog, DialogClose, DialogContent } from "@/components/ui/Dialog";
import { Input } from "@/components/ui/Input";
import { fmtTimestamp } from "@/lib/time";

interface Props {
  caseId: string;
}

function statusVariant(status: ConverterScript["status"]): "success" | "danger" | "default" {
  if (status === "working") return "success";
  if (status === "failed") return "danger";
  return "default";
}

function AttemptRow({ a }: { a: ConverterAttempt }) {
  const failed = a.validation ? a.validation.checks.filter((c) => c.enforced && !c.ok) : [];
  const ok = a.validation?.ok ?? false;
  return (
    <li className="rounded border border-[var(--color-border)] px-2 py-1.5">
      <div className="flex flex-wrap items-center gap-2 text-xs">
        <span className="font-mono">#{a.n}</span>
        <span className="text-[var(--color-fg-muted)]">{a.phase}</span>
        <Badge variant={ok ? "success" : "danger"}>{ok ? "passed" : "failed"}</Badge>
        {a.elapsed_ms > 0 && (
          <span className="text-[var(--color-fg-muted)]">{(a.elapsed_ms / 1000).toFixed(1)}s</span>
        )}
        {a.exit_code != null && a.exit_code !== 0 && (
          <span className="text-[var(--color-fg-muted)]">exit {a.exit_code}</span>
        )}
      </div>
      {a.error && <p className="mt-1 text-xs text-[var(--color-danger)]">{a.error}</p>}
      {failed.length > 0 && (
        <ul className="mt-1 space-y-0.5 text-xs">
          {failed.map((c) => (
            <li key={c.name}>
              <span className="font-mono">{c.name}</span>
              <span className="text-[var(--color-fg-muted)]"> — {c.detail}</span>
            </li>
          ))}
        </ul>
      )}
      {a.stderr_tail && (
        <pre className="mt-1 max-h-32 overflow-auto rounded bg-[var(--color-bg-base)] p-1.5 text-xs leading-snug whitespace-pre-wrap">
          {a.stderr_tail.split("\n").slice(-8).join("\n")}
        </pre>
      )}
    </li>
  );
}

function ScriptDetail({ caseId, id }: { caseId: string; id: string }) {
  // Under the list's key prefix on purpose: the job tray's terminal
  // invalidation of ["converters", caseId] must refetch an expanded row's
  // attempts too, or a saved script re-run keeps showing its pre-job trail.
  const { data, isLoading } = useQuery({
    queryKey: ["converters", caseId, "detail", id],
    queryFn: () => convertersApi.getForCase(caseId, id),
  });
  if (isLoading || !data) {
    return <p className="text-xs text-[var(--color-fg-muted)]">Loading…</p>;
  }
  return (
    <div className="space-y-2">
      {data.hint && (
        <p className="text-xs">
          <span className="text-[var(--color-fg-muted)]">Hint: </span>
          {data.hint}
        </p>
      )}
      <div>
        <p className="mb-1 text-xs font-medium text-[var(--color-fg-secondary)]">
          Attempts ({data.attempts.length})
        </p>
        {data.attempts.length === 0 ? (
          <p className="text-xs text-[var(--color-fg-muted)]">None recorded.</p>
        ) : (
          <ul className="space-y-1">
            {data.attempts.map((a) => (
              <AttemptRow key={a.n} a={a} />
            ))}
          </ul>
        )}
      </div>
      <div>
        <p className="mb-1 text-xs font-medium text-[var(--color-fg-secondary)]">
          Sample sent to the model
        </p>
        <pre className="max-h-40 overflow-auto rounded bg-[var(--color-bg-base)] p-1.5 text-xs leading-snug whitespace-pre-wrap">
          {data.sample_excerpt || "(empty)"}
        </pre>
      </div>
    </div>
  );
}

function RegenerateDialog({
  caseId,
  script,
  open,
  onOpenChange,
  onStarted,
}: {
  caseId: string;
  script: ConverterScript;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  onStarted: (jobId: string) => void;
}) {
  const [hint, setHint] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const addJob = useJobsStore((s) => s.addJob);
  const { data: info } = useQuery({
    queryKey: ["agent-info"],
    queryFn: agentApi.getInfo,
    staleTime: 60_000,
    enabled: open,
  });
  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await convertersApi.regenerate(caseId, script.id, hint.trim() || undefined);
      addJob(
        r.job_id,
        `Regenerating ${script.name}`,
        [
          ["converters", caseId],
          ["sources", caseId],
          ["timelines", caseId],
        ],
      );
      // No list invalidation here: the new row is inserted only after the
      // model's first reply, so a refetch now would find nothing changed. The
      // panel polls the list while it knows a job is in flight instead.
      onStarted(r.job_id);
      onOpenChange(false);
      setHint("");
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        title={`Regenerate ${script.name}`}
        description="Writes a new version of this converter from the same raw file, then converts and ingests it again."
      >
        <div className="space-y-3">
          <p
            role="note"
            className="rounded border border-[var(--color-warning)]/40 bg-[var(--color-warning-dim)] px-3 py-2 text-xs"
          >
            The stored sample of {script.raw_filename ?? "the raw file"} will be sent to{" "}
            <span className="font-mono">{info?.model ?? "the configured model"}</span> again.
          </p>
          <div>
            <label
              htmlFor="regen-hint"
              className="mb-1 block text-xs text-[var(--color-fg-muted)]"
            >
              What should change? <span>— optional hint</span>
            </label>
            <Input
              id="regen-hint"
              placeholder="e.g. the year is 2025, not the file's mtime"
              value={hint}
              onChange={(e) => setHint(e.target.value)}
            />
          </div>
          {error && <p className="text-xs text-[var(--color-danger)]">{error}</p>}
          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button variant="accent" size="sm" disabled={busy} onClick={submit}>
              {busy ? "Starting…" : "Regenerate"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function GeneratedConvertersPanel({ caseId }: Props) {
  const caps = useCapabilities();
  const [params] = useSearchParams();
  const linked = params.get("converter");
  const [expanded, setExpanded] = useState<string | null>(linked);
  // The job tray's "View converter attempts" link only changes the search
  // param while this panel stays mounted, so follow it — not just seed from it.
  useEffect(() => {
    if (linked) setExpanded(linked);
  }, [linked]);
  const [regen, setRegen] = useState<ConverterScript | null>(null);
  // Regenerations this panel started, by parent script id, until their job
  // reaches a terminal state: the Regenerate button is disabled for that
  // script (a second click would start a second job) and the list is polled
  // so the new "generating" row shows up while the job runs — the tray only
  // invalidates on completion, and the row does not exist at submit time.
  const [regenJobs, setRegenJobs] = useState<Record<string, string>>({});
  const jobs = useJobsStore((s) => s.jobs);
  const inFlight = useMemo(() => {
    const out = new Set<string>();
    for (const [scriptId, jobId] of Object.entries(regenJobs)) {
      const j = jobs[jobId];
      if (!j || (j.status !== "completed" && j.status !== "failed")) out.add(scriptId);
    }
    return out;
  }, [regenJobs, jobs]);
  useEffect(() => {
    // Forget finished jobs so the map cannot grow across a long session.
    const done = Object.keys(regenJobs).filter((id) => !inFlight.has(id));
    if (done.length) {
      setRegenJobs((m) => {
        const next = { ...m };
        for (const id of done) delete next[id];
        return next;
      });
    }
  }, [inFlight, regenJobs]);
  const { data } = useQuery({
    queryKey: ["converters", caseId],
    queryFn: () => convertersApi.listForCase(caseId),
    refetchInterval: inFlight.size > 0 ? 2000 : false,
  });
  const scripts = data?.scripts ?? [];
  if (!caps.converter_generation && scripts.length === 0) return null;

  return (
    <div className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-4 py-3">
      <h2 className="mb-1 flex items-center gap-2 text-sm font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wider">
        <Wand2 size={13} /> Generated converters
      </h2>
      <p className="mb-3 text-xs leading-relaxed text-[var(--color-fg-muted)]">
        Scripts the model wrote for this case. Download one to keep or edit it; regenerate when a
        format changed.
      </p>
      {scripts.length === 0 ? (
        <p className="text-xs text-[var(--color-fg-muted)]">
          None yet — choose “Let AI write the converter” in the upload dialog.
        </p>
      ) : (
        <ul className="space-y-1.5">
          {scripts.map((s) => {
            const isOpen = expanded === s.id;
            return (
              <li key={s.id} className="rounded border border-[var(--color-border)] px-2 py-1.5">
                <div className="flex items-start gap-2">
                  <Button
                    variant="ghost"
                    size="sm"
                    className="h-auto min-w-0 flex-1 flex-col items-start justify-start px-1 py-0.5 text-left font-normal"
                    aria-expanded={isOpen}
                    onClick={() => setExpanded(isOpen ? null : s.id)}
                  >
                    <div className="flex flex-wrap items-center gap-1.5">
                      <span className="font-mono text-xs">
                        {s.name} v{s.version}
                      </span>
                      <Badge variant={statusVariant(s.status)}>{s.status}</Badge>
                    </div>
                    <p className="mt-0.5 truncate text-xs text-[var(--color-fg-muted)]">
                      {s.model ?? "?"} · {s.sources_produced ?? 0} source
                      {(s.sources_produced ?? 0) === 1 ? "" : "s"} · {fmtTimestamp(s.created_at)}
                    </p>
                  </Button>
                  <div className="flex shrink-0 items-center gap-0.5">
                    <Button variant="ghost" size="icon" asChild title="Download script">
                      <a
                        href={convertersApi.caseDownloadUrl(caseId, s.id)}
                        download
                        rel="noopener noreferrer"
                      >
                        <Download size={13} />
                      </a>
                    </Button>
                    {caps.converter_generation && (
                      <Button
                        variant="ghost"
                        size="icon"
                        title={inFlight.has(s.id) ? "Regenerating…" : "Regenerate"}
                        aria-label={`Regenerate ${s.name}`}
                        disabled={inFlight.has(s.id) || s.status === "generating"}
                        onClick={() => setRegen(s)}
                      >
                        <RefreshCw size={13} className={inFlight.has(s.id) ? "animate-spin" : ""} />
                      </Button>
                    )}
                  </div>
                </div>
                {isOpen && (
                  <div className="mt-2 border-t border-[var(--color-border)] pt-2">
                    <ScriptDetail caseId={caseId} id={s.id} />
                  </div>
                )}
              </li>
            );
          })}
        </ul>
      )}
      {regen && (
        <RegenerateDialog
          caseId={caseId}
          script={regen}
          open
          onOpenChange={(o) => {
            if (!o) setRegen(null);
          }}
          onStarted={(jobId) => setRegenJobs((m) => ({ ...m, [regen.id]: jobId }))}
        />
      )}
    </div>
  );
}
