/**
 * On-screen rendering of the chart caption — the exact same lines
 * `buildCaptionLines` feeds into exports, so screen and report never drift.
 * The first line (the TraceVector/case header) is export boilerplate and
 * skipped on screen, where the app chrome already says where you are.
 */
export function ChartCaption({ lines }: { lines: string[] }) {
  const visible = lines.slice(1);
  if (visible.length === 0) return null;
  return (
    <div className="mt-3 space-y-0.5 border-t border-[var(--color-border)] pt-2 text-xs text-[var(--color-fg-muted)]">
      {visible.map((line, i) => (
        <div key={i}>{line}</div>
      ))}
    </div>
  );
}
