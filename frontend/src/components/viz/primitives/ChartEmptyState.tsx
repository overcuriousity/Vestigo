interface ChartEmptyStateProps {
  /** "sm" matches compact charts (bar/pie), "md" the taller numeric/time charts. */
  size?: "sm" | "md";
  children: React.ReactNode;
}

/**
 * Placeholder shown instead of a chart when its guard decides there is
 * nothing to plot. The guard condition and message stay per-chart; only the
 * markup is shared. Both class strings are literal so Tailwind's scanner
 * sees them.
 */
export function ChartEmptyState({ size = "md", children }: ChartEmptyStateProps) {
  return (
    <div
      className={
        size === "sm"
          ? "flex h-[160px] items-center justify-center text-sm text-[var(--color-fg-muted)]"
          : "flex h-[220px] items-center justify-center text-sm text-[var(--color-fg-muted)]"
      }
    >
      {children}
    </div>
  );
}
