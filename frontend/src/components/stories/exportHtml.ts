/**
 * Render an export snapshot to a standalone HTML document.
 *
 * Self-contained by construction: the markup comes from `SnapshotRenderer`
 * (which never fetches) and the styling is the app's own compiled CSS,
 * serialized out of the live stylesheets and inlined. The file opens with no
 * network access at all — which is the point in an airgapped deployment, and
 * what makes it archivable as the report itself.
 *
 * The snapshot JSON stays the authoritative record: this document is
 * presentation, hashed and stored alongside it.
 */
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import type { StorySnapshot } from "@/api/types";
import { SnapshotRenderer } from "./SnapshotRenderer";

/** Serialize every same-origin stylesheet the app has loaded. */
function collectStyles(): string {
  const chunks: string[] = [];
  for (const sheet of Array.from(document.styleSheets)) {
    try {
      for (const rule of Array.from(sheet.cssRules)) chunks.push(rule.cssText);
    } catch {
      // A cross-origin sheet can't be read; skip it rather than fail the
      // export — the app's own styles are same-origin.
    }
  }
  return chunks.join("\n");
}

/**
 * Render a snapshot to standalone HTML.
 *
 * `snapshotHash` is required and appears in both a `<meta>` tag and the
 * visible footer: it is the only thing binding this document to the record it
 * claims to render. Without it the server would hash and store whatever
 * markup it was handed, and `html_hash` would be presented with the same
 * authority as `snapshot_hash` while attesting to nothing — so the seal
 * endpoint refuses an artifact that does not carry it.
 */
export function renderExportHtml(snapshot: StorySnapshot, snapshotHash: string): string {
  const body = renderToStaticMarkup(createElement(SnapshotRenderer, { snapshot }));
  const css = collectStyles();
  const title = `${snapshot.story.title} — Vestigo story export`;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="vestigo:snapshot-hash" content="${escapeHtml(snapshotHash)}">
<title>${escapeHtml(title)}</title>
<style>${css}</style>
</head>
<body>
${body}
<footer style="max-width:56rem;margin:0 auto;padding:1.5rem;font-size:11px;opacity:.7">
${escapeHtml(snapshot.story.title)} · exported ${escapeHtml(snapshot.story.exported_at)} by
${escapeHtml(snapshot.story.exported_by)} · snapshot is the authoritative record<br>
snapshot SHA-256 <code>${escapeHtml(snapshotHash)}</code> — verify against the exported JSON
</footer>
</body>
</html>`;
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}
