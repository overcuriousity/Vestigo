import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { Copy, Download, FileDown } from "lucide-react";
import { BASE } from "@/api/client";
import { storiesApi } from "@/api/stories";
import { Button } from "@/components/ui/Button";
import { Spinner } from "@/components/ui/Spinner";
import { triggerDownload } from "@/lib/download";
import { fmtTimestamp } from "@/lib/time";
import { toast } from "@/stores/toasts";
import { renderExportHtml } from "./exportHtml";

interface Props {
  caseId: string;
  storyId: string;
}

/**
 * Size at which the rendered artifact is worth warning about, well under the
 * server's `VESTIGO_STORY_MAX_ARTIFACT_BYTES` (20 MB by default) so the
 * warning arrives before the rejection does.
 */
const ARTIFACT_WARN_BYTES = 15 * 1024 * 1024;

/**
 * Export controls and the log of past exports.
 *
 * Two phases, matching the API: the server resolves and hashes the snapshot
 * (authoritative), then the browser renders that snapshot to standalone HTML
 * and uploads it once (presentation). An export is complete and usable even
 * if the artifact upload never happens.
 */
export function ExportsTab({ caseId, storyId }: Props) {
  const qc = useQueryClient();
  const [busy, setBusy] = useState(false);

  const { data: exports, isLoading } = useQuery({
    queryKey: ["story-exports", caseId, storyId],
    queryFn: () => storiesApi.listExports(caseId, storyId),
  });

  const runExport = useMutation({
    mutationFn: async () => {
      setBusy(true);
      const created = await storiesApi.createExport(caseId, storyId);
      const html = renderExportHtml(created.snapshot, created.snapshot_hash);
      // The artifact inlines the whole compiled stylesheet plus every frozen
      // row, so a large story can exceed the server cap. Say so up front
      // rather than letting the analyst discover a 413 in a toast — the
      // snapshot itself is already stored and usable either way.
      if (html.length > ARTIFACT_WARN_BYTES) {
        toast.info(
          "Large HTML artifact",
          `${(html.length / 1024 / 1024).toFixed(1)} MB — the upload may be rejected; the snapshot JSON is unaffected.`,
        );
      }
      try {
        await storiesApi.uploadArtifact(caseId, storyId, created.id, html);
      } catch (err) {
        // The snapshot is already stored and hashed; a failed artifact upload
        // degrades the export to JSON-only rather than losing it.
        toast.error("Snapshot stored, but the HTML artifact upload failed", (err as Error).message);
      }
      return { created, html };
    },
    onSuccess: ({ created, html }) => {
      qc.invalidateQueries({ queryKey: ["story-exports", caseId, storyId] });
      triggerDownload(
        new Blob([html], { type: "text/html" }),
        `story-${storyId}-${created.id}.html`,
      );
      toast.success("Export created", `snapshot ${created.snapshot_hash.slice(0, 12)}…`);
    },
    onError: (err) => toast.error("Export failed", (err as Error).message),
    onSettled: () => setBusy(false),
  });

  const base = `${BASE}/cases/${caseId}/stories/${storyId}/exports`;

  return (
    <div className="space-y-4">
      <div className="flex items-start gap-3">
        <Button variant="accent" size="sm" disabled={busy} onClick={() => runExport.mutate()}>
          {busy ? <Spinner size={12} /> : <FileDown size={13} />} Export snapshot
        </Button>
        <p className="text-xs text-[var(--color-fg-muted)]">
          Freezes every embed's data as it stands right now, hashes it, and records who
          exported it. Past exports never change.
        </p>
      </div>

      {isLoading && <Spinner size={16} />}
      {exports && exports.length === 0 && (
        <p className="py-6 text-center text-sm text-[var(--color-fg-muted)]">
          No exports yet.
        </p>
      )}
      {exports && exports.length > 0 && (
        <div className="overflow-x-auto rounded border border-[var(--color-border)]">
          <table className="w-full text-xs">
            <thead className="bg-[var(--color-bg-elevated)] text-left text-[var(--color-fg-muted)]">
              <tr>
                <th className="px-3 py-1.5 font-medium">Exported</th>
                <th className="px-3 py-1.5 font-medium">By</th>
                <th className="px-3 py-1.5 font-medium">Snapshot SHA-256</th>
                <th className="px-3 py-1.5 font-medium">Downloads</th>
              </tr>
            </thead>
            <tbody>
              {exports.map((exp) => (
                <tr key={exp.id} className="border-t border-[var(--color-border)]/60">
                  <td className="whitespace-nowrap px-3 py-1.5">
                    {fmtTimestamp(exp.created_at)}
                  </td>
                  <td className="px-3 py-1.5">{exp.created_by}</td>
                  <td className="px-3 py-1.5">
                    <span className="flex items-center gap-1 font-mono text-[10px]">
                      {exp.snapshot_hash.slice(0, 16)}…
                      <button
                        type="button"
                        aria-label="Copy snapshot hash"
                        className="rounded p-0.5 hover:bg-[var(--color-bg-hover)]"
                        onClick={() => {
                          navigator.clipboard?.writeText(exp.snapshot_hash);
                          toast.success("Hash copied");
                        }}
                      >
                        <Copy size={10} />
                      </button>
                    </span>
                  </td>
                  <td className="px-3 py-1.5">
                    <div className="flex items-center gap-3">
                      <a
                        href={`${base}/${exp.id}/snapshot`}
                        download={`story-${storyId}-${exp.id}.json`}
                        className="flex items-center gap-1 text-[var(--color-accent)] hover:underline"
                      >
                        <Download size={10} /> JSON
                      </a>
                      {exp.has_artifact ? (
                        <a
                          href={`${base}/${exp.id}/artifact`}
                          download={`story-${storyId}-${exp.id}.html`}
                          className="flex items-center gap-1 text-[var(--color-accent)] hover:underline"
                        >
                          <Download size={10} /> HTML
                        </a>
                      ) : (
                        <span className="text-[var(--color-fg-muted)]">no artifact</span>
                      )}
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
