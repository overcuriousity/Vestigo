/**
 * Shared blob-download helper.
 *
 * Single implementation for every "save this blob as a file" path (event
 * exports, chart SVG/PNG exports) so browser quirks are fixed in one place:
 * the anchor is attached to the DOM before `click()` (required by
 * Firefox/Safari) and the filename is sanitized — field values in a forensic
 * tool are frequently file paths (`/var/log/audit/audit.log`, `C:\...`), and
 * an unsanitized `a.download` silently lands in a subdirectory or fails
 * outright on Windows.
 */

/** Replace filesystem-illegal characters (path separators, `:*?"<>|`,
 * control chars) with `_`, collapse runs, and trim leading/trailing
 * dots/spaces/underscores. Falls back to "download" if nothing survives. */
export function sanitizeFilename(name: string): string {
  const cleaned = name
    // eslint-disable-next-line no-control-regex
    .replace(/[/\\:*?"<>|\u0000-\u001f]+/g, "_")
    .replace(/_{2,}/g, "_")
    .replace(/^[_. ]+|[_. ]+$/g, "");
  return cleaned || "download";
}

/** Download `blob` as `filename` (sanitized) via a temporary object URL. */
export function triggerDownload(blob: Blob, filename: string): void {
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = sanitizeFilename(filename);
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
  URL.revokeObjectURL(url);
}
