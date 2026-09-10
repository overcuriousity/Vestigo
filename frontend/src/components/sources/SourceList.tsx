import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FileText, Database, Cpu, Download, Trash2, Clock } from "lucide-react";
import { sourcesApi } from "@/api/sources";
import { timelinesApi } from "@/api/timelines";
import { fmtRelative } from "@/lib/time";
import { fmtNum, fmtBytes, fmtParserName, truncateHash } from "@/lib/format";
import { Badge } from "@/components/ui/Badge";
import { Spinner } from "@/components/ui/Spinner";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import {
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "@/components/ui/Popover";
import { Dialog, DialogContent, DialogClose } from "@/components/ui/Dialog";
import { UploadDialog } from "@/components/timelines/UploadDialog";
import type { Source } from "@/api/types";

interface Props {
  caseId: string;
}

/** Query roots that all reflect a source's events; every one shifts when a
 *  clock-skew offset changes, so an offset edit must invalidate them all. */
const OFFSET_AFFECTED_KEYS = new Set([
  "sources",
  "events",
  "histogram",
  "field-histogram",
  "field-histogram-total",
  "field-terms",
  "anomalies",
  "frequency",
  "novelty",
  "range",
  "charset",
  "entropy",
  "combo",
  "order",
  "similar",
  "artifacts",
]);

/** Format a signed second offset compactly, e.g. `+1h`, `-2m 30s`, `+45s`. */
function fmtOffset(seconds: number): string {
  const sign = seconds < 0 ? "-" : "+";
  let s = Math.abs(seconds);
  const parts: string[] = [];
  const h = Math.floor(s / 3600);
  if (h) parts.push(`${h}h`);
  s -= h * 3600;
  const m = Math.floor(s / 60);
  if (m) parts.push(`${m}m`);
  s -= m * 60;
  if (s || parts.length === 0) parts.push(`${s}s`);
  return sign + parts.join(" ");
}

function SourceRow({ caseId, source }: { caseId: string; source: Source }) {
  return (
    <div className="group flex items-center gap-4 rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-5 py-3 hover:border-[var(--color-border-strong)] hover:bg-[var(--color-bg-elevated)] transition-base">
      <FileText size={16} className="shrink-0 text-[var(--color-accent)] opacity-70" />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="font-medium text-[var(--color-fg-primary)] truncate">
            {source.name}
          </span>
          {source.parser && (
            <Badge variant="muted">{fmtParserName(source.parser)}</Badge>
          )}
          {source.status !== "ready" && (
            <Badge variant="accent">
              <span className="flex items-center gap-1">
                <Spinner size={10} /> Ingesting
              </span>
            </Badge>
          )}
          {source.time_offset_seconds !== 0 && (
            <Badge variant="muted">
              <span
                className="flex items-center gap-1"
                title="Query-time clock-skew correction applied"
              >
                <Clock size={10} /> {fmtOffset(source.time_offset_seconds)}
              </span>
            </Badge>
          )}
        </div>
        <div className="mt-1 flex items-center gap-3 text-xs text-[var(--color-fg-muted)]">
          <span className="flex items-center gap-1">
            <Database size={11} /> {fmtNum(source.event_count)} events
          </span>
          {source.vector_count > 0 && (
            <span className="flex items-center gap-1">
              <Cpu size={11} /> {fmtNum(source.vector_count)} vectors
            </span>
          )}
          <span>{fmtBytes(source.size_bytes)}</span>
          <span className="font-mono" title={source.file_hash}>
            {truncateHash(source.file_hash, 12)}
          </span>
          <span>Updated {fmtRelative(source.updated_at)}</span>
        </div>
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <ClockOffsetControl caseId={caseId} source={source} />
        <Button variant="ghost" size="icon" asChild title="Download original file">
          <a href={sourcesApi.downloadUrl(caseId, source.id)} download>
            <Download size={14} />
          </a>
        </Button>
        <DeleteSourceButton caseId={caseId} source={source} />
      </div>
    </div>
  );
}

function ClockOffsetControl({
  caseId,
  source,
}: {
  caseId: string;
  source: Source;
}) {
  const qc = useQueryClient();
  const [open, setOpen] = useState(false);
  const [value, setValue] = useState(String(source.time_offset_seconds));

  const { mutate, isPending, error } = useMutation({
    mutationFn: (seconds: number) =>
      sourcesApi.update(caseId, source.id, seconds),
    onSuccess: () => {
      // Every timeline-scoped view (grid, histogram, detectors, similarity)
      // renders shifted timestamps now — refetch them all.
      qc.invalidateQueries({
        predicate: (q) =>
          typeof q.queryKey[0] === "string" &&
          OFFSET_AFFECTED_KEYS.has(q.queryKey[0]),
      });
      setOpen(false);
    },
  });

  const parsed = Number(value);
  const valid = Number.isFinite(parsed) && Number.isInteger(parsed);

  return (
    <Popover
      open={open}
      onOpenChange={(o) => {
        setOpen(o);
        if (o) setValue(String(source.time_offset_seconds));
      }}
    >
      <PopoverTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          title="Clock offset…"
          className={
            source.time_offset_seconds !== 0
              ? "text-[var(--color-accent)]"
              : undefined
          }
        >
          <Clock size={14} />
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-64 p-3">
        <p className="text-xs font-semibold text-[var(--color-fg-secondary)]">
          Clock-skew correction
        </p>
        <p className="mt-1 text-xs text-[var(--color-fg-muted)]">
          Shift this source's timestamps by N seconds at query time. Events are
          never modified. Use a negative value to move earlier.
        </p>
        <div className="mt-2 flex items-center gap-2">
          <Input
            type="number"
            step={1}
            value={value}
            onChange={(e) => setValue(e.target.value)}
            aria-label="Clock offset in seconds"
          />
          <span className="text-xs text-[var(--color-fg-muted)]">s</span>
        </div>
        {error && (
          <p className="mt-1 text-xs text-[var(--color-danger)]">
            {(error as Error).message}
          </p>
        )}
        <div className="mt-3 flex items-center justify-end gap-2">
          {source.time_offset_seconds !== 0 && (
            <Button
              variant="ghost"
              size="sm"
              disabled={isPending}
              onClick={() => mutate(0)}
            >
              Reset
            </Button>
          )}
          <Button
            size="sm"
            disabled={
              isPending ||
              !valid ||
              parsed === source.time_offset_seconds
            }
            onClick={() => mutate(parsed)}
          >
            {isPending ? "Saving…" : "Save"}
          </Button>
        </div>
      </PopoverContent>
    </Popover>
  );
}

/**
 * Delete a source, behind a confirmation that names what the delete rewrites.
 *
 * A source belongs to the default "All sources" timeline by definition, so
 * that one is never worth mentioning. An analyst-*created* timeline is a
 * different thing: it is a named grouping someone declared, and dropping a
 * source out of it silently invalidates the saved views, baseline windows and
 * findings built on that source set. The server refuses such a delete with 409
 * unless `force` is set (it is the enforcement — this dialog is not), so the
 * confirmation lists those timelines by name and the confirm button is what
 * sends `force`. The timelines query is already cached by the sidebar, so
 * naming them costs no extra request.
 */
function DeleteSourceButton({ caseId, source }: { caseId: string; source: Source }) {
  const [open, setOpen] = useState(false);
  const qc = useQueryClient();

  // Until this resolves we cannot honestly say which groupings the delete
  // rewrites, so the confirm button waits for it rather than sending a
  // `force` computed from an empty list (the server would 409 — correctly —
  // and the analyst would have to click twice for no reason).
  const { data: timelines, isPending: timelinesLoading } = useQuery({
    queryKey: ["timelines", caseId],
    queryFn: () => timelinesApi.list(caseId),
    enabled: open,
  });
  const affected = (timelines ?? []).filter(
    (t) => !t.is_default && t.source_ids.includes(source.id),
  );

  const { mutate, isPending, error, reset } = useMutation({
    // `force` is only ever true once the analyst has read the list above; a
    // first click on a source no timeline names never needs it.
    mutationFn: () => sourcesApi.delete(caseId, source.id, affected.length > 0),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ["sources", caseId] });
      qc.invalidateQueries({ queryKey: ["timelines", caseId] });
      setOpen(false);
    },
  });

  return (
    <Dialog
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        if (!next) reset();
      }}
    >
      <Button
        variant="ghost"
        size="icon"
        title="Delete source"
        onClick={() => setOpen(true)}
      >
        <Trash2 size={14} className="text-[var(--color-danger)]" />
      </Button>
      <DialogContent
        title={`Delete source "${source.name}"?`}
        description="Removes the source and every event and vector derived from it. The original uploaded file is not recoverable from Vestigo afterwards."
      >
        <div className="space-y-4">
          {affected.length > 0 && (
            <div className="rounded border border-[var(--color-danger)]/30 bg-[var(--color-danger-dim)] px-3 py-2 text-xs text-[var(--color-danger)] space-y-1">
              <p>
                This source is part of {affected.length}{" "}
                {affected.length === 1 ? "timeline" : "timelines"}. Deleting it
                removes the source from{" "}
                {affected.length === 1 ? "that grouping" : "those groupings"} and
                from every saved view, baseline window and finding declared over
                it:
              </p>
              <ul className="list-disc pl-4">
                {affected.map((t) => (
                  <li key={t.id} className="font-medium">
                    {t.name}
                  </li>
                ))}
              </ul>
            </div>
          )}
          {error && (
            <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>
          )}
          <div className="flex justify-end gap-2">
            <DialogClose asChild>
              <Button variant="ghost" size="sm">
                Cancel
              </Button>
            </DialogClose>
            <Button
              variant="danger"
              size="sm"
              disabled={isPending || timelinesLoading}
              onClick={() => mutate()}
            >
              {isPending ? "Deleting…" : "Delete Source"}
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}

export function SourceList({ caseId }: Props) {
  const { data: sources, isLoading, error } = useQuery({
    queryKey: ["sources", caseId],
    queryFn: () => sourcesApi.list(caseId),
    refetchInterval: 15_000,
  });

  return (
    <div>
      <div className="mb-3 flex items-center justify-between">
        <h2 className="text-sm font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wider">
          Sources
        </h2>
        <UploadDialog caseId={caseId} />
      </div>
      {isLoading && (
        <div className="flex justify-center py-8">
          <Spinner />
        </div>
      )}
      {error && (
        <p className="text-sm text-[var(--color-danger)]">
          {(error as Error).message}
        </p>
      )}
      {sources && sources.length === 0 && (
        <p className="py-8 text-center text-sm text-[var(--color-fg-muted)]">
          No sources yet. Upload a log file to get started.
        </p>
      )}
      {sources && (
        <div className="space-y-2">
          {sources.map((source) => (
            <SourceRow key={source.id} caseId={caseId} source={source} />
          ))}
        </div>
      )}
    </div>
  );
}
