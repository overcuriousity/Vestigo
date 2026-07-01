import { forwardRef } from "react";
import { cn } from "@/lib/cn";

export const Input = forwardRef<
  HTMLInputElement,
  React.InputHTMLAttributes<HTMLInputElement>
>(({ className, ...props }, ref) => (
  <input
    ref={ref}
    className={cn(
      "h-9 w-full rounded border border-[var(--color-border-strong)] bg-[var(--color-bg-elevated)] px-3 text-sm text-[var(--color-fg-primary)] placeholder:text-[var(--color-fg-muted)] transition-base focus:border-[var(--color-accent)] focus:outline-none disabled:opacity-40",
      className,
    )}
    {...props}
  />
));
Input.displayName = "Input";
