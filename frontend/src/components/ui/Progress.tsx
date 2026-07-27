import * as RadixProgress from "@radix-ui/react-progress";
import { cn } from "@/lib/cn";

interface ProgressProps {
  /** 0–100, or null for indeterminate — work is happening but its size is
   * unknown (a chunked download, a phase that hasn't counted its items). */
  value: number | null;
  className?: string;
  trackClassName?: string;
  indicatorClassName?: string;
}

export function Progress({
  value,
  className,
  trackClassName,
  indicatorClassName,
}: ProgressProps) {
  const indeterminate = value == null;
  return (
    <RadixProgress.Root
      className={cn(
        "relative h-1.5 w-full overflow-hidden rounded-full bg-[var(--color-bg-active)]",
        className,
        trackClassName,
      )}
      // Radix reads `null` as indeterminate and drops `aria-valuenow`, which
      // is exactly right: a screen reader should hear "busy", not "0 percent".
      value={value}
    >
      <RadixProgress.Indicator
        className={cn(
          "h-full bg-[var(--color-accent)]",
          indeterminate ? "progress-indeterminate" : "transition-all duration-300 ease-out",
          indicatorClassName,
        )}
        style={indeterminate ? undefined : { transform: `translateX(-${100 - value}%)` }}
      />
    </RadixProgress.Root>
  );
}
