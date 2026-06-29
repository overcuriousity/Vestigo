/**
 * Multi-step embedding wizard.
 *
 * Step 1 — Source selection: list each source with event count; toggle which to embed.
 * Step 2 — Field selection: for each included source, choose fields (top-level + attrs).
 * Step 3 — Review & run: summary + confirm; posts config to the embed endpoint.
 */
import { useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";
import { Cpu, ChevronLeft, ChevronRight, Check, Info } from "lucide-react";
import { eventsApi } from "@/api/events";
import { timelinesApi } from "@/api/timelines";
import { useJobsStore } from "@/stores/jobs";
import { Dialog, DialogContent, DialogTrigger, DialogClose } from "@/components/ui/Dialog";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import type { EmbeddingFieldConfig, EmbeddingSourceInfo, Timeline } from "@/api/types";

interface Props {
  caseId: string;
  timelineId: string;
  timeline: Timeline;
  /** Called after the embed job is started so parent can update. */
  onJobStarted?: (jobId: string) => void;
}

type Step = "sources" | "fields" | "review";

/** Display name for a field token */
function tokenLabel(token: string): string {
  if (token.startsWith("attr:")) return token.slice(5);
  return token;
}

/** Friendly label for top-level column names */
const TOP_LEVEL_LABELS: Record<string, string> = {
  message: "Message",
  timestamp_desc: "Timestamp description",
  source_long: "Source (long)",
  display_name: "Display name",
  tags: "Parser tags",
};

export function EmbedWizard({ caseId, timelineId, timeline, onJobStarted }: Props) {
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState<Step>("sources");

  // Wizard state: which sources to embed and which fields per source
  const [includedSources, setIncludedSources] = useState<Set<string>>(new Set());
  const [fieldsBySource, setFieldsBySource] = useState<Record<string, Set<string>>>({});

  const addJob = useJobsStore((s) => s.addJob);
  const label = (timeline.vector_count ?? 0) > 0 ? "Re-embed" : "Embed";

  // Fetch per-source field info
  const { data: fieldsData, isLoading: isLoadingFields } = useQuery({
    queryKey: ["embedding-fields", caseId, timelineId],
    queryFn: () => eventsApi.embeddingFields(caseId, timelineId),
    enabled: open,
  });

  // Initialize wizard state once data is loaded
  const initWizard = () => {
    if (!fieldsData) return;
    const included = new Set(fieldsData.sources.map((s) => s.source));
    setIncludedSources(included);
    const fields: Record<string, Set<string>> = {};
    for (const src of fieldsData.sources) {
      fields[src.source] = new Set(src.recommended);
    }
    // Pre-populate from existing config if re-embedding
    if (timeline.embedding_config?.sources) {
      const cfg = timeline.embedding_config.sources;
      const cfgIncluded = new Set(Object.keys(cfg));
      setIncludedSources(cfgIncluded);
      for (const [src, tokens] of Object.entries(cfg)) {
        fields[src] = new Set(tokens);
      }
    }
    setFieldsBySource(fields);
  };

  const handleOpen = (v: boolean) => {
    setOpen(v);
    if (v) {
      setStep("sources");
      // Delay init until data is available
    }
  };

  // Once data arrives, initialize (idempotent if already done)
  if (open && fieldsData && Object.keys(fieldsBySource).length === 0) {
    initWizard();
  }

  const embedMutation = useMutation({
    mutationFn: (config: EmbeddingFieldConfig) =>
      timelinesApi.embed(caseId, timelineId, config),
    onSuccess: (result) => {
      addJob(result.job_id, `Embedding "${timeline.name}"`);
      onJobStarted?.(result.job_id);
      setOpen(false);
    },
  });

  const handleRun = () => {
    const sources: Record<string, string[]> = {};
    for (const src of includedSources) {
      sources[src] = Array.from(fieldsBySource[src] ?? []);
    }
    const config: EmbeddingFieldConfig = { version: 1, sources };
    embedMutation.mutate(config);
  };

  const toggleSource = (source: string) => {
    setIncludedSources((prev) => {
      const next = new Set(prev);
      if (next.has(source)) next.delete(source);
      else next.add(source);
      return next;
    });
  };

  const toggleField = (source: string, token: string) => {
    setFieldsBySource((prev) => {
      const srcFields = new Set(prev[source] ?? []);
      if (srcFields.has(token)) srcFields.delete(token);
      else srcFields.add(token);
      return { ...prev, [source]: srcFields };
    });
  };

  const toggleAllFields = (src: EmbeddingSourceInfo) => {
    const allTokens = [
      ...src.top_level,
      ...src.attributes.map((a) => `attr:${a}`),
    ];
    setFieldsBySource((prev) => {
      const current = prev[src.source] ?? new Set<string>();
      const allSelected = allTokens.every((t) => current.has(t));
      return {
        ...prev,
        [src.source]: allSelected ? new Set<string>() : new Set(allTokens),
      };
    });
  };

  const includedSourcesList = fieldsData?.sources.filter((s) =>
    includedSources.has(s.source),
  ) ?? [];

  const totalFieldCount = includedSourcesList.reduce(
    (n, s) => n + (fieldsBySource[s.source]?.size ?? 0),
    0,
  );

  return (
    <Dialog open={open} onOpenChange={handleOpen}>
      <DialogTrigger asChild>
        <Button variant="outline" size="sm">
          <Cpu size={13} />
          {label}
        </Button>
      </DialogTrigger>
      <DialogContent
        title="Embedding Wizard"
        description="Choose which sources and fields to embed. Each combination is stored as a separate vector collection."
        className="max-w-2xl"
      >
        {isLoadingFields ? (
          <div className="flex justify-center py-10">
            <Spinner />
          </div>
        ) : (
          <div className="space-y-4">
            {/* Step indicator */}
            <div className="flex items-center gap-2 text-xs text-[var(--color-fg-muted)]">
              {(["sources", "fields", "review"] as Step[]).map((s, i) => (
                <span key={s} className="flex items-center gap-1">
                  {i > 0 && <span className="opacity-40">›</span>}
                  <span
                    className={
                      step === s
                        ? "font-semibold text-[var(--color-fg-primary)]"
                        : "opacity-50"
                    }
                  >
                    {i + 1}.{" "}
                    {s === "sources" ? "Sources" : s === "fields" ? "Fields" : "Review"}
                  </span>
                </span>
              ))}
            </div>

            {/* Step 1: Source selection */}
            {step === "sources" && (
              <div className="space-y-3">
                <p className="text-xs text-[var(--color-fg-muted)]">
                  Select which sources to embed. Unselected sources are skipped entirely.
                </p>
                {!fieldsData?.sources.length && (
                  <p className="text-xs text-[var(--color-fg-muted)] italic">
                    No events found in this timeline yet.
                  </p>
                )}
                <div className="max-h-72 overflow-y-auto space-y-1.5 pr-1">
                  {fieldsData?.sources.map((src) => (
                    <label
                      key={src.source}
                      className="flex cursor-pointer items-center gap-3 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2.5 hover:border-[var(--color-accent)]/40 transition-base"
                    >
                      <input
                        type="checkbox"
                        checked={includedSources.has(src.source)}
                        onChange={() => toggleSource(src.source)}
                        className="accent-[var(--color-accent)]"
                      />
                      <div className="flex-1 min-w-0">
                        <span className="text-sm font-mono text-[var(--color-fg-primary)] truncate">
                          {src.source || <em className="opacity-50">unknown</em>}
                        </span>
                        <span className="ml-2 text-xs text-[var(--color-fg-muted)]">
                          {src.count.toLocaleString()} events
                        </span>
                        {src.attributes.length > 0 && (
                          <span className="ml-2 text-xs text-[var(--color-fg-muted)] opacity-70">
                            · {src.attributes.length} attribute{src.attributes.length !== 1 ? "s" : ""}
                          </span>
                        )}
                      </div>
                    </label>
                  ))}
                </div>
                <div className="flex justify-between items-center pt-1">
                  <span className="text-xs text-[var(--color-fg-muted)]">
                    {includedSources.size} of {fieldsData?.sources.length ?? 0} selected
                  </span>
                  <div className="flex gap-2">
                    <DialogClose asChild>
                      <Button variant="ghost" size="sm">Cancel</Button>
                    </DialogClose>
                    <Button
                      variant="accent"
                      size="sm"
                      disabled={includedSources.size === 0}
                      onClick={() => setStep("fields")}
                    >
                      Next <ChevronRight size={13} />
                    </Button>
                  </div>
                </div>
              </div>
            )}

            {/* Step 2: Field selection per source */}
            {step === "fields" && (
              <div className="space-y-3">
                <p className="text-xs text-[var(--color-fg-muted)]">
                  Choose which fields to include in the embedding text for each source.
                  Recommended fields are pre-selected.
                </p>
                <div className="max-h-80 overflow-y-auto space-y-4 pr-1">
                  {includedSourcesList.map((src) => {
                    const selected = fieldsBySource[src.source] ?? new Set<string>();
                    const allTokens = [
                      ...src.top_level,
                      ...src.attributes.map((a) => `attr:${a}`),
                    ];
                    const allSelected = allTokens.every((t) => selected.has(t));
                    return (
                      <div key={src.source} className="rounded border border-[var(--color-border)] p-3 space-y-2">
                        <div className="flex items-center justify-between gap-2">
                          <span className="text-xs font-semibold font-mono text-[var(--color-fg-primary)]">
                            {src.source || "(unknown source)"}
                          </span>
                          <button
                            className="text-xs text-[var(--color-fg-muted)] hover:text-[var(--color-fg-primary)] underline transition-base"
                            onClick={() => toggleAllFields(src)}
                          >
                            {allSelected ? "Deselect all" : "Select all"}
                          </button>
                        </div>
                        <div className="space-y-1">
                          <p className="text-xs text-[var(--color-fg-muted)] font-medium uppercase tracking-wide opacity-60">
                            Standard fields
                          </p>
                          <div className="grid grid-cols-2 gap-1">
                            {src.top_level.map((f) => (
                              <label key={f} className="flex items-center gap-2 text-xs cursor-pointer">
                                <input
                                  type="checkbox"
                                  checked={selected.has(f)}
                                  onChange={() => toggleField(src.source, f)}
                                  className="accent-[var(--color-accent)]"
                                />
                                <span className={f === "message" ? "font-semibold" : ""}>
                                  {TOP_LEVEL_LABELS[f] ?? f}
                                </span>
                              </label>
                            ))}
                          </div>
                        </div>
                        {src.attributes.length > 0 && (
                          <div className="space-y-1">
                            <p className="text-xs text-[var(--color-fg-muted)] font-medium uppercase tracking-wide opacity-60">
                              Attributes
                            </p>
                            <div className="grid grid-cols-2 gap-1">
                              {src.attributes.map((a) => {
                                const token = `attr:${a}`;
                                return (
                                  <label key={token} className="flex items-center gap-2 text-xs cursor-pointer font-mono">
                                    <input
                                      type="checkbox"
                                      checked={selected.has(token)}
                                      onChange={() => toggleField(src.source, token)}
                                      className="accent-[var(--color-accent)]"
                                    />
                                    {a}
                                  </label>
                                );
                              })}
                            </div>
                          </div>
                        )}
                        {selected.size === 0 && (
                          <p className="text-xs text-[var(--color-warning)] flex items-center gap-1">
                            <Info size={11} /> No fields selected — this source will produce empty embeddings.
                          </p>
                        )}
                      </div>
                    );
                  })}
                </div>
                <div className="flex justify-between pt-1">
                  <Button variant="ghost" size="sm" onClick={() => setStep("sources")}>
                    <ChevronLeft size={13} /> Back
                  </Button>
                  <div className="flex gap-2">
                    <DialogClose asChild>
                      <Button variant="ghost" size="sm">Cancel</Button>
                    </DialogClose>
                    <Button
                      variant="accent"
                      size="sm"
                      disabled={totalFieldCount === 0}
                      onClick={() => setStep("review")}
                    >
                      Review <ChevronRight size={13} />
                    </Button>
                  </div>
                </div>
              </div>
            )}

            {/* Step 3: Review & Run */}
            {step === "review" && (
              <div className="space-y-3">
                <p className="text-xs text-[var(--color-fg-muted)]">
                  Review your configuration before running. This will start a background job.
                </p>
                <div className="rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] p-3 space-y-3 max-h-72 overflow-y-auto">
                  {includedSourcesList.map((src) => {
                    const selected = Array.from(fieldsBySource[src.source] ?? []);
                    return (
                      <div key={src.source}>
                        <p className="text-xs font-semibold font-mono text-[var(--color-fg-primary)] mb-1">
                          {src.source || "(unknown)"}{" "}
                          <span className="text-[var(--color-fg-muted)] font-normal">
                            — {src.count.toLocaleString()} events
                          </span>
                        </p>
                        <div className="flex flex-wrap gap-1">
                          {selected.map((t) => (
                            <span
                              key={t}
                              className="inline-flex items-center gap-1 rounded bg-[var(--color-accent-dim)] px-1.5 py-0.5 text-xs text-[var(--color-accent)] font-mono"
                            >
                              <Check size={9} /> {tokenLabel(t)}
                            </span>
                          ))}
                          {selected.length === 0 && (
                            <span className="text-xs text-[var(--color-warning)]">
                              No fields selected
                            </span>
                          )}
                        </div>
                      </div>
                    );
                  })}
                  {fieldsData?.sources
                    .filter((s) => !includedSources.has(s.source))
                    .map((src) => (
                      <div key={src.source} className="opacity-40">
                        <p className="text-xs font-mono line-through text-[var(--color-fg-muted)]">
                          {src.source || "(unknown)"} — skipped
                        </p>
                      </div>
                    ))}
                </div>
                <div className="rounded border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] px-3 py-2 text-xs text-[var(--color-fg-muted)] space-y-0.5">
                  <p><span className="font-medium text-[var(--color-fg-secondary)]">Model:</span> {timeline.embedding_model ?? "all-MiniLM-L6-v2"}</p>
                  <p><span className="font-medium text-[var(--color-fg-secondary)]">Sources:</span> {includedSources.size} embedded, {(fieldsData?.sources.length ?? 0) - includedSources.size} skipped</p>
                  <p><span className="font-medium text-[var(--color-fg-secondary)]">Total fields:</span> {totalFieldCount}</p>
                  <p className="opacity-60 pt-0.5">
                    A different field selection creates a new vector collection (new config hash). Old collections are retained.
                  </p>
                </div>
                {embedMutation.error && (
                  <p className="text-xs text-[var(--color-danger)]">
                    {(embedMutation.error as Error).message}
                  </p>
                )}
                <div className="flex justify-between pt-1">
                  <Button variant="ghost" size="sm" onClick={() => setStep("fields")}>
                    <ChevronLeft size={13} /> Back
                  </Button>
                  <div className="flex gap-2">
                    <DialogClose asChild>
                      <Button variant="ghost" size="sm">Cancel</Button>
                    </DialogClose>
                    <Button
                      variant="accent"
                      size="sm"
                      disabled={embedMutation.isPending || totalFieldCount === 0}
                      onClick={handleRun}
                    >
                      {embedMutation.isPending ? <Spinner size={13} /> : <Cpu size={13} />}
                      {embedMutation.isPending ? "Starting…" : "Run Embedding"}
                    </Button>
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}
