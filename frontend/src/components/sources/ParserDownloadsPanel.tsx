/**
 * Case-page download list for the offline converter scripts (issue #11):
 * "optimized" (*2tracesignal, compact Parquet output) vs. "other"
 * (*2timesketch, stdlib-only CSV/JSONL). Replaces the converter list that
 * used to live inside UploadDialog.
 */
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, Check, Clipboard, Wand2, Search, Zap, FileStack } from "lucide-react";
import { convertersApi } from "@/api/converters";
import { guidance } from "@/lib/guidance";
import { Button } from "@/components/ui/Button";
import { Input } from "@/components/ui/Input";
import { SegmentedControl } from "@/components/ui/SegmentedControl";
import { fmtBytes } from "@/lib/format";

function CopyPromptButton({ prompt, label }: { prompt: string; label: string }) {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(prompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      window.prompt("Copy the prompt below:", prompt);
    }
  };

  return (
    <Button variant="outline" size="sm" onClick={copy}>
      {copied ? <Check size={13} /> : <Clipboard size={13} />}
      {copied ? "Copied" : label}
    </Button>
  );
}

type Mode = "optimized" | "other";

const BLURB: Record<Mode, string> = {
  optimized:
    "*2tracesignal scripts emit a compact, typed Parquet file uploaded directly — " +
    "the server bulk-inserts it via Arrow, skipping row-by-row CSV/JSON parsing " +
    "entirely. Smaller on disk, faster to ingest, and carries forensic provenance " +
    "(source hash, byte offset, content hash) in the schema itself. Needs pyarrow.",
  other:
    "*2timesketch scripts are stdlib-only (no dependencies to install) and emit " +
    "Timesketch-compatible CSV/JSONL, vendored from the upstream 2timesketch project. " +
    "Larger output and slower to ingest than Parquet, but work anywhere Python runs.",
};

export function ParserDownloadsPanel() {
  const [mode, setMode] = useState<Mode>("optimized");
  const [search, setSearch] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["converters"],
    queryFn: convertersApi.list,
    staleTime: Infinity,
  });

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = search.trim().toLowerCase();
    return data.converters.filter((c) => {
      const isOptimized = !!c.native;
      if (mode === "optimized" ? !isOptimized : isOptimized) return false;
      if (!q) return true;
      return c.filename.toLowerCase().includes(q) || c.description.toLowerCase().includes(q);
    });
  }, [data, mode, search]);

  return (
    <div
      data-tour="converter-hint"
      className="rounded-lg border border-[var(--color-border)] bg-[var(--color-bg-surface)] px-4 py-3"
    >
      <h2 className="mb-3 text-sm font-semibold text-[var(--color-fg-secondary)] uppercase tracking-wider">
        Parser downloads
      </h2>

      <SegmentedControl
        value={mode}
        onChange={setMode}
        className="mb-2"
        options={[
          { id: "optimized", icon: Zap, label: "Optimized parsers" },
          { id: "other", icon: FileStack, label: "Other parsers" },
        ]}
      />
      <p className="mb-3 text-[11px] leading-relaxed text-[var(--color-fg-muted)]">
        {BLURB[mode]}
      </p>

      <div className="relative mb-2">
        <Search
          size={13}
          className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-[var(--color-fg-muted)]"
        />
        <Input
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          placeholder="Search parsers…"
          className="pl-7 text-xs"
        />
      </div>

      {isLoading && <p className="text-xs text-[var(--color-fg-muted)]">Loading converters…</p>}
      {error && <p className="text-xs text-[var(--color-danger)]">{(error as Error).message}</p>}

      {data && (
        <div className="max-h-64 space-y-1.5 overflow-y-auto pr-1">
          {filtered.length === 0 && (
            <p className="py-4 text-center text-[11px] text-[var(--color-fg-muted)]">
              No matching parsers.
            </p>
          )}
          {filtered.map((c) => (
            <div
              key={c.name}
              className="flex items-center gap-3 rounded border border-[var(--color-border)] bg-[var(--color-bg-base)] px-3 py-2"
            >
              <div className="min-w-0 flex-1">
                <p className="truncate text-xs font-medium text-[var(--color-fg-secondary)] font-mono">
                  {c.filename}
                </p>
                <p className="mt-0.5 text-[11px] leading-snug text-[var(--color-fg-muted)]">
                  {c.description} ({fmtBytes(c.size_bytes)})
                </p>
              </div>
              <Button variant="ghost" size="icon" asChild title={`Download ${c.filename}`}>
                <a href={convertersApi.downloadUrl(c.name)} download rel="noopener noreferrer">
                  <Download size={13} />
                </a>
              </Button>
            </div>
          ))}
        </div>
      )}

      <div className="mt-3 rounded border border-dashed border-[var(--color-border)] px-3 py-2.5">
        <div className="flex items-start gap-2">
          <Wand2 size={13} className="mt-0.5 shrink-0 text-[var(--color-fg-muted)] opacity-60" />
          <div className="flex-1 space-y-2">
            <p className="text-[11px] leading-relaxed text-[var(--color-fg-muted)]">
              {guidance.converters.hint}
            </p>
            {mode === "optimized" ? (
              <CopyPromptButton
                prompt={guidance.converters.llmPromptParquet}
                label="Copy LLM prompt (Parquet)"
              />
            ) : (
              <CopyPromptButton
                prompt={guidance.converters.llmPromptCsv}
                label="Copy LLM prompt (CSV/JSONL)"
              />
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
