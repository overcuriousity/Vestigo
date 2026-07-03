import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { Download, Check, Clipboard, Wand2 } from "lucide-react";
import { convertersApi } from "@/api/converters";
import { guidance } from "@/lib/guidance";
import { Button } from "@/components/ui/Button";
import { fmtBytes } from "@/lib/format";

function CopyPromptButton() {
  const [copied, setCopied] = useState(false);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(guidance.converters.llmPrompt);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API unavailable — fall back to a prompt-sized selection.
      window.prompt("Copy the prompt below:", guidance.converters.llmPrompt);
    }
  };

  return (
    <Button variant="outline" size="sm" onClick={copy}>
      {copied ? <Check size={13} /> : <Clipboard size={13} />}
      {copied ? "Copied" : "Copy LLM prompt"}
    </Button>
  );
}

/**
 * Offline download list of the vendored 2timesketch normalization scripts,
 * plus a static LLM prompt for formats not covered (issue #11).
 */
export function ConverterPanel() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["converters"],
    queryFn: convertersApi.list,
    staleTime: Infinity,
  });

  return (
    <div className="space-y-3">
      {isLoading && (
        <p className="text-xs text-[var(--color-fg-muted)]">Loading converters…</p>
      )}
      {error && (
        <p className="text-xs text-[var(--color-danger)]">
          {(error as Error).message}
        </p>
      )}
      {data && (
        <div className="max-h-64 space-y-1.5 overflow-y-auto pr-1">
          {data.converters.map((c) => (
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
                <a href={convertersApi.downloadUrl(c.name)} download>
                  <Download size={13} />
                </a>
              </Button>
            </div>
          ))}
          <p className="text-[11px] text-[var(--color-fg-muted)]">
            Stdlib-only Python, run offline: <span className="font-mono">python3 script.py -i input -o timeline.csv</span>.
            Vendored from{" "}
            <span className="font-mono">{data.upstream.replace("https://github.com/", "")}</span>{" "}
            @ <span className="font-mono">{data.commit.slice(0, 12)}</span>.
          </p>
        </div>
      )}

      <div className="rounded border border-dashed border-[var(--color-border)] px-3 py-2.5">
        <div className="flex items-start gap-2">
          <Wand2 size={13} className="mt-0.5 shrink-0 text-[var(--color-fg-muted)] opacity-60" />
          <div className="flex-1 space-y-2">
            <p className="text-[11px] leading-relaxed text-[var(--color-fg-muted)]">
              {guidance.converters.hint}
            </p>
            <CopyPromptButton />
          </div>
        </div>
      </div>
    </div>
  );
}
