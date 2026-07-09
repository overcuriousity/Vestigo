import type { ComponentType } from "react";
import { cn } from "@/lib/cn";
import { InfoHint } from "@/components/ui/InfoHint";

export interface SegmentedOption<T extends string> {
  id: T;
  label: string;
  icon?: ComponentType<{ size?: number }>;
  hint?: string;
}

interface Props<T extends string> {
  value: T;
  onChange: (id: T) => void;
  options: SegmentedOption<T>[];
  className?: string;
}

/** Two-or-more-way pill toggle, extracted from FrameBar's scan/baseline switch. */
export function SegmentedControl<T extends string>({
  value,
  onChange,
  options,
  className,
}: Props<T>) {
  return (
    <div className={cn("flex items-center gap-1", className)}>
      {options.map(({ id, label, icon: Icon, hint }) => (
        <div key={id} className="flex flex-1 items-center gap-1">
          <button
            onClick={() => onChange(id)}
            className={cn(
              "flex flex-1 items-center justify-center gap-1.5 rounded px-2 py-1.5 text-xs font-medium transition-colors",
              value === id
                ? "bg-[var(--color-accent)] text-white"
                : "bg-[var(--color-bg-elevated)] text-[var(--color-fg-muted)] hover:text-[var(--color-fg-secondary)]",
            )}
          >
            {Icon && <Icon size={12} />}
            {label}
          </button>
          {hint && <InfoHint content={hint} />}
        </div>
      ))}
    </div>
  );
}
