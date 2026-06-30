import { cn } from "@/lib/cn";

type BadgeVariant = "default" | "accent" | "success" | "danger" | "anomaly" | "muted";

interface BadgeProps {
  children: React.ReactNode;
  variant?: BadgeVariant;
  className?: string;
}

const variantClasses: Record<BadgeVariant, string> = {
  default:
    "bg-[var(--color-bg-active)] text-[var(--color-fg-secondary)] border border-[var(--color-border-strong)]",
  accent:
    "bg-[var(--color-accent-dim)] text-[var(--color-accent)] border border-[var(--color-accent-muted)]",
  success:
    "bg-[var(--color-success-dim)] text-[var(--color-success)] border border-[var(--color-success)]/30",
  danger:
    "bg-[var(--color-danger-dim)] text-[var(--color-danger)] border border-[var(--color-danger)]/30",
  anomaly:
    "bg-[var(--color-anomaly-dim)] text-[var(--color-anomaly)] border border-[var(--color-anomaly)]/30",
  muted: "bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)]",
};

export function Badge({ children, variant = "default", className }: BadgeProps) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded px-1.5 py-0.5 text-xs font-mono leading-none",
        variantClasses[variant],
        className,
      )}
    >
      {children}
    </span>
  );
}
