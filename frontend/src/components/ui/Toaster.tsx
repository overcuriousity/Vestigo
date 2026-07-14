/**
 * Radix-based toast surface. `ToastProvider`/`ToastViewport` frame the app
 * (mounted in AppShell); `Toasts` renders whatever the global toast store
 * (`stores/toasts.ts`) currently holds — success/error/info feedback for
 * actions whose outcome isn't visible next to the button that fired them.
 */
import * as Toast from "@radix-ui/react-toast";
import { CheckCircle2, CircleAlert, Info, X } from "lucide-react";
import { useToastStore, type ToastItem } from "@/stores/toasts";
import { cn } from "@/lib/cn";

export const ToastProvider = Toast.Provider;
export const ToastViewport = () => (
  <Toast.Viewport className="fixed bottom-4 right-4 z-[200] flex max-h-screen w-80 flex-col gap-2 p-0 outline-none" />
);

const KIND_ICON = {
  success: <CheckCircle2 size={14} className="shrink-0 text-[var(--color-success)]" />,
  error: <CircleAlert size={14} className="shrink-0 text-[var(--color-error)]" />,
  info: <Info size={14} className="shrink-0 text-[var(--color-accent)]" />,
} as const;

function ToastCard({ item }: { item: ToastItem }) {
  const dismiss = useToastStore((s) => s.dismiss);
  return (
    <Toast.Root
      // Errors linger long enough to be read + acted on; success is a glance.
      duration={item.kind === "error" ? 8000 : 4000}
      onOpenChange={(open) => {
        if (!open) dismiss(item.id);
      }}
      className={cn(
        "flex items-start gap-2 rounded border bg-[var(--color-bg-elevated)] p-3 shadow-lg",
        "data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=closed]:fade-out-0",
        item.kind === "error"
          ? "border-[var(--color-error)]/40"
          : item.kind === "success"
            ? "border-[var(--color-success)]/40"
            : "border-[var(--color-border)]",
      )}
    >
      <span className="mt-0.5">{KIND_ICON[item.kind]}</span>
      <div className="min-w-0 flex-1 space-y-0.5">
        <Toast.Title className="text-xs font-medium text-[var(--color-fg-primary)]">
          {item.title}
        </Toast.Title>
        {item.description && (
          <Toast.Description className="break-words text-xs text-[var(--color-fg-muted)]">
            {item.description}
          </Toast.Description>
        )}
        {item.action && (
          <Toast.Action altText={item.action.label} asChild>
            <button
              onClick={item.action.onClick}
              className="mt-1 rounded border border-[var(--color-border)] px-2 py-0.5 text-xs font-medium text-[var(--color-fg-primary)] hover:bg-[var(--color-bg-hover)]"
            >
              {item.action.label}
            </button>
          </Toast.Action>
        )}
      </div>
      <Toast.Close
        aria-label="Dismiss"
        className="rounded p-0.5 text-[var(--color-fg-muted)] hover:bg-[var(--color-bg-hover)] hover:text-[var(--color-fg-primary)]"
      >
        <X size={12} />
      </Toast.Close>
    </Toast.Root>
  );
}

/** Live list of toasts from the global store — mount once inside ToastProvider. */
export function Toasts() {
  const toasts = useToastStore((s) => s.toasts);
  return (
    <>
      {toasts.map((t) => (
        <ToastCard key={t.id} item={t} />
      ))}
    </>
  );
}
