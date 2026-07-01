import { forwardRef } from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { cn } from "@/lib/cn";

const buttonVariants = cva(
  "inline-flex items-center justify-center gap-1.5 whitespace-nowrap rounded text-sm font-medium transition-base cursor-pointer disabled:pointer-events-none disabled:opacity-40 focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-[var(--color-accent)]",
  {
    variants: {
      variant: {
        default:
          "bg-[var(--color-bg-active)] text-[var(--color-fg-primary)] border border-[var(--color-border-strong)] hover:bg-[var(--color-bg-hover)] hover:border-[var(--color-border-strong)]",
        accent:
          "bg-[var(--color-accent)] text-[var(--color-accent-fg)] font-semibold hover:opacity-90",
        ghost:
          "text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-primary)]",
        danger:
          "bg-[var(--color-danger-dim)] text-[var(--color-danger)] border border-[var(--color-danger)]/30 hover:bg-[var(--color-danger)] hover:text-white",
        outline:
          "border border-[var(--color-border-strong)] text-[var(--color-fg-secondary)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-primary)]",
      },
      size: {
        sm: "h-8 px-3 text-xs",
        md: "h-9 px-3.5 text-sm",
        lg: "h-10 px-4",
        icon: "h-8 w-8 p-0",
      },
    },
    defaultVariants: {
      variant: "default",
      size: "md",
    },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        className={cn(buttonVariants({ variant, size, className }))}
        ref={ref}
        {...props}
      />
    );
  },
);
Button.displayName = "Button";
